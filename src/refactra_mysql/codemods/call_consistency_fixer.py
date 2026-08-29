"""
Cross-File Call Consistency FIXER — Automatically fixes stale call sites.

This script reuses the detection logic from call_consistency.py and applies
AST-based fixes to output files. It works in two passes:

  Pass 1 — "db_not_passed" fixes:
    Caller HAS `db` param but doesn't pass it to callee.
    Fix: Insert `db` as first positional argument to the call.

  Pass 2 — "missing_db_arg" fixes:
    Caller does NOT have `db` param, AND doesn't pass `db` to callee.
    Fix: (a) Add `db: Session` to caller's parameter list.
         (b) Insert `db` as first arg in the call.
    Note: This may cascade — fixing a caller may cause ITS callers to break.
          We run multiple passes until convergence.

Usage:
    refactra-mysql fix-consistency [--apply] [--max-passes N]

Relies on:
    - refactra_mysql.config for SOURCE_DIRS, OUTPUT_DIR (from .env)
    - refactra_mysql.quality.call_consistency for detection infrastructure
"""

import ast
import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from refactra_mysql.config import OUTPUT_DIR, REPORTS_DIR, SOURCE_DIRS, setup_logging
from refactra_mysql.io_utils import atomic_write_text
from refactra_mysql.quality.call_consistency import (
    SignatureChange,
    CallIssue,
    FileResult,
    build_signature_changes,
    build_import_graph,
    analyze_file_calls,
)

logger = setup_logging("call_fixer")

_REPORTS_DIR = REPORTS_DIR / "codemods"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class FixAction:
    """A single fix to apply to a file."""

    file: str
    line: int
    fix_type: str  # "insert_db_arg", "add_db_param"
    caller_func: str
    callee_func: str
    detail: str = ""


@dataclass
class FileFixResult:
    """Fix results for one file."""

    file: str
    fixes_applied: int = 0
    fixes_skipped: int = 0
    actions: List[FixAction] = field(default_factory=list)
    error: str = ""


# ============================================================================
# Phase 1: Detect Issues (reuse call_consistency infrastructure)
# ============================================================================


def detect_all_issues(
    source_dirs: List[Path],
    output_dir: Path,
) -> Tuple[List[FileResult], Dict[Tuple[str, str], SignatureChange]]:
    """
    Run the full call consistency analysis and return issues + signature changes.
    """
    logger.info("Phase 1: Building signature change map...")
    changes = build_signature_changes(source_dirs, output_dir)
    db_changes = {k: v for k, v in changes.items() if v.db_added}
    logger.info(
        "  Found %d signature changes (%d with db added)",
        len(changes),
        len(db_changes),
    )

    logger.info("Phase 2: Building import graph...")
    import_graph = build_import_graph(output_dir)
    total_imports = sum(len(v) for v in import_graph.values())
    logger.info(
        "  Resolved %d cross-file imports across %d files",
        total_imports,
        len(import_graph),
    )

    logger.info("Phase 3: Analyzing call sites...")
    output_files = sorted(output_dir.rglob("*.py"))
    output_files = [f for f in output_files if "__pycache__" not in str(f)]

    results: List[FileResult] = []
    for output_path in output_files:
        result = analyze_file_calls(output_path, output_dir, changes, import_graph)
        results.append(result)

    total_critical = sum(r.critical_count for r in results)
    logger.info("  Found %d critical issues across %d files", total_critical, len(results))

    return results, changes


# ============================================================================
# Phase 2: Apply Fixes via Line-Level Rewriting
# ============================================================================


def _find_call_in_line(line_text: str, callee_func: str) -> Optional[Tuple[int, str]]:
    """
    Find the call site in a line for a given callee function name.

    Returns:
        (position_of_open_paren, function_name_as_appears) or None
    """
    # Handle qualified names: "ClassName.method" → search for "method("
    func_parts = callee_func.split(".")
    search_name = func_parts[-1]

    # Look for the function call pattern
    pattern = re.compile(r'\b' + re.escape(search_name) + r'\s*\(')
    match = pattern.search(line_text)
    if match:
        # Return position of the opening paren
        paren_pos = line_text.index("(", match.start())
        return (paren_pos, search_name)

    return None


def _insert_db_arg_in_line(line_text: str, callee_func: str) -> Optional[str]:
    """
    Insert `db` as the first positional argument in a function call.

    Also handles lambda expressions — if the call is inside a lambda,
    adds `db` to the lambda's parameter list too.

    Example:
        get_company_tax_rates(company_id) → get_company_tax_rates(db, company_id)
        get_company_tax_rates()           → get_company_tax_rates(db)
        lambda req, lang: home(req, lang) → lambda db, req, lang: home(db, req, lang)
    """
    result = _find_call_in_line(line_text, callee_func)
    if not result:
        return None

    paren_pos, _ = result

    # Check what's after the opening paren
    after_paren = line_text[paren_pos + 1:].lstrip()

    if after_paren.startswith(")"):
        # Empty args: func() → func(db)
        new_line = line_text[:paren_pos + 1] + "db" + line_text[paren_pos + 1:]
    else:
        # Has args: func(x, y) → func(db, x, y)
        new_line = line_text[:paren_pos + 1] + "db, " + line_text[paren_pos + 1:]

    # Handle lambda: if the call is inside a lambda, add db to lambda params too
    # Pattern: lambda <params>: <call>
    lambda_pattern = re.compile(r'\blambda\s+')
    lambda_match = lambda_pattern.search(new_line)
    if lambda_match:
        # Check that the lambda wraps our call (lambda appears before the call)
        if lambda_match.start() < paren_pos:
            # Find the colon that separates lambda params from body
            colon_pos = new_line.index(":", lambda_match.end())
            lambda_params_start = lambda_match.end()
            lambda_params = new_line[lambda_params_start:colon_pos].strip()

            # Only add db if not already in lambda params
            if not re.search(r'\bdb\b', lambda_params):
                new_line = (
                    new_line[:lambda_params_start]
                    + "db, " + lambda_params
                    + new_line[colon_pos:]
                )

    return new_line


def _add_db_param_to_function(
    lines: List[str],
    func_name: str,
    tree: ast.Module,
) -> Optional[int]:
    """
    Add `db: Session` as the first parameter to a function definition.

    Handles:
        - def func(x, y):           → def func(db: Session, x, y):
        - def func(self, x):        → def func(self, db: Session, x):
        - async def func(x, y):     → async def func(db: Session, x, y):
        - def func():               → def func(db: Session):
        - Multi-line def func(\n...) → def func(\n    db: Session,\n...

    Returns:
        Line number that was modified, or None if not found/already has db.
    """
    # Find the function definition in the AST
    target_func = None
    simple_name = func_name.split(".")[-1] if "." in func_name else func_name

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == simple_name:
                # Check it doesn't already have db
                param_names = [a.arg for a in node.args.args]
                if "db" in param_names:
                    return None  # Already fixed
                target_func = node
                break

    if not target_func:
        return None

    func_line_idx = target_func.lineno - 1  # 0-indexed
    line = lines[func_line_idx]

    # Detect if multi-line def
    if "(" in line and ")" not in line:
        # Multi-line: insert db: Session as the first parameter
        paren_pos = line.index("(")
        after_paren = line[paren_pos + 1:].strip()

        if not after_paren:
            # Nothing after '(' — params start on next line
            # Insert db: Session, on the next line BEFORE existing params
            next_line_idx = func_line_idx + 1
            if next_line_idx < len(lines):
                next_line = lines[next_line_idx]
                indent = len(next_line) - len(next_line.lstrip())
                db_line = " " * indent + "db: Session,\n"
                lines.insert(next_line_idx, db_line)
                return target_func.lineno
            return None
        elif after_paren.startswith("self") or after_paren.startswith("cls"):
            # self/cls on the opening line: def func(self,\n    param1, ...)
            # Insert after self/cls on the same line
            try:
                comma_after_self = line.index(",", paren_pos)
                space_after = ""
                if comma_after_self + 1 < len(line) and line[comma_after_self + 1] == " ":
                    space_after = " "
                # Check if there's content after the comma on this line
                rest = line[comma_after_self + 1:].strip()
                if rest and rest != "\n":
                    # Params continue on same line: def func(self, param1,\n
                    new_line = (
                        line[:comma_after_self + 1]
                        + space_after
                        + "db: Session, "
                        + line[comma_after_self + 1 + len(space_after):]
                    )
                    lines[func_line_idx] = new_line
                else:
                    # Only self, on this line: def func(self,\n    param1, ...)
                    next_line_idx = func_line_idx + 1
                    if next_line_idx < len(lines):
                        next_line = lines[next_line_idx]
                        indent = len(next_line) - len(next_line.lstrip())
                        db_line = " " * indent + "db: Session,\n"
                        lines.insert(next_line_idx, db_line)
                return target_func.lineno
            except ValueError:
                # self is last, no comma: def func(self\n    ):
                # This shouldn't happen in multi-line, treat carefully
                pass
        else:
            # Params start on the opening line: def func(param1,\n    param2, ...)
            # Insert db: Session right after '('
            new_line = line[:paren_pos + 1] + "db: Session, " + line[paren_pos + 1:]
            lines[func_line_idx] = new_line
            return target_func.lineno
        return None

    # Single-line def
    paren_pos = line.index("(")
    after_paren = line[paren_pos + 1:].lstrip()

    if after_paren.startswith("self") or after_paren.startswith("cls"):
        # Insert after self/cls
        # Check if there's a comma after self/cls (more params follow)
        try:
            comma_after_self = line.index(",", paren_pos)
            space_after_comma = ""
            if comma_after_self + 1 < len(line) and line[comma_after_self + 1] == " ":
                space_after_comma = " "
            new_line = (
                line[: comma_after_self + 1]
                + space_after_comma
                + "db: Session, "
                + line[comma_after_self + 1 + len(space_after_comma) :]
            )
        except ValueError:
            # No comma — self/cls is the only param: def func(self): → def func(self, db: Session):
            close_paren = line.index(")", paren_pos)
            new_line = (
                line[:close_paren]
                + ", db: Session"
                + line[close_paren:]
            )
    elif after_paren.startswith(")"):
        # Empty params: def func(): → def func(db: Session):
        new_line = line[: paren_pos + 1] + "db: Session" + line[paren_pos + 1 :]
    else:
        # Has params: def func(x, y): → def func(db: Session, x, y):
        new_line = line[: paren_pos + 1] + "db: Session, " + line[paren_pos + 1 :]

    lines[func_line_idx] = new_line
    return target_func.lineno


def _ensure_session_import(lines: List[str]) -> bool:
    """
    Ensure `from sqlalchemy.orm import Session` is present.
    Returns True if import was added.
    """
    # Check if Session is already imported
    for line in lines:
        if "Session" in line and "sqlalchemy" in line:
            return False
        if "from sqlalchemy.orm import" in line and "Session" in line:
            return False

    # Check if there's an existing sqlalchemy.orm import to extend
    for i, line in enumerate(lines):
        if "from sqlalchemy.orm import" in line:
            # Add Session to existing import
            stripped = line.rstrip()
            if stripped.endswith(")"):
                # Multi-line import — skip for now, too complex
                pass
            else:
                lines[i] = stripped + ", Session\n"
                return True

    # Find the right place to insert (after other imports)
    insert_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            insert_idx = i + 1
        elif stripped and not stripped.startswith("#") and not stripped.startswith('"""') and not stripped.startswith("'''"):
            if insert_idx > 0:
                break

    lines.insert(insert_idx, "from sqlalchemy.orm import Session\n")
    return True


def apply_fixes_to_file(
    output_path: Path,
    output_dir: Path,
    issues: List[CallIssue],
    dry_run: bool = False,
) -> FileFixResult:
    """
    Apply call consistency fixes to a single output file.

    Fix strategy:
      1. "db_not_passed" issues: Caller has db, just insert db in call.
      2. "missing_db_arg" issues: Add db: Session to caller signature + insert db in call.
    """
    rel_path = str(output_path.relative_to(output_dir))
    result = FileFixResult(file=rel_path)

    try:
        original_content = output_path.read_text(encoding="utf-8")
        lines = original_content.splitlines(keepends=True)
    except OSError as e:
        result.error = str(e)
        return result

    # Sort issues by line number (descending) so line numbers stay valid
    sorted_issues = sorted(issues, key=lambda i: i.line, reverse=True)

    # Track which caller functions need db: Session added to their signature
    callers_needing_db: Set[str] = set()
    session_import_needed = False

    for issue in sorted_issues:
        line_idx = issue.line - 1  # 0-indexed

        if line_idx < 0 or line_idx >= len(lines):
            result.fixes_skipped += 1
            continue

        line_text = lines[line_idx]

        # Check if db is already there (previous fix in same pass)
        if _find_call_in_line(line_text, issue.callee_func):
            # Try to check if db is already in args
            # Simple heuristic: look for `func(db,` or `func(db)`
            func_parts = issue.callee_func.split(".")
            search_name = func_parts[-1]
            pattern_check = re.compile(
                re.escape(search_name) + r'\s*\(\s*db\s*[,)]'
            )
            if pattern_check.search(line_text):
                result.fixes_skipped += 1
                continue

        # Fix 1: Insert `db` as first arg in the call
        new_line = _insert_db_arg_in_line(line_text, issue.callee_func)
        if new_line and new_line != line_text:
            lines[line_idx] = new_line
            result.fixes_applied += 1
            result.actions.append(
                FixAction(
                    file=rel_path,
                    line=issue.line,
                    fix_type="insert_db_arg",
                    caller_func=issue.caller_func,
                    callee_func=issue.callee_func,
                    detail=f"{line_text.strip()} → {new_line.strip()}",
                )
            )

            # If caller doesn't have db, mark it for signature fix
            if issue.issue_type == "missing_db_arg":
                callers_needing_db.add(issue.caller_func)
                session_import_needed = True
        else:
            result.fixes_skipped += 1

    # Fix 2: Add `db: Session` to caller function signatures
    if callers_needing_db:
        try:
            # Re-parse after call fixes (line numbers may have shifted)
            current_content = "".join(lines)
            tree = ast.parse(current_content)

            for caller_func in callers_needing_db:
                modified_line = _add_db_param_to_function(lines, caller_func, tree)
                if modified_line:
                    result.fixes_applied += 1
                    result.actions.append(
                        FixAction(
                            file=rel_path,
                            line=modified_line,
                            fix_type="add_db_param",
                            caller_func=caller_func,
                            callee_func="",
                            detail=f"Added db: Session to {caller_func}()",
                        )
                    )
                    # Re-parse after each signature change (line count may change)
                    current_content = "".join(lines)
                    try:
                        tree = ast.parse(current_content)
                    except SyntaxError:
                        logger.warning("Syntax error after modifying %s in %s", caller_func, rel_path)
                        break
        except SyntaxError as e:
            logger.warning("Cannot parse %s for signature fixes: %s", rel_path, e)

    # Fix 3: Ensure Session import exists
    if session_import_needed:
        if _ensure_session_import(lines):
            result.fixes_applied += 1
            result.actions.append(
                FixAction(
                    file=rel_path,
                    line=0,
                    fix_type="add_import",
                    caller_func="",
                    callee_func="",
                    detail="Added: from sqlalchemy.orm import Session",
                )
            )

    # Write the fixed file
    if result.fixes_applied > 0:
        new_content = "".join(lines)

        # Verify syntax is still valid
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            result.error = f"Fix produced invalid syntax: {e}"
            logger.error("ABORTING %s — syntax error after fixes: %s", rel_path, e)
            # Restore original content
            if not dry_run:
                atomic_write_text(output_path, original_content)
            return result

        if not dry_run:
            atomic_write_text(output_path, new_content)
            logger.info("  [PASS] Fixed %s: %d fixes applied", rel_path, result.fixes_applied)
        else:
            logger.info("  [DRY-RUN] Would fix %s: %d fixes", rel_path, result.fixes_applied)

    return result


# ============================================================================
# Multi-Pass Fix Engine
# ============================================================================


def run_fix_passes(
    source_dirs: List[Path],
    output_dir: Path,
    max_passes: int = 5,
    dry_run: bool = False,
) -> List[FileFixResult]:
    """
    Run fix passes until convergence (no more issues) or max_passes reached.

    Each pass:
      1. Detect all call consistency issues
      2. Apply fixes to each file
      3. Re-detect to check for cascading issues
    """
    all_results: List[FileFixResult] = []

    for pass_num in range(1, max_passes + 1):
        logger.info("")
        logger.info("=" * 60)
        logger.info("  FIX PASS %d / %d", pass_num, max_passes)
        logger.info("=" * 60)

        # Detect issues
        analysis_results, changes = detect_all_issues(source_dirs, output_dir)

        # Collect all critical issues grouped by file
        issues_by_file: Dict[str, List[CallIssue]] = defaultdict(list)
        total_critical = 0
        for r in analysis_results:
            for issue in r.issues:
                if issue.severity == "critical":
                    issues_by_file[r.output_file].append(issue)
                    total_critical += 1

        if total_critical == 0:
            logger.info("  [PASS] No more critical issues! Converged at pass %d.", pass_num)
            break

        logger.info("  Found %d critical issues in %d files", total_critical, len(issues_by_file))

        # Apply fixes
        pass_fixes = 0
        for rel_file, issues in sorted(issues_by_file.items()):
            output_path = output_dir / rel_file

            fix_result = apply_fixes_to_file(
                output_path, output_dir, issues, dry_run=dry_run
            )
            all_results.append(fix_result)
            pass_fixes += fix_result.fixes_applied

        logger.info("  Pass %d: applied %d fixes", pass_num, pass_fixes)

        if pass_fixes == 0:
            logger.info("  [WARN]  No fixes applied this pass — stopping to avoid infinite loop.")
            break

        if dry_run:
            logger.info("  [DRY-RUN] Stopping after first pass in dry-run mode.")
            break

    return all_results


# ============================================================================
# Report
# ============================================================================


def print_fix_report(results: List[FileFixResult]) -> None:
    """Print a summary of all fixes applied."""
    total_fixes = sum(r.fixes_applied for r in results)
    total_skipped = sum(r.fixes_skipped for r in results)
    files_fixed = sum(1 for r in results if r.fixes_applied > 0)
    errors = [r for r in results if r.error]

    print()
    print("=" * 80)
    print("  CALL CONSISTENCY FIXER — RESULTS")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    print(f"  Files processed:    {len(results)}")
    print(f"  Files fixed:        {files_fixed}")
    print(f"  Total fixes:        {total_fixes}")
    print(f"  Fixes skipped:      {total_skipped}")
    if errors:
        print(f"  Errors:             {len(errors)}")
    print()

    # Fix type breakdown
    fix_types: dict[str, int] = defaultdict(int)
    for r in results:
        for action in r.actions:
            fix_types[action.fix_type] += 1

    if fix_types:
        print("  Fix breakdown:")
        for ft, count in sorted(fix_types.items()):
            labels = {
                "insert_db_arg": "[CHANGE] db argument inserted into call",
                "add_db_param": "[NOTE] db: Session added to function signature",
                "add_import": "[IMPORT] Session import added",
            }
            label = labels.get(ft, ft)
            print(f"    {label}: {count}")
        print()

    # Per-file details
    for r in results:
        if r.fixes_applied > 0:
            print(f"  [PASS] {r.file}: {r.fixes_applied} fixes")
            for action in r.actions:
                if action.detail:
                    print(f"       L{action.line}: {action.detail[:120]}")
        if r.error:
            print(f"  [FAIL] {r.file}: ERROR — {r.error}")

    print()
    print("=" * 80)
    if total_fixes > 0:
        print(f"  [PASS] Applied {total_fixes} fixes across {files_fixed} files.")
        print("  [WARN]  Run the call_consistency checker to verify remaining issues.")
    else:
        print("  [INFO]  No fixes were needed or applicable.")
    print("=" * 80)
    print()


def save_fix_report(results: List[FileFixResult], output_path: Path) -> Path:
    """Save fix report as JSON."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_fixes": sum(r.fixes_applied for r in results),
        "total_skipped": sum(r.fixes_skipped for r in results),
        "files_fixed": sum(1 for r in results if r.fixes_applied > 0),
        "files": [
            {
                "file": r.file,
                "fixes_applied": r.fixes_applied,
                "fixes_skipped": r.fixes_skipped,
                "error": r.error,
                "actions": [
                    {
                        "line": a.line,
                        "fix_type": a.fix_type,
                        "caller_func": a.caller_func,
                        "callee_func": a.callee_func,
                        "detail": a.detail,
                    }
                    for a in r.actions
                ],
            }
            for r in results
            if r.fixes_applied > 0 or r.error
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, json.dumps(report, indent=2, default=str))
    logger.info("Fix report saved: %s", output_path)
    return output_path


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Automatically fix stale call sites after AI conversion. "
            "Inserts missing `db` arguments and adds `db: Session` "
            "parameters to caller functions."
        ),
    )
    parser.add_argument(
        "--source-dirs",
        default=None,
        help="Comma-separated source directories (default: from .env)",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Directory with converted files (default: from .env)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag, only a preview is produced.",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=5,
        help="Maximum fix passes for cascading fixes (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Save JSON fix report",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    # Resolve directories
    if args.source_dirs:
        source_dirs = [Path(p.strip()) for p in args.source_dirs.split(",")]
    else:
        source_dirs = SOURCE_DIRS

    if not source_dirs:
        logger.error("SOURCE_DIR not configured.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        logger.error("Output directory not found: %s", output_dir)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Call Consistency Fixer")
    logger.info("=" * 60)
    logger.info("Source dirs: %s", [str(s) for s in source_dirs])
    logger.info("Output dir:  %s", output_dir)
    logger.info("Dry run:     %s", dry_run)
    logger.info("Max passes:  %d", args.max_passes)

    # Run fix passes
    results = run_fix_passes(
        source_dirs,
        output_dir,
        max_passes=args.max_passes,
        dry_run=dry_run,
    )

    # Print report
    print_fix_report(results)

    # Save JSON report
    if args.json:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = _REPORTS_DIR / f"call_consistency_fix_{timestamp}.json"
        save_fix_report(results, json_path)

    # Final verification
    if not dry_run and any(r.fixes_applied > 0 for r in results):
        print("\n  [CHECK] Running final verification...")
        final_results, _ = detect_all_issues(source_dirs, output_dir)
        remaining = sum(r.critical_count for r in final_results)
        print(f"\n  [SUMMARY] Remaining critical issues: {remaining}")
        if remaining == 0:
            print("  [PASS] ALL CALL SITES ARE NOW CONSISTENT!")
        else:
            print(f"  [WARN]  {remaining} issues remain — may need manual review.")
            print("     These could be:")
            print("     - Calls from non-converted files")
            print("     - Complex call patterns (*args, **kwargs)")
            print("     - Chained calls through variables")


if __name__ == "__main__":
    main()

"""
Cross-File Call Consistency Validator — Detects stale call sites after conversion.

When the AI converter adds `db: Session` to a function's signature, ALL callers
of that function must also pass `db`. This script detects call sites that were
NOT updated — which would cause TypeError at runtime.

Approach:
  Phase 1: Build a map of all functions whose signature changed (db was added)
  Phase 2: Build an import graph across output files
  Phase 3: For each call site, verify arguments match the new signature
  Phase 4: Check if the calling function has access to `db`

Limitations (honest assessment):
  - Cannot trace calls through variables: obj = SomeClass(); obj.method()
  - Cannot verify *args/**kwargs pass-through
  - Cannot trace dynamic imports (importlib, __import__)
  - May produce false positives for overloaded/same-named functions

Usage:
    refactra-mysql consistency
    refactra-mysql consistency --json
    refactra-mysql consistency --file output/admin/customers.py
"""
import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from refactra_mysql.config import OUTPUT_DIR, REPORTS_DIR, SOURCE_DIRS, setup_logging

logger = setup_logging("call_consistency")

_REPORTS_DIR = REPORTS_DIR / "quality"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class SignatureChange:
    """A function whose signature changed between source and output."""

    module: str  # output module path, e.g. "database.queries.customers"
    func_name: str  # function name, e.g. "get_customer_by_id"
    qualified_name: str  # e.g. "ClassName.method" or "func"
    source_params: List[str]
    output_params: List[str]
    params_added: List[str]
    params_removed: List[str]
    min_required_args: int  # minimum args needed (excluding self/cls)
    db_added: bool  # True if `db` was specifically added
    db_position: int  # position of db in param list (-1 if not present)


@dataclass
class CallIssue:
    """A call site that may be inconsistent with the new signature."""

    caller_file: str
    caller_func: str  # function containing the call
    callee_module: str
    callee_func: str
    line: int
    severity: str  # "critical", "warning", "info"
    issue_type: str
    message: str
    detail: str = ""
    call_args_count: int = 0
    expected_min_args: int = 0


@dataclass
class FileResult:
    """Call consistency analysis for one file."""

    output_file: str
    issues: List[CallIssue] = field(default_factory=list)
    calls_checked: int = 0
    imports_resolved: int = 0
    imports_unresolved: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def is_clean(self) -> bool:
        return self.critical_count == 0


# ============================================================================
# Phase 1: Build Signature Change Map
# ============================================================================


def _get_params(func_node) -> List[str]:
    """Extract parameter names from function definition."""
    params = []
    for arg in func_node.args.posonlyargs:
        params.append(arg.arg)
    for arg in func_node.args.args:
        params.append(arg.arg)
    if func_node.args.vararg:
        params.append(f"*{func_node.args.vararg.arg}")
    for arg in func_node.args.kwonlyargs:
        params.append(arg.arg)
    if func_node.args.kwarg:
        params.append(f"**{func_node.args.kwarg.arg}")
    return params


def _count_min_required(func_node) -> int:
    """Count minimum required positional args (no defaults, no self/cls)."""
    args = func_node.args
    all_args = list(args.posonlyargs) + list(args.args)
    num_defaults = len(args.defaults)
    required = len(all_args) - num_defaults

    # Exclude self/cls
    if all_args and all_args[0].arg in ("self", "cls"):
        required -= 1

    return max(0, required)


def build_signature_changes(
    source_dirs: List[Path], output_dir: Path
) -> Dict[Tuple[str, str], SignatureChange]:
    """
    Compare source vs output files and find all signature changes.

    Returns:
        Dict mapping (module_path, func_qualified_name) → SignatureChange
    """
    changes: Dict[Tuple[str, str], SignatureChange] = {}

    output_files = sorted(output_dir.rglob("*.py"))
    output_files = [f for f in output_files if "__pycache__" not in str(f)]

    for output_path in output_files:
        rel = output_path.relative_to(output_dir)
        module_path = str(rel).replace("/", ".").replace(".py", "")

        # Find source file
        source_path = None
        for sd in source_dirs:
            candidate = sd / rel
            if candidate.is_file():
                source_path = candidate
                break
        if not source_path:
            continue

        try:
            source_code = source_path.read_text(encoding="utf-8")
            output_code = output_path.read_text(encoding="utf-8")
            source_tree = ast.parse(source_code)
            output_tree = ast.parse(output_code)
        except (SyntaxError, OSError):
            continue

        # Extract function signatures from both
        def extract_sigs(tree):
            sigs = {}

            def visit(node, scope=""):
                for child in ast.iter_child_nodes(node):
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        qname = (
                            f"{scope}.{child.name}" if scope else child.name
                        )
                        params = _get_params(child)
                        min_req = _count_min_required(child)
                        sigs[qname] = (params, min_req, child)
                    elif isinstance(child, ast.ClassDef):
                        visit(child, scope=child.name)

            visit(tree)
            return sigs

        source_sigs = extract_sigs(source_tree)
        output_sigs = extract_sigs(output_tree)

        for qname in source_sigs:
            if qname not in output_sigs:
                continue

            s_params, _, _ = source_sigs[qname]
            o_params, o_min_req, o_node = output_sigs[qname]

            # Filter out self/cls for comparison
            s_real = [p for p in s_params if p not in ("self", "cls")]
            o_real = [p for p in o_params if p not in ("self", "cls")]

            if s_real != o_real:
                added = [p for p in o_real if p not in s_real]
                removed = [p for p in s_real if p not in o_real]

                db_added = "db" in added
                db_pos = -1
                if "db" in o_real:
                    db_pos = o_real.index("db")

                func_name = qname.split(".")[-1] if "." in qname else qname

                change = SignatureChange(
                    module=module_path,
                    func_name=func_name,
                    qualified_name=qname,
                    source_params=s_params,
                    output_params=o_params,
                    params_added=added,
                    params_removed=removed,
                    min_required_args=o_min_req,
                    db_added=db_added,
                    db_position=db_pos,
                )
                changes[(module_path, qname)] = change

    return changes


# ============================================================================
# Phase 2: Import Graph
# ============================================================================


@dataclass
class ImportEntry:
    """A single import resolution."""

    local_name: str  # name used in the calling file
    source_module: str  # module imported from
    original_name: str  # name in the source module
    resolved_output_module: str  # mapped to output module path


def build_import_graph(
    output_dir: Path,
) -> Dict[str, List[ImportEntry]]:
    """
    Build an import resolution map for each output file.

    Returns:
        Dict mapping output_file_rel_path → [ImportEntry]
    """
    # Build output module lookup
    output_modules: Set[str] = set()
    for pyfile in output_dir.rglob("*.py"):
        if "__pycache__" not in str(pyfile):
            rel = pyfile.relative_to(output_dir)
            mod = str(rel).replace("/", ".").replace(".py", "")
            output_modules.add(mod)

    import_graph: Dict[str, List[ImportEntry]] = {}

    for pyfile in sorted(output_dir.rglob("*.py")):
        if "__pycache__" in str(pyfile):
            continue

        rel_path = str(pyfile.relative_to(output_dir))

        try:
            src = pyfile.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue

        entries: List[ImportEntry] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue

            source_module = node.module

            # Try the full module first, then progressively remove leading
            # package segments. This supports arbitrary project layouts without
            # embedding application-specific package names.
            source_parts = source_module.split(".")
            candidates = [
                ".".join(source_parts[index:])
                for index in range(len(source_parts))
            ]

            resolved = None
            for c in candidates:
                if c in output_modules:
                    resolved = c
                    break

            if not resolved:
                # Fallback: sub-module import pattern
                # e.g., from project.data.queries import company as company_queries
                # Here source_module="project.data.queries" is a PACKAGE (not a file)
                # and "company" is a sub-module (company.py)
                # So candidate + "." + alias.name might be in output_modules
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    for c in candidates:
                        sub_mod = f"{c}.{alias.name}"
                        if sub_mod in output_modules:
                            local = alias.asname if alias.asname else alias.name
                            entries.append(
                                ImportEntry(
                                    local_name=local,
                                    source_module=source_module,
                                    original_name=alias.name,
                                    resolved_output_module=sub_mod,
                                )
                            )
                            break
                continue

            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname if alias.asname else alias.name
                entries.append(
                    ImportEntry(
                        local_name=local,
                        source_module=source_module,
                        original_name=alias.name,
                        resolved_output_module=resolved,
                    )
                )

        import_graph[rel_path] = entries

    return import_graph


# ============================================================================
# Phase 3: Call Site Analysis
# ============================================================================


def _get_call_name(call_node: ast.Call) -> Optional[Tuple[str, str]]:
    """
    Extract the name from a Call node.

    Returns:
        (base, attr) tuple:
        - Direct call func() → ("func", "")
        - Attribute call obj.method() → ("obj", "method")
        - Chained call a.b.c() → ("a.b", "c")
        Returns None for complex calls (subscript, etc.)
    """
    func = call_node.func

    if isinstance(func, ast.Name):
        return (func.id, "")
    elif isinstance(func, ast.Attribute):
        # Get the base object name
        parts = []
        node = func.value
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            parts.reverse()
            base = ".".join(parts)
            return (base, func.attr)

    return None


def _count_call_args(call_node: ast.Call) -> Tuple[int, bool]:
    """
    Count arguments in a Call node.

    Returns:
        (arg_count, has_starargs) — has_starargs is True if *args or **kwargs used
    """
    positional = len(call_node.args)
    keyword = len(call_node.keywords)

    has_star = any(
        isinstance(a, ast.Starred) for a in call_node.args
    )
    has_double_star = any(k.arg is None for k in call_node.keywords)

    return (positional + keyword, has_star or has_double_star)


def _has_db_arg(call_node: ast.Call) -> bool:
    """Check if 'db' is passed as a positional or keyword argument."""
    # Check positional: first arg is a Name node with id 'db'
    if call_node.args:
        first = call_node.args[0]
        if isinstance(first, ast.Name) and first.id == "db":
            return True
        # Check all positional args
        for arg in call_node.args:
            if isinstance(arg, ast.Name) and arg.id == "db":
                return True

    # Check keyword: db=something
    for kw in call_node.keywords:
        if kw.arg == "db":
            return True

    return False


def _get_enclosing_function(
    tree: ast.Module, line: int
) -> Optional[Tuple[str, bool]]:
    """
    Find the function containing a given line number.

    Returns:
        (function_name, has_db_param) or None
    """
    best = None
    best_start = 0

    def visit(node, scope=""):
        nonlocal best, best_start
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    if child.lineno >= best_start:
                        qname = (
                            f"{scope}.{child.name}" if scope else child.name
                        )
                        params = [a.arg for a in child.args.args]
                        has_db = "db" in params
                        best = (qname, has_db)
                        best_start = child.lineno
                # Recurse into nested scopes
                visit(child, scope=child.name if not scope else f"{scope}.{child.name}")
            elif isinstance(child, ast.ClassDef):
                visit(child, scope=child.name)

    visit(tree)
    return best


def analyze_file_calls(
    output_path: Path,
    output_dir: Path,
    changes: Dict[Tuple[str, str], SignatureChange],
    import_graph: Dict[str, List[ImportEntry]],
) -> FileResult:
    """Analyze all call sites in a single output file for consistency."""
    rel_path = str(output_path.relative_to(output_dir))
    result = FileResult(output_file=rel_path)

    try:
        src = output_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (SyntaxError, OSError):
        return result

    # Get imports for this file
    file_imports = import_graph.get(rel_path, [])
    result.imports_resolved = len(file_imports)

    # Build local name → (output_module, original_name) map
    import_map: Dict[str, Tuple[str, str]] = {}
    for entry in file_imports:
        import_map[entry.local_name] = (
            entry.resolved_output_module,
            entry.original_name,
        )

    # Get this file's own module path
    self_module = str(output_path.relative_to(output_dir)).replace(
        "/", "."
    ).replace(".py", "")

    # Walk all Call nodes
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_info = _get_call_name(node)
        if not call_info:
            continue

        base, attr = call_info

        # Try to resolve the call to a known changed function
        resolved_change: Optional[SignatureChange] = None
        callee_module = ""
        callee_func = ""

        if attr == "":
            # Direct call: func(args)
            # Check if func is imported from another module
            if base in import_map:
                mod, orig_name = import_map[base]

                # Try to find a matching signature change
                # The function might be top-level or class method
                key = (mod, orig_name)
                if key in changes:
                    resolved_change = changes[key]
                    callee_module = mod
                    callee_func = orig_name
            else:
                # Could be a local function — check same file
                key = (self_module, base)
                if key in changes:
                    resolved_change = changes[key]
                    callee_module = self_module
                    callee_func = base

        elif attr != "":
            # Attribute call: obj.method(args) or Class.method(args)
            # Check if 'base' is an imported name
            if base in import_map:
                mod, orig_name = import_map[base]

                # Strategy 1: Class.method pattern
                # e.g., DraftService._copy_page_contents()
                # key = (module, "DraftService._copy_page_contents")
                qname = f"{orig_name}.{attr}"
                key = (mod, qname)
                if key in changes:
                    resolved_change = changes[key]
                    callee_module = mod
                    callee_func = qname

                # Strategy 2: Module-as-alias pattern
                # e.g., from project.data.queries import company as company_queries
                #        company_queries.get_company_by_id()
                # The imported module may resolve as:
                #   - mod="data.queries.company" (sub-module fallback), key=(mod, attr)
                #   - mod="data.queries", key=(mod.orig_name, attr)
                if not resolved_change:
                    # Try direct: (resolved_module, attr)
                    key2 = (mod, attr)
                    if key2 in changes:
                        resolved_change = changes[key2]
                        callee_module = mod
                        callee_func = attr
                    else:
                        # Try sub-module: (resolved_module.orig_name, attr)
                        sub_module = f"{mod}.{orig_name}"
                        key3 = (sub_module, attr)
                        if key3 in changes:
                            resolved_change = changes[key3]
                            callee_module = sub_module
                            callee_func = attr

            # Also check if base is a local class in the same file
            key = (self_module, f"{base}.{attr}")
            if key in changes:
                resolved_change = changes[key]
                callee_module = self_module
                callee_func = f"{base}.{attr}"

        if not resolved_change:
            continue

        # We found a call to a function whose signature changed!
        result.calls_checked += 1

        arg_count, has_star = _count_call_args(node)

        # Skip if *args/**kwargs used — can't verify statically
        if has_star:
            continue

        # Check: does the call pass `db` if it was added?
        if resolved_change.db_added:
            call_has_db = _has_db_arg(node)

            if not call_has_db:
                # db was added but not passed — potential bug!
                # Check if the calling function has db access
                enclosing = _get_enclosing_function(tree, node.lineno)
                caller_name = enclosing[0] if enclosing else "module-level"
                caller_has_db = enclosing[1] if enclosing else False

                if not caller_has_db:
                    # Caller doesn't have db AND doesn't pass db → CRITICAL
                    result.issues.append(
                        CallIssue(
                            caller_file=rel_path,
                            caller_func=caller_name,
                            callee_module=callee_module,
                            callee_func=callee_func,
                            line=node.lineno,
                            severity="critical",
                            issue_type="missing_db_arg",
                            message=(
                                f"Call to '{callee_func}' at L{node.lineno} "
                                f"does NOT pass 'db', and caller '{caller_name}' "
                                f"has no 'db' parameter either"
                            ),
                            detail=(
                                f"Target signature changed: "
                                f"{resolved_change.source_params} → "
                                f"{resolved_change.output_params}"
                            ),
                            call_args_count=arg_count,
                            expected_min_args=resolved_change.min_required_args,
                        )
                    )
                else:
                    # Caller HAS db but doesn't pass it → likely a bug
                    result.issues.append(
                        CallIssue(
                            caller_file=rel_path,
                            caller_func=caller_name,
                            callee_module=callee_module,
                            callee_func=callee_func,
                            line=node.lineno,
                            severity="critical",
                            issue_type="db_not_passed",
                            message=(
                                f"Call to '{callee_func}' at L{node.lineno} "
                                f"does NOT pass 'db', but caller '{caller_name}' "
                                f"has 'db' available"
                            ),
                            detail=(
                                f"Change: {resolved_change.source_params} → "
                                f"{resolved_change.output_params}"
                            ),
                            call_args_count=arg_count,
                            expected_min_args=resolved_change.min_required_args,
                        )
                    )

        # General argument count check
        if arg_count < resolved_change.min_required_args:
            # Already reported as missing_db most likely, but catch others
            if not resolved_change.db_added:
                enclosing = _get_enclosing_function(tree, node.lineno)
                caller_name = enclosing[0] if enclosing else "module-level"
                result.issues.append(
                    CallIssue(
                        caller_file=rel_path,
                        caller_func=caller_name,
                        callee_module=callee_module,
                        callee_func=callee_func,
                        line=node.lineno,
                        severity="warning",
                        issue_type="arg_count_mismatch",
                        message=(
                            f"Call to '{callee_func}' at L{node.lineno} has "
                            f"{arg_count} args but needs ≥{resolved_change.min_required_args}"
                        ),
                        detail=(
                            f"Params added: {resolved_change.params_added}, "
                            f"removed: {resolved_change.params_removed}"
                        ),
                        call_args_count=arg_count,
                        expected_min_args=resolved_change.min_required_args,
                    )
                )

    return result


# ============================================================================
# Report Printing
# ============================================================================


def print_report(
    results: List[FileResult],
    changes: Dict[Tuple[str, str], SignatureChange],
    detailed: bool = True,
) -> bool:
    """Print human-readable report. Returns True if no criticals."""
    total_calls = sum(r.calls_checked for r in results)
    total_critical = sum(r.critical_count for r in results)
    total_issues = sum(len(r.issues) for r in results)
    changed_funcs = len(changes)
    db_added_funcs = sum(1 for c in changes.values() if c.db_added)
    clean_files = sum(1 for r in results if r.is_clean)

    print()
    print("=" * 80)
    print("  CROSS-FILE CALL CONSISTENCY REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    print(f"  Functions with signature changes: {changed_funcs}")
    print(f"  Functions with 'db' added:        {db_added_funcs}")
    print(f"  Cross-file call sites checked:    {total_calls}")
    print()

    # Per-file results
    for r in results:
        if not r.issues and r.calls_checked == 0:
            continue  # Skip files with no relevant calls

        if r.is_clean:
            if r.calls_checked > 0:
                print(f"  [PASS] {r.output_file}: {r.calls_checked} calls checked, clean")
        else:
            print(
                f"  [FAIL] {r.output_file}: {r.critical_count} critical, "
                f"{len(r.issues)} total issues"
            )

    # Critical issues detail
    files_with_criticals = [r for r in results if r.critical_count > 0]
    if files_with_criticals and detailed:
        print()
        print("-" * 80)
        print("  [FAIL] CRITICAL: Stale Call Sites (will cause TypeError at runtime)")
        print("-" * 80)
        for r in files_with_criticals:
            criticals = [i for i in r.issues if i.severity == "critical"]
            print(f"\n  [FILE] {r.output_file}")
            for issue in criticals:
                print(f"     [FAIL] L{issue.line} [{issue.issue_type}]")
                print(f"        {issue.message}")
                if issue.detail:
                    print(f"        → {issue.detail}")

    # Summary
    print()
    print("=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"  Files analyzed:          {len(results)}")
    print(f"  Files with calls:        {sum(1 for r in results if r.calls_checked > 0)}")
    print(f"  Clean files:             {clean_files}")
    print(f"  Total calls checked:     {total_calls}")
    print(f"  Critical issues:         {total_critical}")
    print(f"  Total issues:            {total_issues}")
    print()

    if total_critical == 0:
        print("  [PASS] ALL CROSS-FILE CALLS ARE CONSISTENT!")
    else:
        print(
            f"  [WARN]  {total_critical} STALE CALL SITES — "
            f"will cause TypeError at runtime!"
        )

    # Limitations reminder
    print()
    print("  [INFO]  Limitations of this static analysis:")
    print("     - Cannot trace calls through variables (obj.method())")
    print("     - Cannot verify *args/**kwargs forwarding")
    print("     - Only checks calls resolvable through import statements")
    print("     - Functions called from NON-converted files are not checked")
    print("=" * 80)
    print()

    return total_critical == 0


# ============================================================================
# JSON Report
# ============================================================================


def save_json_report(
    results: List[FileResult],
    changes: Dict[Tuple[str, str], SignatureChange],
    output_path: Path,
) -> Path:
    """Save machine-readable JSON report."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_files": len(results),
        "total_calls_checked": sum(r.calls_checked for r in results),
        "total_critical": sum(r.critical_count for r in results),
        "total_issues": sum(len(r.issues) for r in results),
        "changed_functions": len(changes),
        "db_added_functions": sum(1 for c in changes.values() if c.db_added),
        "all_consistent": all(r.is_clean for r in results),
        "signature_changes": [
            {
                "module": c.module,
                "function": c.qualified_name,
                "params_added": c.params_added,
                "params_removed": c.params_removed,
                "db_added": c.db_added,
            }
            for c in changes.values()
        ],
        "files": [
            {
                "file": r.output_file,
                "calls_checked": r.calls_checked,
                "critical_count": r.critical_count,
                "is_clean": r.is_clean,
                "issues": [
                    {
                        "line": i.line,
                        "severity": i.severity,
                        "issue_type": i.issue_type,
                        "caller_func": i.caller_func,
                        "callee_func": i.callee_func,
                        "callee_module": i.callee_module,
                        "message": i.message,
                        "detail": i.detail,
                    }
                    for i in r.issues
                ],
            }
            for r in results
            if r.calls_checked > 0 or r.issues
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    logger.info("JSON report saved: %s", output_path)
    return output_path


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Check cross-file call consistency after AI conversion. "
            "Detects stale call sites where new parameters (e.g., db: Session) "
            "are required but not passed."
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
        "--detailed",
        action="store_true",
        default=True,
        help="Show detailed issue info (default: True)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        default=False,
        help="Show only summary",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Save JSON report",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Analyze a single file",
    )
    args = parser.parse_args()

    # ── Resolve directories ──
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
    logger.info("Cross-File Call Consistency Validator")
    logger.info("=" * 60)

    # ── Phase 1: Build signature change map ──
    logger.info("Phase 1: Building signature change map...")
    changes = build_signature_changes(source_dirs, output_dir)
    db_changes = {k: v for k, v in changes.items() if v.db_added}
    logger.info(
        "  Found %d signature changes (%d with db added)",
        len(changes),
        len(db_changes),
    )

    # ── Phase 2: Build import graph ──
    logger.info("Phase 2: Building import graph...")
    import_graph = build_import_graph(output_dir)
    total_imports = sum(len(v) for v in import_graph.values())
    logger.info(
        "  Resolved %d cross-file imports across %d files",
        total_imports,
        len(import_graph),
    )

    # ── Phase 3: Analyze call sites ──
    logger.info("Phase 3: Analyzing call sites...")

    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            logger.error("File not found: %s", file_path)
            sys.exit(1)
        output_files = [file_path]
    else:
        output_files = sorted(output_dir.rglob("*.py"))
        output_files = [
            f for f in output_files if "__pycache__" not in str(f)
        ]

    results: List[FileResult] = []
    for output_path in output_files:
        result = analyze_file_calls(
            output_path, output_dir, changes, import_graph
        )
        results.append(result)

    # ── Phase 4: Report ──
    detailed = args.detailed and not args.summary_only
    all_ok = print_report(results, changes, detailed=detailed)

    # ── Save JSON ──
    if args.json:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = _REPORTS_DIR / f"call_consistency_{timestamp}.json"
        save_json_report(results, changes, json_path)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

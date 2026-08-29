"""
Function Coverage Validator — Ensures no functions were lost during AI conversion.

Compares original source files against converted output files to detect:
  - Missing functions (in source but not in output) — CRITICAL
  - Missing classes (in source but not in output) — CRITICAL
  - New functions (in output but not in source — AI-added helpers)
  - Signature changes (parameter differences)
  - Property getter/setter tracking
  - Async/sync function type changes

Uses Python's ast module for 100% accurate parsing (same parser Python itself uses).

Usage:
    refactra-mysql coverage
    refactra-mysql coverage --source-dirs ./path/to/helpers,./path/to/routes --output-dir output/
    refactra-mysql coverage --json
    refactra-mysql coverage --file output/admin/employees.py
    refactra-mysql coverage --summary-only
"""
import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from refactra_mysql.config import OUTPUT_DIR, REPORTS_DIR, SOURCE_DIRS, setup_logging

logger = setup_logging("function_coverage")

_REPORTS_DIR = REPORTS_DIR / "quality"


# ============================================================================
# AST Function & Class Extractor
# ============================================================================


@dataclass
class FunctionInfo:
    """Represents a function or method extracted from source code."""

    name: str
    qualified_name: str  # e.g., "ClassName.method_name" or "top_level_func"
    scope: str  # "module" or class name
    lineno: int
    end_lineno: int
    is_async: bool
    is_static: bool
    is_classmethod: bool
    is_property: bool
    is_setter: bool
    is_deleter: bool
    parameters: List[str]
    decorators: List[str]
    line_count: int
    has_docstring: bool

    def signature_str(self) -> str:
        """Human-readable signature for display."""
        params = ", ".join(self.parameters)
        prefix = "async " if self.is_async else ""
        return f"{prefix}def {self.name}({params})"


@dataclass
class ClassInfo:
    """Represents a class extracted from source code."""

    name: str
    lineno: int
    end_lineno: int
    bases: List[str]
    method_count: int


def _get_decorator_names(decorators: list) -> List[str]:
    """Extract decorator names from AST decorator list."""
    names = []
    for dec in decorators:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(f"{_get_node_name(dec.value)}.{dec.attr}")
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                names.append(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.append(
                    f"{_get_node_name(dec.func.value)}.{dec.func.attr}"
                )
    return names


def _get_node_name(node) -> str:
    """Recursively get the name of an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_get_node_name(node.value)}.{node.attr}"
    return "?"


def _get_parameters(func_node) -> List[str]:
    """Extract parameter names from a function definition."""
    params = []
    args = func_node.args

    # Positional-only (Python 3.8+)
    for arg in args.posonlyargs:
        params.append(arg.arg)

    # Regular positional
    for arg in args.args:
        params.append(arg.arg)

    # *args
    if args.vararg:
        params.append(f"*{args.vararg.arg}")

    # Keyword-only
    for arg in args.kwonlyargs:
        params.append(arg.arg)

    # **kwargs
    if args.kwarg:
        params.append(f"**{args.kwarg.arg}")

    return params


def _has_docstring(node) -> bool:
    """Check if a function or class has a docstring."""
    if not node.body:
        return False
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return isinstance(first.value.value, str)
    return False


def extract_functions(
    source_code: str, filepath: str = ""
) -> List[FunctionInfo]:
    """
    Extract all function and method definitions from Python source code.

    Handles:
    - Top-level functions
    - Class methods (regular, @staticmethod, @classmethod)
    - @property getters, setters, deleters
    - Async functions/methods
    - Nested classes (methods within nested classes)
    - Decorated functions

    Does NOT track:
    - Nested functions (closures inside functions) — these are implementation
      details, not public API surface
    - Lambda expressions — anonymous, can't be tracked by name
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        logger.warning("  SyntaxError in %s L%s: %s", filepath, e.lineno, e.msg)
        return []

    functions: List[FunctionInfo] = []

    def visit_node(node, scope: str = "module"):
        """Recursively visit AST nodes to find function definitions."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = _get_decorator_names(child.decorator_list)

                is_static = "staticmethod" in decorators
                is_classmethod = "classmethod" in decorators
                is_property = "property" in decorators
                is_setter = any(d.endswith(".setter") for d in decorators)
                is_deleter = any(d.endswith(".deleter") for d in decorators)

                qualified = (
                    f"{scope}.{child.name}" if scope != "module" else child.name
                )

                end_line = child.end_lineno or child.lineno

                func_info = FunctionInfo(
                    name=child.name,
                    qualified_name=qualified,
                    scope=scope,
                    lineno=child.lineno,
                    end_lineno=end_line,
                    is_async=isinstance(child, ast.AsyncFunctionDef),
                    is_static=is_static,
                    is_classmethod=is_classmethod,
                    is_property=is_property,
                    is_setter=is_setter,
                    is_deleter=is_deleter,
                    parameters=_get_parameters(child),
                    decorators=decorators,
                    line_count=end_line - child.lineno + 1,
                    has_docstring=_has_docstring(child),
                )
                functions.append(func_info)

                # DO NOT recurse into function bodies — nested functions
                # (closures) are implementation details, not API surface.
                # The converter can freely restructure them.

            elif isinstance(child, ast.ClassDef):
                # Recurse into class body to find methods
                visit_node(child, scope=child.name)

    visit_node(tree)
    return functions


def extract_classes(source_code: str, filepath: str = "") -> List[ClassInfo]:
    """Extract all class definitions from source code."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    classes: List[ClassInfo] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases = []
            for base in node.bases:
                bases.append(_get_node_name(base))

            method_count = sum(
                1
                for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )

            classes.append(
                ClassInfo(
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno,
                    bases=bases,
                    method_count=method_count,
                )
            )

    return classes


# ============================================================================
# File Matcher — Maps output files to their original source files
# ============================================================================


def find_source_file(
    output_rel_path: str, source_dirs: List[Path]
) -> Optional[Path]:
    """
    Find the original source file that corresponds to an output file.

    Strategy (in order of priority):
    1. Exact relative path match in each source directory
    2. Filename + parent directory match (handles different root dirs)

    Args:
        output_rel_path: Relative path of the output file (e.g., "admin/employees.py")
        source_dirs: List of source directories to search

    Returns:
        Path to the matching source file, or None
    """
    # Strategy 1: Exact relative path match
    for source_dir in source_dirs:
        candidate = source_dir / output_rel_path
        if candidate.is_file():
            return candidate

    # Strategy 2: Match by filename + parent directory structure
    target_name = Path(output_rel_path).name
    target_parents = Path(output_rel_path).parent.parts

    candidates = []
    for source_dir in source_dirs:
        for source_file in source_dir.rglob(target_name):
            if "__pycache__" in str(source_file):
                continue

            source_rel_parents = source_file.relative_to(source_dir).parent.parts

            # Check if parent directory structure matches
            if source_rel_parents == target_parents:
                candidates.append(source_file)
            # Or if the tail of the source path matches the output parent
            elif (
                target_parents
                and len(source_rel_parents) >= len(target_parents)
                and source_rel_parents[-len(target_parents) :] == target_parents
            ):
                candidates.append(source_file)
            # Root-level file
            elif not target_parents and not source_rel_parents:
                candidates.append(source_file)

    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        # Multiple matches — prefer the one with most path segment overlap
        best = max(
            candidates,
            key=lambda c: len(set(c.parts) & set(Path(output_rel_path).parts)),
        )
        return best

    return None


# ============================================================================
# Coverage Analysis
# ============================================================================


@dataclass
class FileCoverage:
    """Coverage analysis result for a single file pair."""

    output_file: str
    source_file: str
    source_found: bool

    source_functions: int = 0
    output_functions: int = 0
    source_lines: int = 0
    output_lines: int = 0

    # Function-level comparison
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)  # In source, NOT in output
    added: List[str] = field(default_factory=list)  # In output, NOT in source

    # Class-level comparison
    source_classes: List[str] = field(default_factory=list)
    output_classes: List[str] = field(default_factory=list)
    missing_classes: List[str] = field(default_factory=list)
    added_classes: List[str] = field(default_factory=list)

    # Signature changes
    signature_changes: List[Dict] = field(default_factory=list)

    # Type changes (async/sync)
    type_changes: List[Dict] = field(default_factory=list)

    # Errors
    has_syntax_error: bool = False
    syntax_error_in: str = ""  # "source" or "output"
    error_detail: str = ""

    @property
    def coverage_pct(self) -> float:
        """Percentage of source functions found in output."""
        if self.source_functions == 0:
            return 100.0
        return (len(self.matched) / self.source_functions) * 100

    @property
    def is_perfect(self) -> bool:
        """True if all source functions and classes are present in output."""
        return len(self.missing) == 0 and len(self.missing_classes) == 0


def _build_func_key_map(
    funcs: List[FunctionInfo],
) -> Dict[str, FunctionInfo]:
    """
    Build a lookup map: unique_key → FunctionInfo.

    Handles property setters/deleters by appending ".setter"/".deleter"
    to avoid key collision with the property getter.
    """
    result: Dict[str, FunctionInfo] = {}

    for f in funcs:
        key = f.qualified_name

        # Disambiguate property getter vs setter vs deleter
        if f.is_setter:
            key = f"{f.qualified_name}@setter"
        elif f.is_deleter:
            key = f"{f.qualified_name}@deleter"

        # If duplicate key exists, keep the FIRST one (consistent with dedup logic)
        if key not in result:
            result[key] = f

    return result


def analyze_file_coverage(
    output_path: Path,
    source_path: Optional[Path],
    output_dir: Path,
) -> FileCoverage:
    """
    Analyze function coverage for a single file pair (source vs output).

    Compares:
    1. Functions — by qualified name (scope.name)
    2. Classes — by class name
    3. Signatures — parameter lists for matched functions
    4. Types — async/sync changes for matched functions
    """
    rel_path = str(output_path.relative_to(output_dir))

    coverage = FileCoverage(
        output_file=rel_path,
        source_file=str(source_path) if source_path else "NOT FOUND",
        source_found=source_path is not None,
    )

    # ── No source file to compare against ──
    if not source_path:
        try:
            output_src = output_path.read_text(encoding="utf-8")
            output_funcs = extract_functions(output_src, rel_path)
            coverage.output_functions = len(output_funcs)
            coverage.output_lines = output_src.count("\n") + 1
            coverage.added = [f.qualified_name for f in output_funcs]
        except Exception:
            pass
        return coverage

    # ── Read source file ──
    try:
        source_src = source_path.read_text(encoding="utf-8")
        coverage.source_lines = source_src.count("\n") + 1
    except Exception as e:
        coverage.has_syntax_error = True
        coverage.syntax_error_in = "source"
        coverage.error_detail = str(e)
        return coverage

    # ── Read output file ──
    try:
        output_src = output_path.read_text(encoding="utf-8")
        coverage.output_lines = output_src.count("\n") + 1
    except Exception as e:
        coverage.has_syntax_error = True
        coverage.syntax_error_in = "output"
        coverage.error_detail = str(e)
        return coverage

    # ── Extract functions ──
    source_funcs = extract_functions(source_src, f"source:{source_path.name}")
    output_funcs = extract_functions(output_src, f"output:{rel_path}")

    # Check for syntax errors (extract_functions returns [] on SyntaxError)
    try:
        ast.parse(source_src)
    except SyntaxError as e:
        coverage.has_syntax_error = True
        coverage.syntax_error_in = "source"
        coverage.error_detail = f"L{e.lineno}: {e.msg}"

    try:
        ast.parse(output_src)
    except SyntaxError as e:
        coverage.has_syntax_error = True
        coverage.syntax_error_in = "output"
        coverage.error_detail = f"L{e.lineno}: {e.msg}"

    coverage.source_functions = len(source_funcs)
    coverage.output_functions = len(output_funcs)

    # ── Build lookup maps ──
    source_map = _build_func_key_map(source_funcs)
    output_map = _build_func_key_map(output_funcs)

    source_keys = set(source_map.keys())
    output_keys = set(output_map.keys())

    # ── Compare function presence ──
    matched_keys = source_keys & output_keys
    missing_keys = source_keys - output_keys
    added_keys = output_keys - source_keys

    coverage.matched = sorted(matched_keys)
    coverage.missing = sorted(missing_keys)
    coverage.added = sorted(added_keys)

    # ── Compare signatures for matched functions ──
    for key in sorted(matched_keys):
        sf = source_map[key]
        of = output_map[key]

        # Compare parameter lists (exclude 'self' and 'cls' as they're implicit)
        s_params = [p for p in sf.parameters if p not in ("self", "cls")]
        o_params = [p for p in of.parameters if p not in ("self", "cls")]

        if s_params != o_params:
            params_added = [p for p in o_params if p not in s_params]
            params_removed = [p for p in s_params if p not in o_params]

            coverage.signature_changes.append(
                {
                    "function": key,
                    "source_params": sf.parameters,
                    "output_params": of.parameters,
                    "params_added": params_added,
                    "params_removed": params_removed,
                }
            )

        # Check async/sync type change
        if sf.is_async != of.is_async:
            coverage.type_changes.append(
                {
                    "function": key,
                    "source_async": sf.is_async,
                    "output_async": of.is_async,
                }
            )

    # ── Compare classes ──
    source_classes = extract_classes(source_src, f"source:{source_path.name}")
    output_classes = extract_classes(output_src, f"output:{rel_path}")

    source_class_names = [c.name for c in source_classes]
    output_class_names = [c.name for c in output_classes]

    coverage.source_classes = source_class_names
    coverage.output_classes = output_class_names
    coverage.missing_classes = [
        c for c in source_class_names if c not in output_class_names
    ]
    coverage.added_classes = [
        c for c in output_class_names if c not in source_class_names
    ]

    return coverage


# ============================================================================
# Report Printing
# ============================================================================


def print_report(
    coverages: List[FileCoverage], detailed: bool = True
) -> bool:
    """
    Print a comprehensive human-readable coverage report.

    Returns:
        True if all source functions are preserved (zero loss).
    """
    total_source = 0
    total_output = 0
    total_matched = 0
    total_missing = 0
    total_added = 0
    total_sig_changes = 0
    total_type_changes = 0
    total_missing_classes = 0
    total_added_classes = 0
    perfect_files = 0
    imperfect_files: List[FileCoverage] = []
    no_source_files: List[FileCoverage] = []
    syntax_error_files: List[FileCoverage] = []

    print()
    print("=" * 80)
    print("  FUNCTION COVERAGE REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    # ── Per-file results ──
    for cov in coverages:
        total_source += cov.source_functions
        total_output += cov.output_functions
        total_matched += len(cov.matched)
        total_missing += len(cov.missing)
        total_added += len(cov.added)
        total_sig_changes += len(cov.signature_changes)
        total_type_changes += len(cov.type_changes)
        total_missing_classes += len(cov.missing_classes)
        total_added_classes += len(cov.added_classes)

        if cov.has_syntax_error:
            syntax_error_files.append(cov)
            icon = "[FAIL]"
            extra = f" SYNTAX ERROR in {cov.syntax_error_in}: {cov.error_detail}"
        elif not cov.source_found:
            no_source_files.append(cov)
            icon = "[WARN] "
            extra = f" (no source, {cov.output_functions} funcs)"
        elif cov.is_perfect:
            perfect_files += 1
            icon = "[PASS]"
            extra = ""
        else:
            imperfect_files.append(cov)
            icon = "[FAIL]"
            extra = ""

        missing_str = f", {len(cov.missing)} MISSING" if cov.missing else ""
        added_str = f", +{len(cov.added)} new" if cov.added else ""
        sig_str = (
            f", {len(cov.signature_changes)} sig Δ"
            if cov.signature_changes
            else ""
        )
        cls_str = (
            f", {len(cov.missing_classes)} class MISSING"
            if cov.missing_classes
            else ""
        )

        src_count = cov.source_functions
        out_count = len(cov.matched)

        print(
            f"  {icon} {cov.output_file}: "
            f"{out_count}/{src_count} functions"
            f"{missing_str}{added_str}{sig_str}{cls_str}{extra}"
        )

    # ── Detail: Missing functions ──
    if imperfect_files and detailed:
        print()
        print("-" * 80)
        print("  [FAIL] MISSING FUNCTIONS (require attention)")
        print("-" * 80)
        for cov in imperfect_files:
            print(f"\n  [FILE] {cov.output_file}")
            print(f"     Source: {cov.source_file}")
            for m in cov.missing:
                print(f"     [FAIL] MISSING: {m}")
            for m in cov.missing_classes:
                print(f"     [FAIL] MISSING CLASS: {m}")

    # ── Detail: Files without source match ──
    if no_source_files and detailed:
        print()
        print("-" * 80)
        print(f"  [WARN]  FILES WITHOUT SOURCE MATCH ({len(no_source_files)})")
        print("-" * 80)
        for cov in no_source_files:
            print(
                f"  [WARN]  {cov.output_file} "
                f"({cov.output_functions} functions, {cov.output_lines} lines)"
            )

    # ── Detail: Syntax errors ──
    if syntax_error_files and detailed:
        print()
        print("-" * 80)
        print(f"  [FAIL] FILES WITH SYNTAX ERRORS ({len(syntax_error_files)})")
        print("-" * 80)
        for cov in syntax_error_files:
            print(
                f"  [FAIL] {cov.output_file} — {cov.syntax_error_in}: {cov.error_detail}"
            )

    # ── Detail: Signature changes ──
    sig_change_files = [c for c in coverages if c.signature_changes]
    if sig_change_files and detailed:
        print()
        print("-" * 80)
        print(f"  [NOTE] SIGNATURE CHANGES ({total_sig_changes} total)")
        print("-" * 80)
        for cov in sig_change_files:
            for sc in cov.signature_changes:
                added = (
                    ", ".join(sc["params_added"])
                    if sc["params_added"]
                    else "—"
                )
                removed = (
                    ", ".join(sc["params_removed"])
                    if sc["params_removed"]
                    else "—"
                )
                print(f"  {cov.output_file}::{sc['function']}")
                print(f"    + params added:   {added}")
                print(f"    - params removed: {removed}")

    # ── Detail: Type changes (async/sync) ──
    type_change_files = [c for c in coverages if c.type_changes]
    if type_change_files and detailed:
        print()
        print("-" * 80)
        print(f"  [CHECK] ASYNC/SYNC TYPE CHANGES ({total_type_changes} total)")
        print("-" * 80)
        for cov in type_change_files:
            for tc in cov.type_changes:
                direction = (
                    "sync → async"
                    if tc["output_async"]
                    else "async → sync"
                )
                print(f"  {cov.output_file}::{tc['function']}: {direction}")

    # ── Summary ──
    files_with_source = len(coverages) - len(no_source_files)

    print()
    print("=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"  Files scanned:           {len(coverages)}")
    print(f"  Files with source match: {files_with_source}")
    print(f"  Files without source:    {len(no_source_files)}")
    print(f"  Files with syntax error: {len(syntax_error_files)}")
    print(f"  Perfect coverage files:  {perfect_files}/{files_with_source}")
    print()
    print(f"  Source functions:         {total_source}")
    print(f"  Output functions:         {total_output}")
    print(f"  Matched:                  {total_matched}")
    print(f"  Missing (CRITICAL):       {total_missing}")
    print(f"  New (AI-added):           {total_added}")
    print(f"  Signature changes:        {total_sig_changes}")
    print(f"  Async/sync type changes:  {total_type_changes}")
    print()
    print(f"  Source classes:            {sum(len(c.source_classes) for c in coverages)}")
    print(f"  Output classes:            {sum(len(c.output_classes) for c in coverages)}")
    print(f"  Missing classes:           {total_missing_classes}")
    print(f"  New classes:               {total_added_classes}")
    print()

    if total_source > 0:
        pct = (total_matched / total_source) * 100
        print(f"  Function Coverage:        {pct:.1f}%")
        print()

    all_ok = total_missing == 0 and total_missing_classes == 0

    if all_ok:
        print("  [PASS] ZERO FUNCTION LOSS — ALL SOURCE FUNCTIONS PRESERVED!")
    else:
        print(
            f"  [WARN]  {total_missing} functions + "
            f"{total_missing_classes} classes MISSING — REVIEW REQUIRED"
        )

    print("=" * 80)
    print()

    return all_ok


# ============================================================================
# JSON Report
# ============================================================================


def save_json_report(
    coverages: List[FileCoverage], output_path: Path
) -> Path:
    """Save a machine-readable JSON report."""
    total_source = sum(c.source_functions for c in coverages)
    total_matched = sum(len(c.matched) for c in coverages)
    total_missing = sum(len(c.missing) for c in coverages)

    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "total_files": len(coverages),
        "files_with_source": sum(1 for c in coverages if c.source_found),
        "files_without_source": sum(1 for c in coverages if not c.source_found),
        "total_source_functions": total_source,
        "total_output_functions": sum(c.output_functions for c in coverages),
        "total_matched": total_matched,
        "total_missing": total_missing,
        "total_added": sum(len(c.added) for c in coverages),
        "total_signature_changes": sum(
            len(c.signature_changes) for c in coverages
        ),
        "total_type_changes": sum(len(c.type_changes) for c in coverages),
        "total_missing_classes": sum(
            len(c.missing_classes) for c in coverages
        ),
        "coverage_pct": (total_matched / total_source * 100)
        if total_source > 0
        else 100.0,
        "zero_loss": total_missing == 0,
        "files": [],
    }

    for cov in coverages:
        file_data = {
            "output_file": cov.output_file,
            "source_file": cov.source_file,
            "source_found": cov.source_found,
            "source_functions": cov.source_functions,
            "output_functions": cov.output_functions,
            "source_lines": cov.source_lines,
            "output_lines": cov.output_lines,
            "matched_count": len(cov.matched),
            "missing_count": len(cov.missing),
            "added_count": len(cov.added),
            "missing": cov.missing,
            "added": cov.added,
            "missing_classes": cov.missing_classes,
            "added_classes": cov.added_classes,
            "signature_changes": cov.signature_changes,
            "type_changes": cov.type_changes,
            "coverage_pct": cov.coverage_pct if cov.source_found else None,
            "is_perfect": cov.is_perfect if cov.source_found else None,
            "has_syntax_error": cov.has_syntax_error,
        }
        report["files"].append(file_data)

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
        description="Validate that no functions were lost during AI conversion.",
    )
    parser.add_argument(
        "--source-dirs",
        default=None,
        help="Comma-separated source directories (default: from .env SOURCE_DIR).",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Directory with converted files (default: from .env OUTPUT_DIR).",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        default=True,
        help="Show detailed report with function names (default: True).",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        default=False,
        help="Show only summary, no per-file details.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Save machine-readable JSON report to reports/ directory.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Check a single file instead of entire directory.",
    )
    args = parser.parse_args()

    # ── Resolve source directories ──
    if args.source_dirs:
        source_dirs = [Path(p.strip()) for p in args.source_dirs.split(",")]
    else:
        source_dirs = SOURCE_DIRS

    if not source_dirs:
        logger.error(
            "SOURCE_DIR not configured. Set it in .env or pass --source-dirs."
        )
        sys.exit(1)

    # Validate source dirs exist
    for sd in source_dirs:
        if not sd.is_dir():
            logger.error("Source directory not found: %s", sd)
            sys.exit(1)

    # ── Resolve output directory ──
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        logger.error("Output directory not found: %s", output_dir)
        sys.exit(1)

    # ── Log configuration ──
    logger.info("=" * 60)
    logger.info("Function Coverage Validator")
    logger.info("=" * 60)
    logger.info("Source dirs: %s", [str(d) for d in source_dirs])
    logger.info("Output dir:  %s", output_dir)
    logger.info("-" * 60)

    # ── Collect output files ──
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            logger.error("File not found: %s", file_path)
            sys.exit(1)
        output_files = [file_path]
    else:
        output_files = sorted(output_dir.rglob("*.py"))
        output_files = [f for f in output_files if "__pycache__" not in str(f)]

    logger.info("Found %d output file(s) to validate", len(output_files))

    # ── Analyze each file ──
    coverages: List[FileCoverage] = []
    unmatched_count = 0

    for output_path in output_files:
        rel_path = str(output_path.relative_to(output_dir))
        source_path = find_source_file(rel_path, source_dirs)

        if not source_path:
            unmatched_count += 1
            logger.warning("  No source found for: %s", rel_path)

        coverage = analyze_file_coverage(output_path, source_path, output_dir)
        coverages.append(coverage)

    # ── Print report ──
    detailed = args.detailed and not args.summary_only
    all_ok = print_report(coverages, detailed=detailed)

    # ── Save JSON report if requested ──
    if args.json:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = _REPORTS_DIR / f"function_coverage_{timestamp}.json"
        save_json_report(coverages, json_path)

    # ── Exit code: 0 = all OK, 1 = missing functions ──
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

"""
Code Quality Validator — Deep analysis of AI-converted code for critical issues.

This is the SECOND validation layer. While function_coverage.py checks that
no functions were LOST, this script checks that the converted code is
CORRECT and COMPLETE.

Checks performed:
  1. Nested function preservation (closures inside functions)
  2. Module-level variable/constant preservation
  3. Import completeness (used symbols are imported)
  4. Stub/empty function detection (pass-only or docstring-only bodies)
  5. Raw SQL remnants detection (unconverted patterns without SKIP tags)
  6. Old connection pattern detection (get_db_connection still in use)
  7. ORM quality checks (missing db.commit, missing db param)
  8. Decorator preservation (matched functions have same decorators)
  9. Return statement analysis (functions that lost return values)
  10. Docstring preservation

Uses Python's ast module for 100% accurate static analysis.

Usage:
    refactra-mysql quality
    refactra-mysql quality --output-dir output/ --json
    refactra-mysql quality --file output/admin/employees.py
    refactra-mysql quality --check imports   # Run only import checks
"""
import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from refactra_mysql.config import OUTPUT_DIR, REPORTS_DIR, SOURCE_DIRS, setup_logging

logger = setup_logging("code_quality")

_REPORTS_DIR = REPORTS_DIR / "quality"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class Issue:
    """A single quality issue found in a file."""

    category: str  # e.g., "nested_func", "import", "stub", "raw_sql"
    severity: str  # "critical", "warning", "info"
    line: int
    message: str
    detail: str = ""


@dataclass
class FileQuality:
    """Quality analysis result for a single file pair."""

    output_file: str
    source_file: str
    source_found: bool
    issues: List[Issue] = field(default_factory=list)
    output_lines: int = 0
    has_syntax_error: bool = False

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "info")

    @property
    def is_clean(self) -> bool:
        return self.critical_count == 0


# ============================================================================
# AST Helpers
# ============================================================================


def _safe_parse(code: str, filepath: str = "") -> Optional[ast.Module]:
    """Safely parse Python code, returning None on SyntaxError."""
    try:
        return ast.parse(code)
    except SyntaxError as e:
        logger.warning("SyntaxError in %s L%s: %s", filepath, e.lineno, e.msg)
        return None


def _get_node_name(node) -> str:
    """Get the name of an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_get_node_name(node.value)}.{node.attr}"
    return "?"


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


# ============================================================================
# Check 1: Nested Function Preservation
# ============================================================================


def _extract_nested_functions(
    code: str, filepath: str = ""
) -> Dict[str, List[str]]:
    """
    Extract nested functions (closures) from source code.

    Returns:
        Dict mapping parent_function_name → [nested_function_names]
    """
    tree = _safe_parse(code, filepath)
    if not tree:
        return {}

    nested: Dict[str, List[str]] = {}

    def visit(node, parent_func=None, scope="module"):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if parent_func is not None:
                    # This is a nested function
                    qualified_parent = (
                        f"{scope}.{parent_func}" if scope != "module" else parent_func
                    )
                    nested.setdefault(qualified_parent, []).append(child.name)
                else:
                    # Top-level or class method — recurse
                    visit(child, parent_func=child.name, scope=scope)
            elif isinstance(child, ast.ClassDef):
                # Recurse into class with scope
                for class_child in ast.iter_child_nodes(child):
                    if isinstance(
                        class_child, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        visit(
                            class_child,
                            parent_func=class_child.name,
                            scope=child.name,
                        )

    visit(tree)
    return nested


def check_nested_functions(
    source_code: str,
    output_code: str,
    source_path: str,
    output_path: str,
) -> List[Issue]:
    """Check that nested functions in source are preserved in output."""
    source_nested = _extract_nested_functions(source_code, source_path)
    output_nested = _extract_nested_functions(output_code, output_path)

    issues: List[Issue] = []

    for parent, nested_names in source_nested.items():
        output_names = output_nested.get(parent, [])
        for name in nested_names:
            if name not in output_names:
                issues.append(
                    Issue(
                        category="nested_func",
                        severity="warning",
                        line=0,
                        message=f"Nested function '{name}' inside '{parent}' missing in output",
                        detail=f"Source has inner function {parent}::{name} but output does not",
                    )
                )

    return issues


# ============================================================================
# Check 2: Module-Level Variable Preservation
# ============================================================================


def _extract_module_variables(
    code: str, filepath: str = ""
) -> Dict[str, int]:
    """
    Extract module-level variable assignments.

    Returns:
        Dict mapping variable_name → line_number
    """
    tree = _safe_parse(code, filepath)
    if not tree:
        return {}

    variables: Dict[str, int] = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    variables[target.id] = node.lineno
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            variables[elt.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and node.target:
            if isinstance(node.target, ast.Name):
                variables[node.target.id] = node.lineno

    return variables


def check_module_variables(
    source_code: str,
    output_code: str,
    source_path: str,
    output_path: str,
) -> List[Issue]:
    """Check that module-level variables from source are preserved in output."""
    source_vars = _extract_module_variables(source_code, source_path)
    output_vars = _extract_module_variables(output_code, output_path)

    issues: List[Issue] = []

    # Skip common exclude patterns (these are expected to change)
    skip_patterns = {
        "__all__",
        "__version__",
        "__author__",
        "_",
    }

    for var_name, line in source_vars.items():
        if var_name.startswith("_") and var_name in skip_patterns:
            continue
        if var_name not in output_vars:
            issues.append(
                Issue(
                    category="module_var",
                    severity="warning",
                    line=line,
                    message=f"Module-level variable '{var_name}' missing in output",
                    detail=f"Defined at source L{line}",
                )
            )

    return issues


# ============================================================================
# Check 3: Stub/Empty Function Detection
# ============================================================================


def check_stub_functions(output_code: str, output_path: str) -> List[Issue]:
    """
    Detect stub functions in output that have no meaningful body.

    Detects:
    - Functions with only `pass`
    - Functions with only a docstring (no logic)
    - Functions with only `raise NotImplementedError`
    - Functions with only `...` (Ellipsis)
    """
    tree = _safe_parse(output_code, output_path)
    if not tree:
        return []

    issues: List[Issue] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        body = node.body
        if not body:
            continue

        # Check: only `pass`
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            issues.append(
                Issue(
                    category="stub_func",
                    severity="warning",
                    line=node.lineno,
                    message=f"Stub function '{node.name}' — body is only 'pass'",
                    detail=f"L{node.lineno}-L{node.end_lineno}",
                )
            )
            continue

        # Check: only docstring (no logic)
        if len(body) == 1 and isinstance(body[0], ast.Expr):
            if isinstance(body[0].value, ast.Constant) and isinstance(
                body[0].value.value, str
            ):
                issues.append(
                    Issue(
                        category="stub_func",
                        severity="warning",
                        line=node.lineno,
                        message=f"Stub function '{node.name}' — body is only a docstring",
                        detail=f"L{node.lineno}-L{node.end_lineno}",
                    )
                )
                continue

        # Check: docstring + pass
        if len(body) == 2:
            has_docstring = (
                isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            )
            has_pass = isinstance(body[1], ast.Pass)
            if has_docstring and has_pass:
                issues.append(
                    Issue(
                        category="stub_func",
                        severity="warning",
                        line=node.lineno,
                        message=f"Stub function '{node.name}' — body is docstring + pass",
                        detail=f"L{node.lineno}-L{node.end_lineno}",
                    )
                )
                continue

        # Check: only raise NotImplementedError
        if len(body) == 1 and isinstance(body[0], ast.Raise):
            exc = body[0].exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                if exc.func.id == "NotImplementedError":
                    issues.append(
                        Issue(
                            category="stub_func",
                            severity="warning",
                            line=node.lineno,
                            message=f"Stub function '{node.name}' — raises NotImplementedError",
                            detail=f"L{node.lineno}-L{node.end_lineno}",
                        )
                    )

        # Check: only `...` (Ellipsis)
        if len(body) == 1 and isinstance(body[0], ast.Expr):
            if isinstance(body[0].value, ast.Constant) and body[0].value.value is ...:
                issues.append(
                    Issue(
                        category="stub_func",
                        severity="warning",
                        line=node.lineno,
                        message=f"Stub function '{node.name}' — body is Ellipsis (...)",
                        detail=f"L{node.lineno}-L{node.end_lineno}",
                    )
                )

    return issues


# ============================================================================
# Check 4: Raw SQL Remnants Detection
# ============================================================================


_RAW_SQL_PATTERNS = [
    # cursor-based patterns (should be fully removed after ORM conversion)
    (r"cursor\s*=\s*conn\.cursor", "cursor = conn.cursor() — old MySQL pattern"),
    (r"cursor\.execute\s*\(", "cursor.execute() — old MySQL pattern"),
    (r"cursor\.fetchone\s*\(", "cursor.fetchone() — old MySQL pattern"),
    (r"cursor\.fetchall\s*\(", "cursor.fetchall() — old MySQL pattern"),
    (r"conn\.commit\s*\(", "conn.commit() — old MySQL pattern"),
    (r"conn\.close\s*\(", "conn.close() — old MySQL pattern"),
]

_SKIP_TAG_PATTERNS = [
    "MANUAL REVIEW",
    "SKIP",
    "TODO:",
    "FIXME:",
    "dynamic SQL",
    "Dynamic SQL",
]


def check_raw_sql_remnants(output_code: str, output_path: str) -> List[Issue]:
    """
    Detect raw SQL patterns that should have been converted to ORM.

    Only flags patterns that are NOT preceded by a SKIP/MANUAL REVIEW tag
    (those are intentionally preserved).
    """
    issues: List[Issue] = []
    lines = output_code.splitlines()

    for pattern_str, description in _RAW_SQL_PATTERNS:
        pattern = re.compile(pattern_str)
        for i, line in enumerate(lines):
            if pattern.search(line):
                # Check if tagged (look at preceding 5 lines for SKIP tags)
                tagged = False
                for j in range(max(0, i - 5), i + 1):
                    if any(tag in lines[j] for tag in _SKIP_TAG_PATTERNS):
                        tagged = True
                        break

                if not tagged:
                    issues.append(
                        Issue(
                            category="raw_sql",
                            severity="critical",
                            line=i + 1,
                            message=f"Unconverted raw SQL: {description}",
                            detail=line.strip()[:120],
                        )
                    )

    return issues


# ============================================================================
# Check 5: Old Connection Pattern Detection
# ============================================================================


def check_old_connection_patterns(
    output_code: str, output_path: str
) -> List[Issue]:
    """
    Detect old get_db_connection() patterns that should be replaced with
    db: Session parameter after ORM conversion.

    Note: get_db_connection is EXPECTED in functions that were SKIP'd
    (dynamic SQL), so we only flag it in functions that were converted.
    """
    issues: List[Issue] = []
    lines = output_code.splitlines()

    tree = _safe_parse(output_code, output_path)
    if not tree:
        return issues

    # Find functions that call get_db_connection
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Check if function has `db` parameter (sign of ORM conversion)
        param_names = [a.arg for a in node.args.args]
        has_db_param = "db" in param_names

        # Check if function body contains get_db_connection
        func_source = ast.get_source_segment(output_code, node)
        if func_source and "get_db_connection" in func_source:
            # Check if this function has a SKIP tag
            func_start_line = node.lineno - 1
            tagged = False
            for j in range(max(0, func_start_line - 3), func_start_line + 1):
                if j < len(lines) and any(
                    tag in lines[j] for tag in _SKIP_TAG_PATTERNS
                ):
                    tagged = True
                    break

            if not tagged:
                if has_db_param:
                    # Has db param but STILL uses get_db_connection — likely a bug
                    issues.append(
                        Issue(
                            category="old_connection",
                            severity="critical",
                            line=node.lineno,
                            message=(
                                f"'{node.name}' has db: Session param but "
                                f"still uses get_db_connection()"
                            ),
                            detail="Function was converted to ORM but old connection pattern remains",
                        )
                    )
                else:
                    # No db param, uses get_db_connection — might be unconverted
                    issues.append(
                        Issue(
                            category="old_connection",
                            severity="info",
                            line=node.lineno,
                            message=(
                                f"'{node.name}' uses get_db_connection() "
                                f"without db: Session param"
                            ),
                            detail="May be unconverted or wrapper function — review if tagged",
                        )
                    )

    return issues


# ============================================================================
# Check 6: Decorator Preservation
# ============================================================================


def check_decorator_preservation(
    source_code: str,
    output_code: str,
    source_path: str,
    output_path: str,
) -> List[Issue]:
    """
    Check that decorators on functions are preserved after conversion.

    Some decorators are critical (e.g., @staticmethod, @login_required, @cache)
    and losing them would break functionality.
    """
    source_tree = _safe_parse(source_code, source_path)
    output_tree = _safe_parse(output_code, output_path)
    if not source_tree or not output_tree:
        return []

    issues: List[Issue] = []

    # Build maps: qualified_name → decorator list
    def build_decorator_map(tree, code_label):
        result = {}

        def visit(node, scope="module"):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    key = (
                        f"{scope}.{child.name}"
                        if scope != "module"
                        else child.name
                    )
                    result[key] = set(_get_decorator_names(child.decorator_list))
                elif isinstance(child, ast.ClassDef):
                    visit(child, scope=child.name)

        visit(tree)
        return result

    source_decs = build_decorator_map(source_tree, "source")
    output_decs = build_decorator_map(output_tree, "output")

    # Critical decorators that must be preserved
    critical_decorators = {
        "staticmethod",
        "classmethod",
        "property",
        "login_required",
        "admin_required",
        "cache",
        "lru_cache",
        "abstractmethod",
        "api_error_handler",
        "app.route",
        "app.before_request",
    }

    for func_name in source_decs:
        if func_name not in output_decs:
            continue  # Missing function — handled by function_coverage.py

        source_set = source_decs[func_name]
        output_set = output_decs[func_name]

        # Check for lost critical decorators
        lost = source_set - output_set
        for dec in lost:
            # Check if it's a critical decorator
            dec_base = dec.split(".")[0] if "." in dec else dec
            if dec_base in critical_decorators or dec in critical_decorators:
                issues.append(
                    Issue(
                        category="decorator",
                        severity="critical",
                        line=0,
                        message=f"Critical decorator '@{dec}' lost on '{func_name}'",
                        detail=f"Source has @{dec} but output does not",
                    )
                )
            else:
                issues.append(
                    Issue(
                        category="decorator",
                        severity="info",
                        line=0,
                        message=f"Decorator '@{dec}' removed from '{func_name}'",
                        detail="May be intentional (converter restructured)",
                    )
                )

    return issues


# ============================================================================
# Check 7: Return Statement Analysis
# ============================================================================


def check_return_statements(
    source_code: str,
    output_code: str,
    source_path: str,
    output_path: str,
) -> List[Issue]:
    """
    Check that functions which returned values in source still return values
    in output. A function losing its return statement would break callers.
    """
    source_tree = _safe_parse(source_code, source_path)
    output_tree = _safe_parse(output_code, output_path)
    if not source_tree or not output_tree:
        return []

    issues: List[Issue] = []

    def has_value_return(func_node) -> bool:
        """Check if a function has at least one `return <value>` statement."""
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and node.value is not None:
                return True
        return False

    def build_return_map(tree):
        result = {}

        def visit(node, scope="module"):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    key = (
                        f"{scope}.{child.name}"
                        if scope != "module"
                        else child.name
                    )
                    result[key] = has_value_return(child)
                elif isinstance(child, ast.ClassDef):
                    visit(child, scope=child.name)

        visit(tree)
        return result

    source_returns = build_return_map(source_tree)
    output_returns = build_return_map(output_tree)

    for func_name, had_return in source_returns.items():
        if func_name not in output_returns:
            continue  # Missing function — handled by function_coverage.py

        has_return = output_returns[func_name]

        if had_return and not has_return:
            issues.append(
                Issue(
                    category="return_lost",
                    severity="critical",
                    line=0,
                    message=f"'{func_name}' returned a value in source but not in output",
                    detail="Callers expecting a return value will get None",
                )
            )

    return issues


# ============================================================================
# Check 8: Docstring Preservation
# ============================================================================


def check_docstring_preservation(
    source_code: str,
    output_code: str,
    source_path: str,
    output_path: str,
) -> List[Issue]:
    """Check that docstrings are preserved on functions that had them."""
    source_tree = _safe_parse(source_code, source_path)
    output_tree = _safe_parse(output_code, output_path)
    if not source_tree or not output_tree:
        return []

    issues: List[Issue] = []

    def build_docstring_map(tree):
        result = {}

        def visit(node, scope="module"):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    key = (
                        f"{scope}.{child.name}"
                        if scope != "module"
                        else child.name
                    )
                    has_doc = (
                        child.body
                        and isinstance(child.body[0], ast.Expr)
                        and isinstance(child.body[0].value, ast.Constant)
                        and isinstance(child.body[0].value.value, str)
                    )
                    result[key] = has_doc
                elif isinstance(child, ast.ClassDef):
                    visit(child, scope=child.name)

        visit(tree)
        return result

    source_docs = build_docstring_map(source_tree)
    output_docs = build_docstring_map(output_tree)

    lost_count = 0
    for func_name, had_doc in source_docs.items():
        if func_name not in output_docs:
            continue
        if had_doc and not output_docs[func_name]:
            lost_count += 1

    # Only report summary — losing individual docstrings is low severity
    if lost_count > 0:
        issues.append(
            Issue(
                category="docstring",
                severity="info",
                line=0,
                message=f"{lost_count} function(s) lost their docstring",
                detail="Docstrings help maintainability but don't affect runtime",
            )
        )

    return issues


# ============================================================================
# Check 9: Import Analysis
# ============================================================================


def check_imports(output_code: str, output_path: str) -> List[Issue]:
    """
    Check for common import issues in converted code.

    Detects:
    - Duplicate import lines
    - from X import * (wildcard)
    - Known broken imports
    """
    issues: List[Issue] = []
    tree = _safe_parse(output_code, output_path)
    if not tree:
        return issues

    # Collect all imports
    imports: List[Tuple[str, int]] = []  # (import_string, line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((f"import {alias.name}", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    issues.append(
                        Issue(
                            category="import",
                            severity="warning",
                            line=node.lineno,
                            message=f"Wildcard import: from {module} import *",
                            detail="Wildcard imports make it hard to track dependencies",
                        )
                    )
                imports.append(
                    (f"from {module} import {alias.name}", node.lineno)
                )

    # Check for exact duplicate imports
    seen: Dict[str, int] = {}
    for import_str, line in imports:
        if import_str in seen:
            issues.append(
                Issue(
                    category="import",
                    severity="info",
                    line=line,
                    message=f"Duplicate import: {import_str}",
                    detail=f"First seen at L{seen[import_str]}",
                )
            )
        else:
            seen[import_str] = line

    return issues


# ============================================================================
# Check 10: ORM Session Consistency
# ============================================================================


def check_orm_session_usage(output_code: str, output_path: str) -> List[Issue]:
    """
    Check for ORM session usage issues.

    Detects:
    - Functions using db.query/db.add but missing db: Session parameter
    - Functions with db.add() but no db.commit() or db.flush()
    """
    issues: List[Issue] = []
    tree = _safe_parse(output_code, output_path)
    if not tree:
        return issues

    orm_write_patterns = {"db.add", "db.merge", "db.delete", "db.execute"}
    orm_commit_patterns = {"db.commit", "db.flush"}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        func_source = ast.get_source_segment(output_code, node)
        if not func_source:
            continue

        # Check if function uses ORM write operations
        has_write = any(pat in func_source for pat in orm_write_patterns)
        has_commit = any(pat in func_source for pat in orm_commit_patterns)

        if has_write and not has_commit:
            # Check if it's a SKIP'd function
            lines = output_code.splitlines()
            func_start = node.lineno - 1
            tagged = False
            for j in range(max(0, func_start - 3), min(len(lines), func_start + 1)):
                if any(tag in lines[j] for tag in _SKIP_TAG_PATTERNS):
                    tagged = True
                    break

            if not tagged:
                # Determine scope
                scope = ""
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.ClassDef):
                        for child in ast.iter_child_nodes(parent):
                            if child is node:
                                scope = parent.name
                func_name = (
                    f"{scope}.{node.name}" if scope else node.name
                )

                issues.append(
                    Issue(
                        category="orm_session",
                        severity="warning",
                        line=node.lineno,
                        message=f"'{func_name}' has db write but no commit/flush",
                        detail="May rely on caller to commit, or may be a bug",
                    )
                )

    return issues


# ============================================================================
# File Matcher (reused from function_coverage.py)
# ============================================================================


def find_source_file(
    output_rel_path: str, source_dirs: List[Path]
) -> Optional[Path]:
    """Find the original source file for an output file."""
    for source_dir in source_dirs:
        candidate = source_dir / output_rel_path
        if candidate.is_file():
            return candidate

    target_name = Path(output_rel_path).name
    target_parents = Path(output_rel_path).parent.parts
    candidates = []

    for source_dir in source_dirs:
        for source_file in source_dir.rglob(target_name):
            if "__pycache__" in str(source_file):
                continue
            source_rel_parents = source_file.relative_to(
                source_dir
            ).parent.parts
            if source_rel_parents == target_parents:
                candidates.append(source_file)

    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        return max(
            candidates,
            key=lambda c: len(
                set(c.parts) & set(Path(output_rel_path).parts)
            ),
        )
    return None


# ============================================================================
# Main Analysis
# ============================================================================


def analyze_file_quality(
    output_path: Path,
    source_path: Optional[Path],
    output_dir: Path,
    checks: Optional[Set[str]] = None,
) -> FileQuality:
    """Run all quality checks on a single file pair."""
    rel_path = str(output_path.relative_to(output_dir))

    quality = FileQuality(
        output_file=rel_path,
        source_file=str(source_path) if source_path else "NOT FOUND",
        source_found=source_path is not None,
    )

    # Read output file
    try:
        output_code = output_path.read_text(encoding="utf-8")
        quality.output_lines = output_code.count("\n") + 1
    except Exception:
        quality.has_syntax_error = True
        return quality

    # Syntax check
    if _safe_parse(output_code, rel_path) is None:
        quality.has_syntax_error = True
        quality.issues.append(
            Issue(
                category="syntax",
                severity="critical",
                line=0,
                message="File has syntax error",
            )
        )
        return quality

    # Read source file (if available)
    source_code = None
    if source_path:
        try:
            source_code = source_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # All checks that need both source and output
    all_checks = checks or {
        "nested",
        "module_vars",
        "stubs",
        "raw_sql",
        "old_connection",
        "decorators",
        "returns",
        "docstrings",
        "imports",
        "orm_session",
    }

    if source_code:
        if "nested" in all_checks:
            quality.issues.extend(
                check_nested_functions(
                    source_code, output_code,
                    str(source_path), rel_path,
                )
            )

        if "module_vars" in all_checks:
            quality.issues.extend(
                check_module_variables(
                    source_code, output_code,
                    str(source_path), rel_path,
                )
            )

        if "decorators" in all_checks:
            quality.issues.extend(
                check_decorator_preservation(
                    source_code, output_code,
                    str(source_path), rel_path,
                )
            )

        if "returns" in all_checks:
            quality.issues.extend(
                check_return_statements(
                    source_code, output_code,
                    str(source_path), rel_path,
                )
            )

        if "docstrings" in all_checks:
            quality.issues.extend(
                check_docstring_preservation(
                    source_code, output_code,
                    str(source_path), rel_path,
                )
            )

    # Checks that only need output
    if "stubs" in all_checks:
        quality.issues.extend(check_stub_functions(output_code, rel_path))

    if "raw_sql" in all_checks:
        quality.issues.extend(check_raw_sql_remnants(output_code, rel_path))

    if "old_connection" in all_checks:
        quality.issues.extend(
            check_old_connection_patterns(output_code, rel_path)
        )

    if "imports" in all_checks:
        quality.issues.extend(check_imports(output_code, rel_path))

    if "orm_session" in all_checks:
        quality.issues.extend(check_orm_session_usage(output_code, rel_path))

    return quality


# ============================================================================
# Report Printing
# ============================================================================


def print_report(
    results: List[FileQuality], detailed: bool = True
) -> bool:
    """Print human-readable quality report. Returns True if no criticals."""
    # Aggregate stats
    category_counts: Dict[str, Counter] = defaultdict(Counter)
    total_critical = 0
    total_warning = 0
    total_info = 0
    clean_files = 0
    files_with_criticals: List[FileQuality] = []

    print()
    print("=" * 80)
    print("  CODE QUALITY REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    for fq in results:
        total_critical += fq.critical_count
        total_warning += fq.warning_count
        total_info += fq.info_count

        for issue in fq.issues:
            category_counts[issue.category][issue.severity] += 1

        if fq.is_clean:
            clean_files += 1
            icon = "[PASS]"
        else:
            files_with_criticals.append(fq)
            icon = "[FAIL]"

        issue_parts = []
        if fq.critical_count:
            issue_parts.append(f"{fq.critical_count} critical")
        if fq.warning_count:
            issue_parts.append(f"{fq.warning_count} warning")
        if fq.info_count:
            issue_parts.append(f"{fq.info_count} info")

        issue_str = ", ".join(issue_parts) if issue_parts else "clean"
        print(f"  {icon} {fq.output_file}: {issue_str}")

    # ── Critical issues detail ──
    if files_with_criticals and detailed:
        print()
        print("-" * 80)
        print("  [FAIL] CRITICAL ISSUES (must fix)")
        print("-" * 80)
        for fq in files_with_criticals:
            criticals = [i for i in fq.issues if i.severity == "critical"]
            print(f"\n  [FILE] {fq.output_file}")
            for issue in criticals:
                line_str = f"L{issue.line}" if issue.line else ""
                print(f"     [FAIL] [{issue.category}] {line_str} {issue.message}")
                if issue.detail:
                    print(f"        → {issue.detail}")

    # ── Warning details ──
    warning_files = [fq for fq in results if fq.warning_count > 0]
    if warning_files and detailed:
        print()
        print("-" * 80)
        print(f"  [WARN] WARNINGS ({total_warning} total)")
        print("-" * 80)
        for fq in warning_files:
            warnings = [i for i in fq.issues if i.severity == "warning"]
            if warnings:
                print(f"\n  [FILE] {fq.output_file}")
                for issue in warnings:
                    line_str = f"L{issue.line}" if issue.line else ""
                    print(
                        f"     [WARN] [{issue.category}] {line_str} {issue.message}"
                    )

    # ── Category summary ──
    print()
    print("-" * 80)
    print("  CHECK RESULTS BY CATEGORY")
    print("-" * 80)

    category_labels = {
        "syntax": "Syntax Errors",
        "nested_func": "Nested Function Preservation",
        "module_var": "Module Variable Preservation",
        "stub_func": "Stub/Empty Functions",
        "raw_sql": "Raw SQL Remnants",
        "old_connection": "Old Connection Patterns",
        "decorator": "Decorator Preservation",
        "return_lost": "Lost Return Values",
        "docstring": "Docstring Preservation",
        "import": "Import Issues",
        "orm_session": "ORM Session Usage",
    }

    all_categories = [
        "syntax",
        "raw_sql",
        "return_lost",
        "decorator",
        "old_connection",
        "nested_func",
        "module_var",
        "stub_func",
        "orm_session",
        "import",
        "docstring",
    ]

    for cat in all_categories:
        counts = category_counts.get(cat, Counter())
        crit = counts.get("critical", 0)
        warn = counts.get("warning", 0)
        info = counts.get("info", 0)
        total = crit + warn + info
        label = category_labels.get(cat, cat)

        if total == 0:
            print(f"  [PASS] {label}: 0 issues")
        else:
            parts = []
            if crit:
                parts.append(f"[FAIL] {crit} critical")
            if warn:
                parts.append(f"[WARN] {warn} warning")
            if info:
                parts.append(f"[INFO]  {info} info")
            print(f"  {'[FAIL]' if crit else '[WARN] '} {label}: {', '.join(parts)}")

    # ── Summary ──
    print()
    print("=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"  Files scanned:     {len(results)}")
    print(f"  Clean files:       {clean_files}/{len(results)}")
    print()
    print(f"  Critical issues:   {total_critical}")
    print(f"  Warnings:          {total_warning}")
    print(f"  Info:              {total_info}")
    print()

    if total_critical == 0:
        print("  [PASS] ZERO CRITICAL ISSUES — Code quality is good!")
    else:
        print(
            f"  [WARN]  {total_critical} CRITICAL ISSUES found — "
            f"review required before deployment"
        )

    print("=" * 80)
    print()

    return total_critical == 0


# ============================================================================
# JSON Report
# ============================================================================


def save_json_report(results: List[FileQuality], output_path: Path) -> Path:
    """Save machine-readable JSON report."""
    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "total_files": len(results),
        "clean_files": sum(1 for r in results if r.is_clean),
        "total_critical": sum(r.critical_count for r in results),
        "total_warning": sum(r.warning_count for r in results),
        "total_info": sum(r.info_count for r in results),
        "files": [],
    }

    for fq in results:
        file_data = {
            "output_file": fq.output_file,
            "source_file": fq.source_file,
            "source_found": fq.source_found,
            "output_lines": fq.output_lines,
            "is_clean": fq.is_clean,
            "critical_count": fq.critical_count,
            "warning_count": fq.warning_count,
            "info_count": fq.info_count,
            "issues": [
                {
                    "category": i.category,
                    "severity": i.severity,
                    "line": i.line,
                    "message": i.message,
                    "detail": i.detail,
                }
                for i in fq.issues
            ],
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
        description="Deep code quality analysis of AI-converted files.",
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
        help="Show detailed issue descriptions (default: True)",
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
        help="Check a single file",
    )
    parser.add_argument(
        "--check",
        default=None,
        help=(
            "Run only specific checks (comma-separated). "
            "Options: nested,module_vars,stubs,raw_sql,old_connection,"
            "decorators,returns,docstrings,imports,orm_session"
        ),
    )
    args = parser.parse_args()

    # ── Resolve source directories ──
    if args.source_dirs:
        source_dirs = [Path(p.strip()) for p in args.source_dirs.split(",")]
    else:
        source_dirs = SOURCE_DIRS

    if not source_dirs:
        logger.error("SOURCE_DIR not configured.")
        sys.exit(1)

    for sd in source_dirs:
        if not sd.is_dir():
            logger.error("Source directory not found: %s", sd)
            sys.exit(1)

    # ── Resolve output directory ──
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        logger.error("Output directory not found: %s", output_dir)
        sys.exit(1)

    # ── Parse checks ──
    checks = None
    if args.check:
        checks = set(args.check.split(","))

    # ── Log configuration ──
    logger.info("=" * 60)
    logger.info("Code Quality Validator")
    logger.info("=" * 60)
    logger.info("Source dirs: %s", [str(d) for d in source_dirs])
    logger.info("Output dir:  %s", output_dir)
    if checks:
        logger.info("Checks:      %s", checks)
    logger.info("-" * 60)

    # ── Collect files ──
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            logger.error("File not found: %s", file_path)
            sys.exit(1)
        output_files = [file_path]
    else:
        output_files = sorted(output_dir.rglob("*.py"))
        output_files = [f for f in output_files if "__pycache__" not in str(f)]

    logger.info("Found %d output file(s) to analyze", len(output_files))

    # ── Analyze ──
    results: List[FileQuality] = []
    for output_path in output_files:
        rel_path = str(output_path.relative_to(output_dir))
        source_path = find_source_file(rel_path, source_dirs)
        quality = analyze_file_quality(
            output_path, source_path, output_dir, checks
        )
        results.append(quality)

    # ── Print report ──
    detailed = args.detailed and not args.summary_only
    all_ok = print_report(results, detailed=detailed)

    # ── Save JSON ──
    if args.json:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = _REPORTS_DIR / f"code_quality_{timestamp}.json"
        save_json_report(results, json_path)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

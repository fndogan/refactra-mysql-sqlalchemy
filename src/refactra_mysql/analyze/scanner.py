"""
Codebase Scanner — Detects raw SQL patterns in Python files.

Scans a directory of Python files and identifies all raw SQL usage patterns
including cursor.execute calls, connection management, dynamic SQL building,
and categorizes each by conversion difficulty.

Usage:
    refactra-mysql analyze --source-dir ./path/to/queries
    refactra-mysql analyze --source-dir ./path/to/queries --output report.json
"""
import ast
import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# Add parent to path for config import
from refactra_mysql.config import REPORTS_DIR, setup_logging

logger = setup_logging("scanner")

_REPORTS_DIR = REPORTS_DIR / "analyze"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class SQLCall:
    """Represents a single cursor.execute() call found in source code."""
    line_number: int
    sql_type: str  # SELECT, INSERT, UPDATE, DELETE, OTHER
    sql_preview: str  # First 120 chars of the SQL string
    is_dynamic: bool  # True if SQL uses string concatenation or f-strings
    has_join: bool
    has_subquery: bool
    has_case_when: bool
    has_aggregation: bool
    complexity: str  # "simple", "moderate", "complex"


@dataclass
class FunctionInfo:
    """Represents a function containing raw SQL calls."""
    name: str
    line_start: int
    line_end: int
    has_get_db_connection: bool
    has_cursor: bool
    has_manual_close: bool
    has_try_except: bool
    has_dynamic_where: bool  # conditions.append pattern
    has_dynamic_set: bool  # set_parts.append pattern
    has_f_string_sql: bool
    sql_calls: list[SQLCall] = field(default_factory=list)

    @property
    def complexity(self) -> str:
        if self.has_dynamic_where or self.has_f_string_sql:
            return "complex"
        if self.has_try_except and len(self.sql_calls) > 2:
            return "moderate"
        return "simple"


@dataclass
class FileReport:
    """Analysis report for a single Python file."""
    filepath: str
    total_lines: int
    total_functions: int
    functions_with_sql: int
    total_execute_calls: int
    get_db_connection_calls: int
    cursor_close_calls: int
    connection_close_calls: int
    connection_commit_calls: int
    connection_rollback_calls: int
    dynamic_where_count: int
    f_string_sql_count: int
    join_count: int
    case_when_count: int
    on_duplicate_key_count: int
    complexity_distribution: dict = field(default_factory=dict)
    functions: list[FunctionInfo] = field(default_factory=list)

    @property
    def overall_complexity(self) -> str:
        if self.f_string_sql_count > 0 or self.dynamic_where_count > 3:
            return "complex"
        if self.dynamic_where_count > 0 or self.join_count > 5:
            return "moderate"
        return "simple"


# =============================================================================
# Pattern Detection
# =============================================================================

# Compiled regex patterns for performance
_PATTERNS = {
    "get_db_connection": re.compile(r"get_db_connection\s*\("),
    "cursor_execute": re.compile(r"cursor\.execute\s*\("),
    "cursor_fetchall": re.compile(r"cursor\.fetchall\s*\("),
    "cursor_fetchone": re.compile(r"cursor\.fetchone\s*\("),
    "cursor_close": re.compile(r"cursor\.close\s*\("),
    "connection_close": re.compile(r"connection\.close\s*\("),
    "connection_commit": re.compile(r"connection\.commit\s*\("),
    "connection_rollback": re.compile(r"connection\.rollback\s*\("),
    "cursor_lastrowid": re.compile(r"cursor\.lastrowid"),
    "dict_cursor": re.compile(r"cursor\(DictCursor\)"),
    "conditions_append": re.compile(r"conditions\.append\(|where_clauses\.append\(|where_parts\.append\("),
    "set_parts_append": re.compile(r"set_parts\.append\(|set_clauses\.append\("),
    "f_string_sql": re.compile(r'f""".*(?:SELECT|INSERT|UPDATE|DELETE)', re.IGNORECASE),
    "on_duplicate_key": re.compile(r"ON\s+DUPLICATE\s+KEY", re.IGNORECASE),
    "insert_ignore": re.compile(r"INSERT\s+IGNORE", re.IGNORECASE),
}

_SQL_TYPE_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_SUBQUERY_RE = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_CASE_RE = re.compile(r"\bCASE\s+WHEN\b", re.IGNORECASE)
_AGG_RE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT|STRING_AGG)\s*\(", re.IGNORECASE)


def _detect_sql_type(sql_text: str) -> str:
    """Detect the type of SQL statement from its text."""
    match = _SQL_TYPE_RE.search(sql_text)
    return match.group(1).upper() if match else "OTHER"


def _classify_sql_complexity(sql_text: str) -> str:
    """Classify SQL complexity based on its content."""
    score = 0
    if _JOIN_RE.search(sql_text):
        score += 2
    if _SUBQUERY_RE.search(sql_text):
        score += 3
    if _CASE_RE.search(sql_text):
        score += 2
    if _AGG_RE.search(sql_text):
        score += 1

    if score >= 4:
        return "complex"
    if score >= 2:
        return "moderate"
    return "simple"


# =============================================================================
# File Scanner
# =============================================================================

def scan_file(filepath: Path) -> Optional[FileReport]:
    """
    Scan a single Python file for raw SQL patterns.

    Args:
        filepath: Path to the Python file to scan.

    Returns:
        FileReport with analysis results, or None if the file has no SQL usage.
    """
    try:
        content = filepath.read_text(encoding="utf-8")
        lines = content.splitlines()
    except (UnicodeDecodeError, OSError) as e:
        logger.warning("Could not read %s: %s", filepath, e)
        return None

    # Quick check — skip files without any SQL patterns
    if "cursor" not in content and "get_db_connection" not in content:
        return None

    # Count pattern occurrences
    report = FileReport(
        filepath=str(filepath),
        total_lines=len(lines),
        total_functions=0,
        functions_with_sql=0,
        total_execute_calls=len(_PATTERNS["cursor_execute"].findall(content)),
        get_db_connection_calls=len(_PATTERNS["get_db_connection"].findall(content)),
        cursor_close_calls=len(_PATTERNS["cursor_close"].findall(content)),
        connection_close_calls=len(_PATTERNS["connection_close"].findall(content)),
        connection_commit_calls=len(_PATTERNS["connection_commit"].findall(content)),
        connection_rollback_calls=len(_PATTERNS["connection_rollback"].findall(content)),
        dynamic_where_count=len(_PATTERNS["conditions_append"].findall(content)),
        f_string_sql_count=len(_PATTERNS["f_string_sql"].findall(content)),
        join_count=len(_JOIN_RE.findall(content)),
        case_when_count=len(_CASE_RE.findall(content)),
        on_duplicate_key_count=len(_PATTERNS["on_duplicate_key"].findall(content)),
    )

    # Skip files with no SQL calls
    if report.total_execute_calls == 0 and report.get_db_connection_calls == 0:
        return None

    # Parse AST for function-level analysis
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        logger.warning("Syntax error in %s: %s", filepath, e)
        return report

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            report.total_functions += 1

            # Extract function source lines
            func_start = node.lineno - 1
            func_end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else func_start + 1
            func_source = "\n".join(lines[func_start:func_end])

            # Check if function has SQL patterns
            has_execute = bool(_PATTERNS["cursor_execute"].search(func_source))
            has_connection = bool(_PATTERNS["get_db_connection"].search(func_source))

            if has_execute or has_connection:
                report.functions_with_sql += 1

                func_info = FunctionInfo(
                    name=node.name,
                    line_start=node.lineno,
                    line_end=func_end,
                    has_get_db_connection=has_connection,
                    has_cursor=bool(_PATTERNS["dict_cursor"].search(func_source)),
                    has_manual_close=bool(
                        _PATTERNS["cursor_close"].search(func_source)
                        or _PATTERNS["connection_close"].search(func_source)
                    ),
                    has_try_except="except" in func_source,
                    has_dynamic_where=bool(_PATTERNS["conditions_append"].search(func_source)),
                    has_dynamic_set=bool(_PATTERNS["set_parts_append"].search(func_source)),
                    has_f_string_sql=bool(_PATTERNS["f_string_sql"].search(func_source)),
                )

                # Analyze individual execute calls within the function
                for match in _PATTERNS["cursor_execute"].finditer(func_source):
                    # Try to extract SQL string context (next ~200 chars)
                    sql_context = func_source[match.start():match.start() + 300]
                    sql_call = SQLCall(
                        line_number=func_start + func_source[:match.start()].count("\n") + 1,
                        sql_type=_detect_sql_type(sql_context),
                        sql_preview=sql_context[:120].replace("\n", " ").strip(),
                        is_dynamic="f\"\"\"" in sql_context or "sql +=" in func_source,
                        has_join=bool(_JOIN_RE.search(sql_context)),
                        has_subquery=bool(_SUBQUERY_RE.search(sql_context)),
                        has_case_when=bool(_CASE_RE.search(sql_context)),
                        has_aggregation=bool(_AGG_RE.search(sql_context)),
                        complexity=_classify_sql_complexity(sql_context),
                    )
                    func_info.sql_calls.append(sql_call)

                report.functions.append(func_info)

    # Compute complexity distribution
    dist = {"simple": 0, "moderate": 0, "complex": 0}
    for func in report.functions:
        dist[func.complexity] += 1
    report.complexity_distribution = dist

    return report


def scan_directory(source_dir: Path, exclude_patterns: Optional[list[str]] = None) -> list[FileReport]:
    """
    Scan all Python files in a directory for raw SQL patterns.

    Args:
        source_dir: Root directory to scan.
        exclude_patterns: List of glob patterns to exclude (e.g., ["**/test_*", "**/__pycache__/*"]).

    Returns:
        List of FileReport objects for files that contain SQL patterns.
    """
    exclude_patterns = exclude_patterns or ["**/__pycache__/*", "**/venv/*", "**/.git/*"]
    reports = []

    py_files = sorted(source_dir.rglob("*.py"))
    logger.info("Found %d Python files in %s", len(py_files), source_dir)

    for filepath in py_files:
        # Check exclusion patterns
        rel_path = str(filepath.relative_to(source_dir))
        skip = False
        for pattern in exclude_patterns:
            if filepath.match(pattern):
                skip = True
                break
        if skip:
            continue

        report = scan_file(filepath)
        if report:
            reports.append(report)
            logger.debug(
                "  %-50s  │ %3d execute │ %s",
                rel_path[:50],
                report.total_execute_calls,
                report.overall_complexity,
            )

    logger.info("Scanned %d files with SQL patterns", len(reports))
    return reports


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Scan a Python codebase for raw SQL patterns and generate a migration report.",
    )
    parser.add_argument(
        "--source-dir",
        default="",
        help="Directories to analyze (comma-separated). Falls back to SOURCE_DIR in .env.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output file path for JSON report (default: print summary to stdout).",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=["**/__pycache__/*", "**/venv/*"],
        help="Glob patterns to exclude from scanning.",
    )
    args = parser.parse_args()

    # Resolve source directories
    if args.source_dir:
        source_dirs = [Path(p.strip()) for p in args.source_dir.split(",") if p.strip()]
    else:
        from refactra_mysql.config import SOURCE_DIRS
        source_dirs = SOURCE_DIRS

    if not source_dirs:
        logger.error("No source directories specified. Use --source-dir or set SOURCE_DIR in .env.")
        sys.exit(1)

    # Validate dirs
    for sd in source_dirs:
        if not sd.is_dir():
            logger.error("Source directory does not exist: %s", sd)
            sys.exit(1)

    # Run scan across all directories
    all_reports = []
    for sd in source_dirs:
        logger.info("Scanning: %s", sd)
        reports = scan_directory(sd, exclude_patterns=args.exclude)
        all_reports.extend(reports)

    if not all_reports:
        logger.warning("No files with raw SQL patterns found.")
        sys.exit(0)

    # Print summary
    from refactra_mysql.analyze import reporter as rpt
    rpt.print_summary(all_reports)

    # Save JSON report
    if args.output:
        output_path = Path(args.output)
    else:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = _REPORTS_DIR / f"scanner_{timestamp}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(r) for r in all_reports]
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info("Full report saved to %s", output_path)


if __name__ == "__main__":
    main()

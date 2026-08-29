"""
Code Validator — Validates AI-converted Python code.

Checks converted files for:
1. Python syntax correctness (py_compile)
2. Import resolution
3. SQLAlchemy pattern validity
4. Missing model references

Usage:
    refactra-mysql validate --file ./path/to/converted.py --models-file ./path/to/models.py
"""
import ast
import argparse
import re
import sys
from pathlib import Path
from typing import Any, Optional

from refactra_mysql.config import setup_logging

logger = setup_logging("validator")


# =============================================================================
# Validation Checks
# =============================================================================

def check_syntax(filepath: Path) -> list[str]:
    """
    Check if a Python file has valid syntax.

    Returns:
        List of error messages. Empty means syntax is valid.
    """
    errors = []
    try:
        source = filepath.read_text(encoding="utf-8")
        ast.parse(source)
    except SyntaxError as e:
        errors.append(f"Syntax error at line {e.lineno}: {e.msg}")
    except Exception as e:
        errors.append(f"Unexpected error: {e}")
    return errors


def check_model_references(filepath: Path, models_file: Path) -> list[str]:
    """
    Check if all ORM model references in the file match available models.

    Returns:
        List of warnings about unresolved model references.
    """
    warnings = []

    try:
        source = filepath.read_text(encoding="utf-8")
        models_source = models_file.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read file: {e}"]

    # Extract model class names from models file
    try:
        models_tree = ast.parse(models_source)
    except SyntaxError:
        return ["Cannot parse models file"]

    available_models = set()
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef):
            available_models.add(node.name)

    # Find db.query(ModelName) references in the converted file
    query_pattern = re.compile(r"db\.query\(\s*(\w+)")
    for match in query_pattern.finditer(source):
        model_name = match.group(1)
        if model_name not in available_models and model_name != "func":
            warnings.append(
                f"Model '{model_name}' used in db.query() but not found in models file"
            )

    # Find Model.column references
    dot_pattern = re.compile(r"(\b[A-Z]\w+)\.(\w+)")
    for match in dot_pattern.finditer(source):
        class_name = match.group(1)
        # Skip common non-model classes
        if class_name in {"Session", "Optional", "List", "Dict", "Path", "Exception", "None", "True", "False"}:
            continue
        if class_name in available_models:
            continue
        if class_name not in available_models and len(class_name) > 2:
            # Only warn if it looks like it could be a model reference
            attr = match.group(2)
            if attr not in {"__name__", "__class__", "__dict__", "args", "message"}:
                warnings.append(
                    f"Possible unresolved model reference: {class_name}.{attr}"
                )

    return warnings


def check_orm_patterns(filepath: Path) -> list[str]:
    """
    Check for common ORM conversion issues.

    Returns:
        List of warnings about potential issues.
    """
    warnings = []

    try:
        source = filepath.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read file: {e}"]

    # Check for leftover raw SQL patterns
    if "cursor.execute" in source:
        warnings.append("Leftover cursor.execute() found — conversion incomplete")

    if "cursor.fetchall" in source:
        warnings.append("Leftover cursor.fetchall() found — conversion incomplete")

    if "cursor.fetchone" in source:
        warnings.append("Leftover cursor.fetchone() found — conversion incomplete")

    if "get_db_connection()" in source:
        warnings.append("Leftover get_db_connection() found — boilerplate not removed")

    if "cursor.close()" in source:
        warnings.append("Leftover cursor.close() found — cleanup not removed")

    if "connection.close()" in source:
        warnings.append("Leftover connection.close() found — cleanup not removed")

    # Check for missing Session import
    if "db.query(" in source and "Session" not in source:
        warnings.append("db.query() used but Session import may be missing")

    # Check for missing text() import when used
    if "text(" in source and "from sqlalchemy import" not in source:
        if "from sqlalchemy" not in source:
            warnings.append("text() used but sqlalchemy import may be missing")

    return warnings


def validate_file(filepath: Path, models_file: Optional[Path] = None) -> dict:
    """
    Run all validation checks on a single file.

    Returns:
        Dict with 'errors', 'warnings', and 'status' keys.
    """
    result: dict[str, Any] = {
        "file": str(filepath),
        "errors": [],
        "warnings": [],
        "status": "pass",
    }

    # Syntax check (critical)
    syntax_errors = check_syntax(filepath)
    result["errors"].extend(syntax_errors)

    # ORM pattern check
    orm_warnings = check_orm_patterns(filepath)
    result["warnings"].extend(orm_warnings)

    # Model reference check
    if models_file and models_file.is_file():
        model_warnings = check_model_references(filepath, models_file)
        result["warnings"].extend(model_warnings)

    # Set status
    if result["errors"]:
        result["status"] = "fail"
    elif result["warnings"]:
        result["status"] = "warn"

    return result


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Validate AI-converted Python files for correctness.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Validate a single file.",
    )
    parser.add_argument(
        "--source-dir",
        default=None,
        help="Validate all Python files in a directory.",
    )
    parser.add_argument(
        "--models-file",
        default=None,
        help="Path to SQLAlchemy models file for reference checking.",
    )
    args = parser.parse_args()

    models_path = Path(args.models_file) if args.models_file else None

    if args.file:
        filepath = Path(args.file)
        result = validate_file(filepath, models_path)
        _print_result(result)

    elif args.source_dir:
        source_dir = Path(args.source_dir)
        py_files = sorted(source_dir.rglob("*.py"))
        py_files = [f for f in py_files if "__pycache__" not in str(f)]

        total_pass = 0
        total_warn = 0
        total_fail = 0

        for filepath in py_files:
            result = validate_file(filepath, models_path)
            _print_result(result)

            if result["status"] == "pass":
                total_pass += 1
            elif result["status"] == "warn":
                total_warn += 1
            else:
                total_fail += 1

        print(f"\n{'=' * 50}")
        print(f"TOTAL: {total_pass} pass, {total_warn} warn, {total_fail} fail")
        print(f"{'=' * 50}")

        if total_fail > 0:
            sys.exit(1)
    else:
        parser.error("Specify --file or --source-dir")


def _print_result(result: dict) -> None:
    """Pretty-print a validation result."""
    filepath = Path(result["file"]).name
    status = result["status"].upper()

    icon = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]"}.get(result["status"], "?")
    print(f"  {icon} {filepath}: {status}")

    for err in result["errors"]:
        print(f"      ERROR: {err}")
    for warn in result["warnings"]:
        print(f"      WARN:  {warn}")


if __name__ == "__main__":
    main()

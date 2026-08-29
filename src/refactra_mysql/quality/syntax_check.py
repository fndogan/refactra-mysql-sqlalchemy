"""
Syntax Checker — Verifies that converted Python files compile correctly.

Runs py_compile on each file and reports any syntax errors.

Usage:
    refactra-mysql syntax --source-dir ./output
"""
import argparse
import py_compile
import sys
from pathlib import Path

from refactra_mysql.config import setup_logging

logger = setup_logging("syntax_check")


def check_file(filepath: Path) -> dict:
    """
    Check if a Python file compiles without syntax errors.

    Returns:
        Dict with 'file', 'status', and optional 'error' keys.
    """
    result = {"file": str(filepath), "status": "pass"}

    try:
        py_compile.compile(str(filepath), doraise=True)
    except py_compile.PyCompileError as e:
        result["status"] = "fail"
        result["error"] = str(e)

    return result


def check_directory(source_dir: Path) -> list[dict]:
    """Check all Python files in a directory."""
    py_files = sorted(source_dir.rglob("*.py"))
    py_files = [f for f in py_files if "__pycache__" not in str(f)]

    results = []
    pass_count = 0
    fail_count = 0

    logger.info("Checking %d Python files in %s", len(py_files), source_dir)
    logger.info("-" * 60)

    for filepath in py_files:
        result = check_file(filepath)
        results.append(result)

        if result["status"] == "pass":
            pass_count += 1
            logger.info("  [PASS] %s", filepath.name)
        else:
            fail_count += 1
            logger.error("  [FAIL] %s", filepath.name)
            logger.error("    %s", result.get("error", "Unknown error"))

    logger.info("-" * 60)
    logger.info("Results: %d pass, %d fail", pass_count, fail_count)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Check Python files for syntax errors.",
    )
    parser.add_argument(
        "--source-dir",
        required=True,
        help="Directory containing Python files to check.",
    )
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.is_dir():
        logger.error("Directory does not exist: %s", source_dir)
        sys.exit(1)

    results = check_directory(source_dir)

    if any(r["status"] == "fail" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()

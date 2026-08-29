"""
Comparison Tool — Side-by-side diff between original and converted files.

Generates a comparison report showing what changed during conversion,
useful for manual review of AI-converted code.

Usage:
    refactra-mysql compare --original-dir ./path/to/original --converted-dir ./path/to/converted
"""
import argparse
import difflib
from pathlib import Path
from typing import Any

from refactra_mysql.config import setup_logging

logger = setup_logging("comparison")


def compare_files(original: Path, converted: Path) -> dict:
    """
    Compare original and converted files and generate a diff.

    Returns:
        Dict with 'file', 'lines_added', 'lines_removed', 'diff' keys.
    """
    result: dict[str, Any] = {
        "file": str(original.name),
        "lines_added": 0,
        "lines_removed": 0,
        "lines_unchanged": 0,
        "diff": "",
    }

    try:
        orig_lines = original.read_text(encoding="utf-8").splitlines(keepends=True)
        conv_lines = converted.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as e:
        result["error"] = str(e)
        return result

    diff = list(difflib.unified_diff(
        orig_lines,
        conv_lines,
        fromfile=f"original/{original.name}",
        tofile=f"converted/{converted.name}",
        lineterm="",
    ))

    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            result["lines_added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            result["lines_removed"] += 1

    result["lines_unchanged"] = len(orig_lines) - result["lines_removed"]
    result["diff"] = "\n".join(diff)

    return result


def compare_directories(original_dir: Path, converted_dir: Path) -> list[dict]:
    """Compare all matching Python files between two directories."""
    results = []

    orig_files = {f.name: f for f in original_dir.rglob("*.py") if "__pycache__" not in str(f)}
    conv_files = {f.name: f for f in converted_dir.rglob("*.py") if "__pycache__" not in str(f)}

    matched = set(orig_files.keys()) & set(conv_files.keys())
    only_original = set(orig_files.keys()) - set(conv_files.keys())
    only_converted = set(conv_files.keys()) - set(orig_files.keys())

    logger.info("Files matched:          %d", len(matched))
    logger.info("Files only in original: %d", len(only_original))
    logger.info("Files only in converted:%d", len(only_converted))
    logger.info("-" * 60)

    total_added = 0
    total_removed = 0

    for filename in sorted(matched):
        result = compare_files(orig_files[filename], conv_files[filename])
        results.append(result)
        total_added += result["lines_added"]
        total_removed += result["lines_removed"]

        if result["lines_added"] > 0 or result["lines_removed"] > 0:
            logger.info(
                "  %s: +%d/-%d lines",
                filename,
                result["lines_added"],
                result["lines_removed"],
            )

    logger.info("-" * 60)
    logger.info("Total: +%d lines added, -%d lines removed", total_added, total_removed)
    logger.info("Net change: %+d lines", total_added - total_removed)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Compare original and converted Python files.",
    )
    parser.add_argument(
        "--original-dir",
        required=True,
        help="Directory with original files.",
    )
    parser.add_argument(
        "--converted-dir",
        required=True,
        help="Directory with converted files.",
    )
    parser.add_argument(
        "--show-diff",
        action="store_true",
        help="Print unified diff for each file.",
    )
    args = parser.parse_args()

    original_dir = Path(args.original_dir)
    converted_dir = Path(args.converted_dir)

    results = compare_directories(original_dir, converted_dir)

    if args.show_diff:
        for result in results:
            if result.get("diff"):
                print(f"\n{'=' * 60}")
                print(f"  {result['file']}")
                print(f"{'=' * 60}")
                print(result["diff"])


if __name__ == "__main__":
    main()

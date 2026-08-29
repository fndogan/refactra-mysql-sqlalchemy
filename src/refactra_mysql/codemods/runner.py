"""
Run All LibCST Codemods — Orchestrator.

Applies all codemods in the correct sequence to a source directory,
collects detailed change information, and generates reports.

Usage:
    refactra-mysql codemods --source-dir ./path/to/queries --output-dir ./output --dry-run
    refactra-mysql codemods --source-dir ./path/to/queries --output-dir ./output
"""
import argparse
import sys
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext

from refactra_mysql.config import setup_logging
from refactra_mysql.codemods.cleanup_boilerplate import CleanupBoilerplateCommand
from refactra_mysql.codemods.cursor_to_text import CursorToTextCommand
from refactra_mysql.codemods.fetch_transform import FetchTransformCommand
from refactra_mysql.codemods.reporter import CodemodReporter
from refactra_mysql.io_utils import atomic_write_text

logger = setup_logging("codemods")


# Codemods are applied in this order — sequence matters!
CODEMOD_PIPELINE = [
    ("Cleanup Boilerplate", CleanupBoilerplateCommand),
    ("Cursor → text()", CursorToTextCommand),
    ("Fetch Transform", FetchTransformCommand),
]


def apply_codemod(source_code: str, codemod_class, filename: str = "<unknown>") -> tuple[str, list[str]]:
    """
    Apply a single codemod to source code.

    Args:
        source_code: Python source code as string.
        codemod_class: LibCST codemod command class.
        filename: Filename for error reporting.

    Returns:
        Tuple of (transformed_code, list_of_changes).
    """
    try:
        context = CodemodContext()
        tree = cst.parse_module(source_code)
        transformer = codemod_class(context)
        modified_tree = tree.visit(transformer)

        # Collect changes from transformer
        changes = getattr(transformer, "changes", [])
        return modified_tree.code, list(changes)

    except cst.ParserSyntaxError as e:
        logger.warning("Syntax error in %s: %s", filename, e)
        return source_code, []
    except Exception as e:
        logger.error("Error in %s on %s: %s", codemod_class.__name__, filename, e)
        return source_code, [f"ERROR │ {codemod_class.__name__}: {e}"]


def process_file(
    filepath: Path,
    output_path: Path,
    reporter: CodemodReporter,
    dry_run: bool = False,
) -> dict:
    """
    Apply all codemods to a single file.

    Args:
        filepath: Input file path.
        output_path: Output file path.
        reporter: Reporter instance to collect changes.
        dry_run: If True, don't write changes.

    Returns:
        Dict with processing results.
    """
    result = {
        "file": str(filepath),
        "status": "skipped",
        "changes": 0,
    }

    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        result["status"] = "error"
        result["error"] = str(e)
        reporter.add_file_result(filepath.name, [], "", "", status="error", error=str(e))
        return result

    def write_output(content: str) -> None:
        """Write a complete Python output tree without rewriting unchanged in-place files."""
        if dry_run:
            return
        same_path = filepath.resolve() == output_path.resolve()
        if same_path and content == source:
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, content)

    # Skip files without relevant patterns
    if "cursor" not in source and "get_db_connection" not in source:
        reporter.add_file_result(filepath.name, [], source, source, status="skipped")
        write_output(source)
        return result

    # Apply each codemod in sequence, collecting all changes
    current_code = source
    all_changes: list[str] = []

    for step_name, codemod_class in CODEMOD_PIPELINE:
        modified_code, changes = apply_codemod(current_code, codemod_class, filepath.name)
        if changes:
            all_changes.extend([f"[{step_name}] {c}" for c in changes])
        current_code = modified_code

    # Determine status
    if current_code == source:
        result["status"] = "unchanged"
        reporter.add_file_result(filepath.name, all_changes, source, current_code, status="unchanged")
        write_output(source)
        return result

    result["changes"] = len(all_changes)
    result["status"] = "modified"

    # Report
    reporter.add_file_result(filepath.name, all_changes, source, current_code, status="modified")

    # Write output
    if not dry_run:
        write_output(current_code)
        logger.info("  [PASS] %s (%d changes)", filepath.name, len(all_changes))
    else:
        logger.info("  ○ %s (%d changes) [dry-run]", filepath.name, len(all_changes))

    return result


def run_pipeline(
    source_dir: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
    seen_files: set | None = None,
) -> CodemodReporter:
    """
    Run the full codemod pipeline on all Python files in a directory.

    Args:
        source_dir: Directory containing source files.
        output_dir: Directory for output files. If None, modifies in-place.
        dry_run: If True, preview changes without writing.
        seen_files: Set of already-processed absolute paths (prevents duplicates).

    Returns:
        CodemodReporter with all collected results.
    """
    if seen_files is None:
        seen_files = set()

    reporter = CodemodReporter(dry_run=dry_run)

    logger.info("=" * 60)
    logger.info("LibCST Codemod Pipeline")
    logger.info("=" * 60)
    logger.info("Source:  %s", source_dir)
    logger.info("Output:  %s", output_dir or "(in-place)")
    logger.info("Dry run: %s", dry_run)
    logger.info("-" * 60)

    py_files = sorted(source_dir.rglob("*.py"))
    py_files = [f for f in py_files if "__pycache__" not in str(f)]

    # Deduplicate across multiple source dirs
    unique_files = []
    for f in py_files:
        abs_path = str(f.resolve())
        if abs_path not in seen_files:
            seen_files.add(abs_path)
            unique_files.append(f)

    skipped = len(py_files) - len(unique_files)
    logger.info("Found %d Python files (%d skipped as duplicates)", len(py_files), skipped)

    for filepath in unique_files:
        rel_path = filepath.relative_to(source_dir)
        if output_dir:
            out_path = output_dir / rel_path
        else:
            out_path = filepath

        process_file(filepath, out_path, reporter, dry_run=dry_run)

    return reporter


def main():
    parser = argparse.ArgumentParser(
        description="Apply LibCST codemods to remove raw SQL boilerplate.",
    )
    parser.add_argument(
        "--source-dir",
        default="",
        help="Directories to process (comma-separated). Falls back to SOURCE_DIR in .env.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for transformed files.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Explicitly allow modifying source files in-place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files.",
    )
    args = parser.parse_args()

    if args.output_dir and args.in_place:
        parser.error("Use either --output-dir or --in-place, not both.")
    if not args.dry_run and not args.output_dir and not args.in_place:
        parser.error(
            "Refusing to modify source files implicitly. Use --output-dir, "
            "--dry-run, or explicitly pass --in-place."
        )

    # Resolve source directories
    if args.source_dir:
        source_dirs = [Path(p.strip()) for p in args.source_dir.split(",") if p.strip()]
    else:
        from refactra_mysql.config import SOURCE_DIRS
        source_dirs = SOURCE_DIRS

    if not source_dirs:
        logger.error("No source directories specified. Use --source-dir or set SOURCE_DIR in .env.")
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else None

    # Run pipeline on each source directory
    combined_reporter = CodemodReporter(dry_run=args.dry_run)
    seen_files: set[str] = set()

    for source_dir in source_dirs:
        if not source_dir.is_dir():
            logger.error("Source directory does not exist: %s", source_dir)
            sys.exit(1)

        reporter = run_pipeline(source_dir, output_dir, dry_run=args.dry_run, seen_files=seen_files)

        # Merge results into combined reporter
        combined_reporter.results.extend(reporter.results)

    # Print combined console report
    combined_reporter.print_console_report()

    # Save detailed reports
    report_path = combined_reporter.save_report()
    json_path = combined_reporter.save_json()

    logger.info("Text report: %s", report_path)
    logger.info("JSON report: %s", json_path)

    # Exit with error if any files failed
    summary = combined_reporter.get_summary()
    if summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

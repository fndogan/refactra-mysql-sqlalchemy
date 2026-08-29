"""
Codemod Reporter — Generates detailed change reports for LibCST codemods.

Creates structured reports showing exactly what each codemod changed
in each file, with per-file and per-codemod breakdowns.

Reports are saved to the reports/ directory as timestamped files.

Usage (called by the codemod pipeline, not standalone):
    reporter = CodemodReporter()
    reporter.add_file_result("invoices.py", changes, original_code, modified_code)
    reporter.save()
"""
import json
import difflib
from datetime import datetime, timezone
from pathlib import Path

from refactra_mysql.config import REPORTS_DIR, setup_logging

logger = setup_logging("codemod_reporter")

_REPORTS_DIR = REPORTS_DIR / "codemods"


class CodemodReporter:
    """
    Collects and formats detailed change reports from LibCST codemods.

    Attributes:
        dry_run: Whether this was a dry run (no files written).
        results: List of per-file result dicts.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.results: list[dict] = []
        self._start_time = datetime.now(timezone.utc)

    def add_file_result(
        self,
        filename: str,
        changes: list[str],
        original_code: str,
        modified_code: str,
        status: str = "modified",
        error: str | None = None,
    ) -> None:
        """
        Record the result of processing a single file.

        Args:
            filename: Name of the processed file.
            changes: List of change descriptions from codemods.
            original_code: Original source code.
            modified_code: Modified source code after codemods.
            status: One of 'modified', 'unchanged', 'skipped', 'error'.
            error: Error message if status is 'error'.
        """
        # Generate diff
        diff_lines = []
        if status == "modified" and original_code != modified_code:
            diff_lines = list(difflib.unified_diff(
                original_code.splitlines(keepends=True),
                modified_code.splitlines(keepends=True),
                fromfile=f"original/{filename}",
                tofile=f"modified/{filename}",
                n=2,
            ))

        self.results.append({
            "filename": filename,
            "status": status,
            "changes": changes,
            "change_count": len(changes),
            "diff_lines": len(diff_lines),
            "diff": "".join(diff_lines),
            "error": error,
        })

    def get_summary(self) -> dict:
        """Get aggregate summary statistics."""
        total_files = len(self.results)
        modified = sum(1 for r in self.results if r["status"] == "modified")
        unchanged = sum(1 for r in self.results if r["status"] == "unchanged")
        skipped = sum(1 for r in self.results if r["status"] == "skipped")
        errors = sum(1 for r in self.results if r["status"] == "error")
        total_changes = sum(r["change_count"] for r in self.results)

        # Count by change type
        change_types: dict[str, int] = {}
        for r in self.results:
            for change in r["changes"]:
                # Extract type (e.g., "REMOVE" or "CONVERT")
                ctype = change.split("│")[0].strip() if "│" in change else "OTHER"
                change_types[ctype] = change_types.get(ctype, 0) + 1

        return {
            "dry_run": self.dry_run,
            "total_files": total_files,
            "modified": modified,
            "unchanged": unchanged,
            "skipped": skipped,
            "errors": errors,
            "total_changes": total_changes,
            "change_types": change_types,
            "duration_seconds": (datetime.now(timezone.utc) - self._start_time).total_seconds(),
        }

    def print_console_report(self) -> None:
        """Print formatted report to console."""
        summary = self.get_summary()

        print("\n" + "=" * 70)
        print("  LIBCST CODEMOD REPORT" + ("  [DRY RUN]" if self.dry_run else ""))
        print("=" * 70)

        # Summary table
        print(f"""
┌──────────────────────────────┬──────────┐
│ Metric                       │ Count    │
├──────────────────────────────┼──────────┤
│ Files scanned                │ {summary['total_files']:>8} │
│ Files modified               │ {summary['modified']:>8} │
│ Files unchanged              │ {summary['unchanged']:>8} │
│ Files skipped (no SQL)       │ {summary['skipped']:>8} │
│ Files with errors            │ {summary['errors']:>8} │
│ Total changes                │ {summary['total_changes']:>8} │
└──────────────────────────────┴──────────┘
""")

        # Change type breakdown
        if summary["change_types"]:
            print("  CHANGE TYPES:")
            for ctype, count in sorted(summary["change_types"].items()):
                print(f"    {ctype:<10} {count:>5}x")
            print()

        # Per-file details
        modified_files = [r for r in self.results if r["status"] == "modified"]
        if modified_files:
            print("─" * 70)
            print("  PER-FILE CHANGES")
            print("─" * 70)

            for r in sorted(modified_files, key=lambda x: x["change_count"], reverse=True):
                print(f"\n  [FILE] {r['filename']} ({r['change_count']} changes)")
                for change in r["changes"]:
                    print(f"     {change}")

        # Errors
        error_files = [r for r in self.results if r["status"] == "error"]
        if error_files:
            print("\n" + "─" * 70)
            print("  [WARN] ERRORS")
            print("─" * 70)
            for r in error_files:
                print(f"  [FAIL] {r['filename']}: {r['error']}")

        print("\n" + "=" * 70)
        print(f"  Completed in {summary['duration_seconds']:.1f}s")
        print("=" * 70 + "\n")

    def save_report(self) -> Path:
        """
        Save the full report to the reports/ directory.

        Returns:
            Path to the saved report file.
        """
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = self._start_time.strftime("%Y%m%d_%H%M%S")
        mode = "dryrun" if self.dry_run else "applied"
        report_path = _REPORTS_DIR / f"codemod_{mode}_{timestamp}.txt"

        summary = self.get_summary()

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"  LIBCST CODEMOD REPORT — {timestamp}\n")
            f.write(f"  Mode: {'DRY RUN' if self.dry_run else 'APPLIED'}\n")
            f.write("=" * 70 + "\n\n")

            # Summary
            f.write(f"Files scanned:    {summary['total_files']}\n")
            f.write(f"Files modified:   {summary['modified']}\n")
            f.write(f"Files unchanged:  {summary['unchanged']}\n")
            f.write(f"Files skipped:    {summary['skipped']}\n")
            f.write(f"Files with errors:{summary['errors']}\n")
            f.write(f"Total changes:    {summary['total_changes']}\n")
            f.write(f"Duration:         {summary['duration_seconds']:.1f}s\n\n")

            # Change type breakdown
            if summary["change_types"]:
                f.write("CHANGE TYPE BREAKDOWN:\n")
                for ctype, count in sorted(summary["change_types"].items()):
                    f.write(f"  {ctype:<10} {count:>5}x\n")
                f.write("\n")

            # Per-file details with diffs
            f.write("=" * 70 + "\n")
            f.write("  PER-FILE DETAILS\n")
            f.write("=" * 70 + "\n\n")

            for r in sorted(self.results, key=lambda x: x["change_count"], reverse=True):
                if r["status"] == "skipped":
                    continue

                f.write(f"{'─' * 70}\n")
                f.write(f"  {r['filename']} — {r['status'].upper()} ({r['change_count']} changes)\n")
                f.write(f"{'─' * 70}\n")

                if r["changes"]:
                    f.write("  Changes:\n")
                    for change in r["changes"]:
                        f.write(f"    {change}\n")
                    f.write("\n")

                if r["diff"]:
                    f.write("  Diff:\n")
                    f.write(r["diff"])
                    f.write("\n")

                if r["error"]:
                    f.write(f"  ERROR: {r['error']}\n")

                f.write("\n")

        logger.info("Report saved: %s", report_path)
        return report_path

    def save_json(self) -> Path:
        """Save report as JSON for programmatic access."""
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = self._start_time.strftime("%Y%m%d_%H%M%S")
        mode = "dryrun" if self.dry_run else "applied"
        json_path = _REPORTS_DIR / f"codemod_{mode}_{timestamp}.json"

        data = {
            "summary": self.get_summary(),
            "files": self.results,
        }
        # Remove diff from JSON (too large)
        for r in data["files"]:
            r.pop("diff", None)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info("JSON report saved: %s", json_path)
        return json_path

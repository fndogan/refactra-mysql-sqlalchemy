"""
AI Converter Reporter — Generates detailed conversion reports.

Creates structured reports showing:
  - Per-file conversion results (functions converted/failed/skipped)
  - Safety classification summary (safe/review/skip counts)
  - API usage (tokens and wait time)
  - Per-function details

Uses the same report structure as the deterministic codemod pipeline.

Usage:
    reporter = AIConverterReporter(provider="anthropic", model="provider-model-id")
    reporter.add_file_result(result_dict)
    reporter.set_api_usage(usage_dict, limiter_stats)
    reporter.print_console_report()
    reporter.save_report()
    reporter.save_json()
"""
import json
from datetime import datetime
from pathlib import Path

from refactra_mysql.config import REPORTS_DIR, setup_logging

logger = setup_logging("ai_reporter")

_REPORTS_DIR = REPORTS_DIR / "converter"


class AIConverterReporter:
    """Collects and formats detailed reports from AI conversion runs."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "unknown",
        dry_run: bool = False,
    ):
        self.provider = provider
        self.model = model
        self.dry_run = dry_run
        self.results: list[dict] = []
        self.api_usage: dict = {}
        self.limiter_stats: dict = {}
        self.start_time = datetime.now()

    def add_file_result(self, result: dict) -> None:
        """Record the result of processing a single file."""
        self.results.append(result)

    def set_api_usage(self, usage: dict, limiter_stats: dict) -> None:
        """Record API usage stats."""
        self.api_usage = usage
        self.limiter_stats = limiter_stats

    def get_summary(self) -> dict:
        """Get aggregate summary statistics."""
        total_files = len(self.results)
        skipped_files = sum(1 for r in self.results if r.get("status") == "skipped")
        total_funcs = sum(r.get("functions_total", 0) for r in self.results)
        converted = sum(r.get("functions_converted", 0) for r in self.results)
        failed = sum(r.get("functions_failed", 0) for r in self.results)
        skipped_unsafe = sum(r.get("functions_skipped_unsafe", 0) for r in self.results)
        review = sum(r.get("functions_review", 0) for r in self.results)

        elapsed = (datetime.now() - self.start_time).total_seconds()

        return {
            "files_processed": total_files,
            "files_skipped": skipped_files,
            "functions_total": total_funcs,
            "functions_converted": converted,
            "functions_failed": failed,
            "functions_skipped_unsafe": skipped_unsafe,
            "functions_review": review,
            "elapsed_seconds": round(elapsed, 1),
        }

    def print_console_report(self) -> None:
        """Print formatted report to console."""
        summary = self.get_summary()

        logger.info("=" * 60)
        logger.info("AI CONVERTER REPORT")
        logger.info("=" * 60)
        logger.info("Provider:            %s (%s)", self.provider, self.model)
        logger.info("Dry run:             %s", self.dry_run)
        logger.info("Elapsed:             %.0fs", summary["elapsed_seconds"])
        logger.info("-" * 60)
        logger.info("CONVERSION SUMMARY")
        logger.info("-" * 60)
        logger.info("Files processed:     %d", summary["files_processed"])
        logger.info("Files skipped:       %d", summary["files_skipped"])
        logger.info("Functions total:     %d", summary["functions_total"])
        logger.info("Functions converted: %d [PASS]", summary["functions_converted"])
        logger.info("Functions failed:    %d [FAIL]", summary["functions_failed"])
        logger.info("Functions SKIP:      %d [SKIP] (unsafe/dynamic SQL)", summary["functions_skipped_unsafe"])
        logger.info("Functions REVIEW:    %d [WARN] (needs human review)", summary["functions_review"])

        if self.api_usage:
            logger.info("-" * 60)
            logger.info("API USAGE")
            logger.info("  Requests:        %d", self.api_usage.get("requests", 0))
            logger.info("  Input tokens:    %d", self.api_usage.get("input_tokens", 0))
            logger.info("  Output tokens:   %d", self.api_usage.get("output_tokens", 0))
            logger.info("  Cached tokens:   %d", self.api_usage.get("cached_tokens", 0))
            if self.limiter_stats:
                logger.info("  Total wait time: %.1fs", self.limiter_stats.get("total_wait_seconds", 0))

        # Per-file results
        logger.info("-" * 60)
        logger.info("PER-FILE RESULTS")
        logger.info("-" * 60)
        for r in self.results:
            if r.get("status") == "skipped":
                logger.info("  ─ %s (no SQL functions)", r["file"])
            elif r.get("functions_failed", 0) > 0:
                logger.info(
                    "  [FAIL] %s: %d/%d converted, %d FAILED",
                    r["file"], r["functions_converted"], r["functions_total"],
                    r["functions_failed"],
                )
            else:
                skipped = r.get("functions_skipped_unsafe", 0)
                review = r.get("functions_review", 0)
                extra = ""
                if skipped:
                    extra += f", {skipped} SKIP"
                if review:
                    extra += f", {review} REVIEW"
                logger.info(
                    "  [PASS] %s: %d/%d converted%s",
                    r["file"], r["functions_converted"], r["functions_total"], extra,
                )

        # Skipped details
        all_skipped = []
        for r in self.results:
            for d in r.get("skipped_details", []):
                all_skipped.append({"file": r["file"], **d})

        if all_skipped:
            logger.info("-" * 60)
            logger.info("SKIPPED FUNCTIONS (unsafe — need manual conversion)")
            logger.info("-" * 60)
            for s in all_skipped:
                logger.info("  [SKIP] %s::%s — %s", s["file"], s["function"], s["reason"])

        logger.info("=" * 60)
        logger.info("Done!")

    def save_report(self) -> Path:
        """Save the full report to the reports/ directory as TXT."""
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary = self.get_summary()

        lines = [
            "=" * 70,
            "  AI CONVERTER REPORT",
            f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            "",
            f"  Provider:            {self.provider} ({self.model})",
            f"  Dry run:             {self.dry_run}",
            f"  Elapsed:             {summary['elapsed_seconds']:.0f}s",
            "",
            "─" * 70,
            "  SUMMARY",
            "─" * 70,
            f"  Files processed:     {summary['files_processed']}",
            f"  Files skipped:       {summary['files_skipped']}",
            f"  Functions total:     {summary['functions_total']}",
            f"  Functions converted: {summary['functions_converted']} [PASS]",
            f"  Functions failed:    {summary['functions_failed']} [FAIL]",
            f"  Functions SKIP:      {summary['functions_skipped_unsafe']} [SKIP]",
            f"  Functions REVIEW:    {summary['functions_review']} [WARN]",
        ]

        if self.api_usage:
            lines.extend([
                "",
                "─" * 70,
                "  API USAGE",
                "─" * 70,
                f"  Requests:            {self.api_usage.get('requests', 0)}",
                f"  Input tokens:        {self.api_usage.get('input_tokens', 0)}",
                f"  Output tokens:       {self.api_usage.get('output_tokens', 0)}",
                f"  Cached tokens:       {self.api_usage.get('cached_tokens', 0)}",
                f"  Total wait time:     {self.limiter_stats.get('total_wait_seconds', 0):.1f}s",
            ])

        lines.extend([
            "",
            "─" * 70,
            "  PER-FILE RESULTS",
            "─" * 70,
        ])

        for r in self.results:
            status_icon = "[PASS]" if r.get("functions_failed", 0) == 0 else "[FAIL]"
            skipped = r.get("functions_skipped_unsafe", 0)
            review = r.get("functions_review", 0)
            extra = ""
            if skipped:
                extra += f", {skipped} SKIP"
            if review:
                extra += f", {review} REVIEW"
            lines.append(
                f"  {status_icon} {r['file']}: "
                f"{r.get('functions_converted', 0)}/{r.get('functions_total', 0)} converted"
                + (f", {r['functions_failed']} FAILED" if r.get("functions_failed", 0) > 0 else "")
                + extra
            )

        # Skipped details
        all_skipped = []
        for r in self.results:
            for d in r.get("skipped_details", []):
                all_skipped.append({"file": r["file"], **d})

        if all_skipped:
            lines.extend([
                "",
                "─" * 70,
                "  SKIPPED FUNCTIONS (need manual conversion)",
                "─" * 70,
            ])
            for s in all_skipped:
                lines.append(f"  [SKIP] {s['file']}::{s['function']} — {s['reason']}")

        lines.extend(["", "=" * 70])

        txt_path = _REPORTS_DIR / f"ai_converter_{timestamp}.txt"
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("TXT report saved: %s", txt_path)
        return txt_path

    def save_json(self) -> Path:
        """Save report as JSON for programmatic access."""
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        report_data = {
            "timestamp": datetime.now().isoformat(),
            "provider": self.provider,
            "model": self.model,
            "dry_run": self.dry_run,
            "summary": self.get_summary(),
            "api_usage": self.api_usage,
            "rate_limiter": self.limiter_stats,
            "files": self.results,
        }

        json_path = _REPORTS_DIR / f"ai_converter_{timestamp}.json"
        json_path.write_text(
            json.dumps(report_data, indent=2, default=str), encoding="utf-8",
        )
        logger.info("JSON report saved: %s", json_path)
        return json_path

"""
Report Generator — Formats and displays scan results.

Generates human-readable summary tables and statistics from scanner output.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scanner import FileReport

from refactra_mysql.config import setup_logging

logger = setup_logging("reporter")


def print_summary(reports: list[FileReport]) -> None:
    """
    Print a formatted summary of all scan reports to stdout.

    Args:
        reports: List of FileReport objects from the scanner.
    """
    # Aggregate totals
    total_files = len(reports)
    total_lines = sum(r.total_lines for r in reports)
    total_functions = sum(r.total_functions for r in reports)
    total_sql_functions = sum(r.functions_with_sql for r in reports)
    total_executes = sum(r.total_execute_calls for r in reports)
    total_connections = sum(r.get_db_connection_calls for r in reports)
    total_cursor_close = sum(r.cursor_close_calls for r in reports)
    total_conn_close = sum(r.connection_close_calls for r in reports)
    total_commits = sum(r.connection_commit_calls for r in reports)
    total_rollbacks = sum(r.connection_rollback_calls for r in reports)
    total_dynamic_where = sum(r.dynamic_where_count for r in reports)
    total_f_string = sum(r.f_string_sql_count for r in reports)
    total_joins = sum(r.join_count for r in reports)
    total_case = sum(r.case_when_count for r in reports)
    total_on_dup = sum(r.on_duplicate_key_count for r in reports)

    # Complexity distribution
    complexity_dist = {"simple": 0, "moderate": 0, "complex": 0}
    for r in reports:
        for func in r.functions:
            complexity_dist[func.complexity] += 1

    # Boilerplate that LibCST will remove
    boilerplate_count = (
        total_connections
        + total_cursor_close
        + total_conn_close
        + total_commits
        + total_rollbacks
    )

    # Print report
    print("\n" + "=" * 80)
    print("  MYSQL → SQLALCHEMY ORM MIGRATION — ANALYSIS REPORT")
    print("=" * 80)

    print(f"""
┌─────────────────────────────────────┬────────────┐
│ Metric                              │ Count      │
├─────────────────────────────────────┼────────────┤
│ Files with raw SQL                  │ {total_files:>10} │
│ Total lines of code                 │ {total_lines:>10} │
│ Total functions                     │ {total_functions:>10} │
│ Functions with SQL                  │ {total_sql_functions:>10} │
│ cursor.execute() calls              │ {total_executes:>10} │
├─────────────────────────────────────┼────────────┤
│ BOILERPLATE (LibCST will remove):   │            │
│   get_db_connection() calls         │ {total_connections:>10} │
│   cursor.close() calls             │ {total_cursor_close:>10} │
│   connection.close() calls          │ {total_conn_close:>10} │
│   connection.commit() calls         │ {total_commits:>10} │
│   connection.rollback() calls       │ {total_rollbacks:>10} │
│   ── Total boilerplate lines ──     │ {boilerplate_count:>10} │
├─────────────────────────────────────┼────────────┤
│ SQL COMPLEXITY INDICATORS:          │            │
│   Dynamic WHERE (conditions.append) │ {total_dynamic_where:>10} │
│   f-string SQL                      │ {total_f_string:>10} │
│   JOIN statements                   │ {total_joins:>10} │
│   CASE WHEN expressions             │ {total_case:>10} │
│   ON DUPLICATE KEY / UPSERT         │ {total_on_dup:>10} │
├─────────────────────────────────────┼────────────┤
│ FUNCTION COMPLEXITY:                │            │
│   Simple  (direct ORM conversion)   │ {complexity_dist['simple']:>10} │
│   Moderate (AI + review needed)     │ {complexity_dist['moderate']:>10} │
│   Complex (manual review required)  │ {complexity_dist['complex']:>10} │
└─────────────────────────────────────┴────────────┘
""")

    # Per-file table
    print("─" * 80)
    print("  PER-FILE BREAKDOWN")
    print("─" * 80)
    print(f"{'File':<45} {'Lines':>6} {'Exec':>5} {'Conn':>5} {'Dyn':>4} {'Level':<10}")
    print("─" * 80)

    # Sort by execute count descending
    for r in sorted(reports, key=lambda x: x.total_execute_calls, reverse=True):
        filename = Path(r.filepath).name
        if len(filename) > 44:
            filename = filename[:41] + "..."
        print(
            f"{filename:<45} {r.total_lines:>6} {r.total_execute_calls:>5} "
            f"{r.get_db_connection_calls:>5} {r.dynamic_where_count:>4} {r.overall_complexity:<10}"
        )

    print("─" * 80)

    # Estimation
    simple_hours = complexity_dist["simple"] * 0.1  # 6 min each
    moderate_hours = complexity_dist["moderate"] * 0.3  # 18 min each
    complex_hours = complexity_dist["complex"] * 0.5  # 30 min each
    total_hours = simple_hours + moderate_hours + complex_hours

    print(f"""
┌─────────────────────────────────────────────────────┐
│              ESTIMATED MIGRATION EFFORT              │
├─────────────────────────────────────────────────────┤
│ LibCST boilerplate removal:    ~{boilerplate_count:>5} changes (auto) │
│ AI-powered ORM conversion:     ~{total_executes:>5} queries        │
│ Manual review & fixes:         ~{total_hours:>5.1f} hours          │
│                                                     │
│ Estimated total time:                               │
│   With this tool:          {total_hours + 8:>5.0f} hours             │
│   Without this tool:       {total_hours * 4:>5.0f} hours             │
└─────────────────────────────────────────────────────┘
""")

"""
Safety Classifier — Categorize SQL functions by conversion risk.

Risk Levels:
  [PASS] SAFE        → Auto-convert with AI
  [WARN] REVIEW      → Convert but flag for human review
  [FAIL] SKIP        → Do NOT auto-convert, add TODO marker

Higher-risk patterns are held for human review instead of being changed
automatically.
"""
import re
from enum import Enum
from dataclasses import dataclass


class RiskLevel(Enum):
    SAFE = "safe"
    REVIEW = "review"
    SKIP = "skip"


@dataclass
class ClassificationResult:
    level: RiskLevel
    reason: str
    category: str  # dynamic_sql, financial, transaction, ddl, safe


# ── Detection patterns ──

_DYNAMIC_SQL_PATTERNS = [
    # f-string with SQL keywords
    (re.compile(r'f["\'].*(?:SELECT|INSERT|UPDATE|DELETE)', re.IGNORECASE), "f-string SQL"),
    # .format() with SQL
    (re.compile(r'\.format\(.*\).*(?:SELECT|INSERT|UPDATE|DELETE)', re.IGNORECASE | re.DOTALL), ".format() SQL"),
    # String concatenation with SQL
    (re.compile(r'(?:sql|query|stmt)\s*(?:\+|=\s*.*\+)', re.IGNORECASE), "SQL string concat"),
    # f-string table/column name injection
    (re.compile(r'f["\'].*\{.*(?:table|column|field)', re.IGNORECASE), "dynamic table/column"),
    # String substitution in a table position. Ordinary DB-API value
    # placeholders in WHERE clauses must not be classified as dynamic SQL.
    (
        re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO)\s+%s\b', re.IGNORECASE),
        "dynamic table via %s",
    ),
]

_FINANCIAL_KEYWORDS = re.compile(
    r'\b(?:total_amount|subtotal|tax_amount|grand_total|balance|'
    r'payment_amount|net_amount|discount_amount|refund_amount|'
    r'credit_amount|debit_amount)\b',
    re.IGNORECASE,
)

_CASE_WHEN = re.compile(r'CASE\s+WHEN', re.IGNORECASE)

_TRANSACTION_PATTERNS = [
    (re.compile(r'FOR\s+UPDATE', re.IGNORECASE), "row locking (FOR UPDATE)"),
    (re.compile(r'LOCK\s+(?:TABLE|IN)', re.IGNORECASE), "table locking"),
    (re.compile(r'isolation', re.IGNORECASE), "isolation level"),
    (re.compile(r'SAVEPOINT', re.IGNORECASE), "savepoint"),
]

_DDL_PATTERNS = re.compile(
    r'\b(?:CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE)\b',
    re.IGNORECASE,
)


def classify_function(func_source: str, func_name: str = "") -> ClassificationResult:
    """
    Classify a function's SQL conversion risk.

    Args:
        func_source: The function's source code.
        func_name: The function name (for logging).

    Returns:
        ClassificationResult with risk level, reason, and category.
    """
    # DDL — never convert
    if _DDL_PATTERNS.search(func_source):
        return ClassificationResult(
            level=RiskLevel.SKIP,
            reason="DDL/migration SQL (CREATE/ALTER/DROP TABLE)",
            category="ddl",
        )

    # Dynamic SQL — skip (AI will hallucinate model names)
    for pattern, desc in _DYNAMIC_SQL_PATTERNS:
        if pattern.search(func_source):
            return ClassificationResult(
                level=RiskLevel.SKIP,
                reason=f"Dynamic SQL detected: {desc}",
                category="dynamic_sql",
            )

    # Financial logic with CASE WHEN — flag for review
    if _CASE_WHEN.search(func_source) and _FINANCIAL_KEYWORDS.search(func_source):
        return ClassificationResult(
            level=RiskLevel.REVIEW,
            reason="Financial logic with CASE WHEN — verify business rules",
            category="financial",
        )

    # Transaction-sensitive patterns — flag for review
    for pattern, desc in _TRANSACTION_PATTERNS:
        if pattern.search(func_source):
            return ClassificationResult(
                level=RiskLevel.REVIEW,
                reason=f"Transaction-sensitive: {desc}",
                category="transaction",
            )

    # Safe — auto-convert
    return ClassificationResult(
        level=RiskLevel.SAFE,
        reason="Static SQL, safe to auto-convert",
        category="safe",
    )

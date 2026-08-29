from refactra_mysql.converter.safety_classifier import RiskLevel, classify_function


def test_static_sql_is_safe() -> None:
    result = classify_function('cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))')
    assert result.level is RiskLevel.SAFE


def test_dynamic_sql_is_skipped() -> None:
    result = classify_function('sql = f"SELECT * FROM {table_name}"')
    assert result.level is RiskLevel.SKIP


def test_dynamic_table_placeholder_is_skipped() -> None:
    result = classify_function('cursor.execute("SELECT * FROM %s" % table_name)')
    assert result.level is RiskLevel.SKIP


def test_transaction_sensitive_sql_requires_review() -> None:
    result = classify_function('cursor.execute("SELECT * FROM jobs FOR UPDATE")')
    assert result.level is RiskLevel.REVIEW

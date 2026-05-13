"""Tests for tools.sql.query_database.

These are integration tests: they hit the real Supabase via the agent_ro role.
They run against the loaded synthetic dataset (seed=42), so expected row
counts are stable.

Run with:
    uv run pytest tests/test_sql.py -v
"""

from __future__ import annotations

from tools.sql import MAX_ROWS, query_database


def test_simple_count_returns_one_row():
    r = query_database("SELECT COUNT(*) AS n FROM customers")
    assert r["error"] is None
    assert r["columns"] == ["n"]
    assert r["row_count"] == 1
    assert r["truncated"] is False
    assert r["rows"][0][0] > 0


def test_select_with_many_rows_truncates_at_cap():
    # product_events has ~50k rows; far over MAX_ROWS.
    r = query_database("SELECT id FROM product_events ORDER BY id")
    assert r["error"] is None
    assert r["row_count"] == MAX_ROWS
    assert r["truncated"] is True


def test_select_with_few_rows_does_not_truncate():
    r = query_database("SELECT * FROM company_events ORDER BY date LIMIT 50")
    assert r["error"] is None
    assert r["row_count"] <= 50
    assert r["truncated"] is False


def test_write_attempts_are_rejected_by_role():
    r = query_database("DELETE FROM customers WHERE id = 1")
    assert r["error"] is not None
    assert "InsufficientPrivilege" in r["error"]


def test_syntax_error_is_returned_not_raised():
    r = query_database("SELECT * FORM customers")  # typo
    assert r["error"] is not None
    assert r["columns"] == []
    assert r["rows"] == []


def test_dates_are_iso_strings_in_rows():
    r = query_database("SELECT signup_date FROM customers ORDER BY signup_date LIMIT 1")
    assert r["error"] is None
    assert r["row_count"] == 1
    val = r["rows"][0][0]
    # date columns serialize as ISO strings (YYYY-MM-DD)
    assert isinstance(val, str)
    assert len(val) == 10
    assert val[4] == "-" and val[7] == "-"


def test_numeric_columns_serialize_as_floats():
    r = query_database("SELECT mrr_usd FROM subscriptions WHERE mrr_usd > 0 LIMIT 1")
    assert r["error"] is None
    assert r["row_count"] == 1
    val = r["rows"][0][0]
    # Decimal columns get coerced to float by our _to_json_safe helper
    assert isinstance(val, float)
    assert val > 0

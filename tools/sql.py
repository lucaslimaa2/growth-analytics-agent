"""SQL query tool. Runs read-only SQL against Supabase via the agent_ro role.

Used by the agent loop (Phase 4 onward) for ad-hoc questions that don't fit
the pre-built metric tools.

Run directly to execute a SQL query:
    uv run python -m tools.sql "SELECT COUNT(*) FROM customers"
"""

from __future__ import annotations

import datetime
import os
import sys
from decimal import Decimal
from typing import Any, TypedDict

import psycopg
from dotenv import load_dotenv

load_dotenv()

MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 10_000


class QueryResult(TypedDict):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    error: str | None


def _to_json_safe(v: Any) -> Any:
    """Convert types JSON can't serialize natively."""
    if v is None:
        return None
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def query_database(sql: str) -> QueryResult:
    """Run a read-only SQL query and return up to MAX_ROWS rows.

    The connection uses the agent_ro Postgres role which has SELECT-only
    privileges. Any DDL or DML (INSERT, UPDATE, DELETE, DROP, etc.) is
    rejected by Postgres with InsufficientPrivilege. A statement timeout
    of STATEMENT_TIMEOUT_MS milliseconds applies to every query.

    Returns a dict shaped:
        {
            "columns":   ["col1", ...],
            "rows":      [[v1, v2, ...], ...],   # JSON-safe values
            "row_count": int,                     # rows actually returned
            "truncated": bool,                    # True if the result had >MAX_ROWS rows
            "error":     str | None,
        }
    """
    url = os.environ.get("SUPABASE_DB_URL_READONLY")
    if not url:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "error": "SUPABASE_DB_URL_READONLY not set",
        }

    try:
        with psycopg.connect(url, connect_timeout=15, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(sql)

                if cur.description is None:
                    return {
                        "columns": [],
                        "rows": [],
                        "row_count": 0,
                        "truncated": False,
                        "error": None,
                    }

                columns = [d[0] for d in cur.description]
                fetched = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(fetched) > MAX_ROWS
                rows = [
                    [_to_json_safe(v) for v in row]
                    for row in fetched[:MAX_ROWS]
                ]
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                    "error": None,
                }
    except psycopg.Error as e:
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else ""
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "error": f"{type(e).__name__}: {first_line}",
        }


def format_as_markdown(result: QueryResult) -> str:
    """Format a QueryResult as a markdown table with a row-count footer."""
    if result["error"]:
        return f"**Error:** {result['error']}"
    if not result["columns"]:
        return "*(no result set)*"

    cols = result["columns"]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for row in result["rows"]:
        cells = ["" if v is None else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    footer = f"\n*{result['row_count']} rows" + (" (truncated at 500)*" if result["truncated"] else "*")
    return "\n".join(lines) + footer


if __name__ == "__main__":
    sql = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "SELECT current_user, COUNT(*) AS n_customers FROM customers"
    print(f"SQL: {sql}\n")
    result = query_database(sql)
    print(format_as_markdown(result))

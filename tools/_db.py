"""Shared read-only Postgres connection helper for agent tools.

Every tool that reads from Supabase routes through this module so the
connection setup (env loading, override, statement timeout, prepare
threshold disabled for the transaction pooler) lives in one place.

Usage:
    from tools._db import readonly_cursor

    with readonly_cursor() as cur:
        cur.execute("SELECT 1")
        rows = cur.fetchall()
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

STATEMENT_TIMEOUT_MS = 10_000


@contextmanager
def readonly_cursor() -> Iterator[psycopg.Cursor]:
    """Yield a cursor on a fresh read-only connection.

    Uses SUPABASE_DB_URL_READONLY (agent_ro role, SELECT-only). Applies a
    statement timeout so a runaway query can't hang the agent loop.
    """
    url = os.environ.get("SUPABASE_DB_URL_READONLY")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL_READONLY not set in environment")
    with psycopg.connect(url, connect_timeout=15, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            yield cur

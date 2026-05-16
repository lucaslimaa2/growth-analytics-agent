"""Per-IP rate limiting backed by Postgres.

Each request inserts a row into `rate_limits`. Before allowing a request, we
count this IP's rows in the last RATE_LIMIT_WINDOW_SECONDS. If that count
exceeds RATE_LIMIT_MAX, the request is rejected with the seconds-to-retry.

Connection: SUPABASE_DB_URL_POOLED (postgres role, write access). The agent's
own queries continue to use the read-only role (SUPABASE_DB_URL_READONLY).

Storage cleanup: rows older than the window are not actively pruned per-call
(would add latency). A periodic prune runs on a small percentage of requests
to keep the table compact.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX = 10  # requests per window per IP
PRUNE_PROBABILITY = 0.02  # 2% of requests trigger a cleanup


class RateLimitResult:
    __slots__ = ("allowed", "count", "retry_after")

    def __init__(self, allowed: bool, count: int, retry_after: int = 0) -> None:
        self.allowed = allowed
        self.count = count
        self.retry_after = retry_after


def check_rate_limit(ip: str) -> RateLimitResult:
    """Check and record a request for `ip`. Returns whether it's allowed.

    On any DB error we fail OPEN (allow the request). Rate limiting is a
    convenience, not a correctness gate — never break the app to enforce it.
    """
    url = os.environ.get("SUPABASE_DB_URL_POOLED")
    if not url:
        return RateLimitResult(True, 0)

    try:
        with psycopg.connect(url, connect_timeout=5, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM rate_limits "
                    "WHERE ip = %s AND ts > NOW() - make_interval(secs => %s)",
                    (ip, RATE_LIMIT_WINDOW_SECONDS),
                )
                count = int(cur.fetchone()[0])

                if count >= RATE_LIMIT_MAX:
                    # Find when the oldest request in the window falls out, that's our retry hint.
                    cur.execute(
                        "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(ts))) "
                        "FROM rate_limits "
                        "WHERE ip = %s AND ts > NOW() - make_interval(secs => %s)",
                        (ip, RATE_LIMIT_WINDOW_SECONDS),
                    )
                    age_sec = float(cur.fetchone()[0] or 0)
                    retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - age_sec) + 1)
                    return RateLimitResult(False, count, retry_after)

                cur.execute("INSERT INTO rate_limits (ip) VALUES (%s)", (ip,))

                if random.random() < PRUNE_PROBABILITY:
                    cur.execute(
                        "DELETE FROM rate_limits WHERE ts < NOW() - make_interval(secs => %s)",
                        (RATE_LIMIT_WINDOW_SECONDS * 60,),
                    )
            conn.commit()
        return RateLimitResult(True, count + 1)
    except Exception:  # noqa: BLE001
        # Fail open: a rate-limit infra outage must not block real users.
        return RateLimitResult(True, 0)

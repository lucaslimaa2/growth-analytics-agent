"""time_series_compare: period-over-period comparison of a metric.

Returns the totals for two periods, the delta, the percent change, and
(optionally) which segment drove the change when a breakdown dimension is
requested.

Supported metrics (each maps to a SQL recipe):
    mrr_end           - active MRR at end of month, optionally by plan
    new_signups       - count of customers who signed up that month, optionally by channel/country/plan
    new_activations   - count of customers who activated that month, optionally by channel
    new_paying        - count of customers who started their first paid sub that month, optionally by channel/plan
    churn_mrr         - MRR lost to churn that month, optionally by end_reason/plan

Period must be "YYYY-MM" format (single month).

Run directly to test:
    uv run python -m tools.time_series mrr_end 2025-04 2025-05
    uv run python -m tools.time_series new_signups 2025-12 2025-11 channel
"""

from __future__ import annotations

import sys
from typing import TypedDict

from tools._db import readonly_cursor

# (metric_name, breakdown_dim or None) -> SQL template producing rows of (segment, value)
# Template uses %s placeholders for the period (YYYY-MM).
METRIC_TOTAL_SQL: dict[str, str] = {
    "mrr_end": """
        SELECT NULL::text AS segment, SUM(s.mrr_usd) AS value
        FROM subscriptions s
        WHERE s.mrr_usd > 0
          AND s.started_at <= (date_trunc('month', to_date(%s, 'YYYY-MM')) + interval '1 month - 1 day')::date
          AND (s.ended_at IS NULL OR s.ended_at > (date_trunc('month', to_date(%s, 'YYYY-MM')) + interval '1 month - 1 day')::date)
    """,
    "new_signups": """
        SELECT NULL::text AS segment, COUNT(*) AS value
        FROM customers
        WHERE TO_CHAR(signup_date, 'YYYY-MM') = %s
    """,
    "new_activations": """
        SELECT NULL::text AS segment, COUNT(*) AS value
        FROM customers
        WHERE activated_date IS NOT NULL
          AND TO_CHAR(activated_date, 'YYYY-MM') = %s
    """,
    "new_paying": """
        SELECT NULL::text AS segment, COUNT(*) AS value
        FROM (
            SELECT customer_id, MIN(started_at) AS first_started
            FROM subscriptions WHERE mrr_usd > 0
            GROUP BY customer_id
        ) fp
        WHERE TO_CHAR(first_started, 'YYYY-MM') = %s
    """,
    "churn_mrr": """
        SELECT NULL::text AS segment, COALESCE(SUM(mrr_usd), 0) AS value
        FROM subscriptions
        WHERE end_reason IN ('voluntary', 'involuntary_payment', 'price_increase', 'outage_followup')
          AND TO_CHAR(ended_at, 'YYYY-MM') = %s
    """,
}

# Allowed (metric, dim) pairs for the breakdown path
BREAKDOWN_SQL: dict[tuple[str, str], str] = {
    ("mrr_end", "plan"): """
        SELECT s.plan AS segment, SUM(s.mrr_usd) AS value
        FROM subscriptions s
        WHERE s.mrr_usd > 0
          AND s.started_at <= (date_trunc('month', to_date(%s, 'YYYY-MM')) + interval '1 month - 1 day')::date
          AND (s.ended_at IS NULL OR s.ended_at > (date_trunc('month', to_date(%s, 'YYYY-MM')) + interval '1 month - 1 day')::date)
        GROUP BY s.plan
    """,
    ("new_signups", "channel"): """
        SELECT channel AS segment, COUNT(*) AS value
        FROM customers
        WHERE TO_CHAR(signup_date, 'YYYY-MM') = %s
        GROUP BY channel
    """,
    ("new_signups", "country"): """
        SELECT country AS segment, COUNT(*) AS value
        FROM customers
        WHERE TO_CHAR(signup_date, 'YYYY-MM') = %s
        GROUP BY country
    """,
    ("new_signups", "initial_plan"): """
        SELECT initial_plan AS segment, COUNT(*) AS value
        FROM customers
        WHERE TO_CHAR(signup_date, 'YYYY-MM') = %s
        GROUP BY initial_plan
    """,
    ("new_activations", "channel"): """
        SELECT channel AS segment, COUNT(*) AS value
        FROM customers
        WHERE activated_date IS NOT NULL AND TO_CHAR(activated_date, 'YYYY-MM') = %s
        GROUP BY channel
    """,
    ("new_paying", "channel"): """
        SELECT c.channel AS segment, COUNT(*) AS value
        FROM (
            SELECT customer_id, MIN(started_at) AS first_started
            FROM subscriptions WHERE mrr_usd > 0
            GROUP BY customer_id
        ) fp
        JOIN customers c ON c.id = fp.customer_id
        WHERE TO_CHAR(fp.first_started, 'YYYY-MM') = %s
        GROUP BY c.channel
    """,
    ("churn_mrr", "end_reason"): """
        SELECT end_reason AS segment, SUM(mrr_usd) AS value
        FROM subscriptions
        WHERE end_reason IN ('voluntary', 'involuntary_payment', 'price_increase', 'outage_followup')
          AND TO_CHAR(ended_at, 'YYYY-MM') = %s
        GROUP BY end_reason
    """,
    ("churn_mrr", "plan"): """
        SELECT plan AS segment, SUM(mrr_usd) AS value
        FROM subscriptions
        WHERE end_reason IN ('voluntary', 'involuntary_payment', 'price_increase', 'outage_followup')
          AND TO_CHAR(ended_at, 'YYYY-MM') = %s
        GROUP BY plan
    """,
}


class SegmentDelta(TypedDict):
    segment: str | None
    value_a: float
    value_b: float
    delta: float
    delta_pct: float | None


class CompareResult(TypedDict):
    metric: str
    period_a: str
    period_b: str
    breakdown_by: str | None
    total_a: float
    total_b: float
    delta: float
    delta_pct: float | None
    by_segment: list[SegmentDelta]
    error: str | None


def time_series_compare(
    metric: str,
    period_a: str,
    period_b: str,
    breakdown_by: str | None = None,
) -> CompareResult:
    """Compare a metric between two months; optionally break down by a dimension.

    Args:
        metric: one of METRIC_TOTAL_SQL keys (mrr_end, new_signups, etc.).
        period_a, period_b: "YYYY-MM" strings.
        breakdown_by: optional dimension name. Must be a supported (metric, dim) pair.
    """
    if metric not in METRIC_TOTAL_SQL:
        return _error(
            metric,
            period_a,
            period_b,
            breakdown_by,
            f"unknown metric: {metric}. Supported: {sorted(METRIC_TOTAL_SQL)}",
        )
    if breakdown_by is not None and (metric, breakdown_by) not in BREAKDOWN_SQL:
        return _error(
            metric,
            period_a,
            period_b,
            breakdown_by,
            f"breakdown_by={breakdown_by} not supported for metric={metric}. "
            f"Supported pairs: {sorted(BREAKDOWN_SQL.keys())}",
        )

    try:
        with readonly_cursor() as cur:
            total_a = _scalar_total(cur, metric, period_a)
            total_b = _scalar_total(cur, metric, period_b)

            by_segment: list[SegmentDelta] = []
            if breakdown_by is not None:
                a_map = _segmented_totals(cur, metric, breakdown_by, period_a)
                b_map = _segmented_totals(cur, metric, breakdown_by, period_b)
                all_segments = set(a_map) | set(b_map)
                for seg in sorted(all_segments, key=lambda s: (s is None, s or "")):
                    va = a_map.get(seg, 0.0)
                    vb = b_map.get(seg, 0.0)
                    by_segment.append(
                        {
                            "segment": seg,
                            "value_a": round(va, 2),
                            "value_b": round(vb, 2),
                            "delta": round(vb - va, 2),
                            "delta_pct": _pct_change(va, vb),
                        }
                    )
                by_segment.sort(key=lambda r: abs(r["delta"]), reverse=True)
    except Exception as exc:  # noqa: BLE001
        return _error(metric, period_a, period_b, breakdown_by, f"{type(exc).__name__}: {exc}")

    return {
        "metric": metric,
        "period_a": period_a,
        "period_b": period_b,
        "breakdown_by": breakdown_by,
        "total_a": round(total_a, 2),
        "total_b": round(total_b, 2),
        "delta": round(total_b - total_a, 2),
        "delta_pct": _pct_change(total_a, total_b),
        "by_segment": by_segment,
        "error": None,
    }


def _scalar_total(cur, metric: str, period: str) -> float:
    sql = METRIC_TOTAL_SQL[metric]
    n_params = sql.count("%s")
    cur.execute(sql, (period,) * n_params)
    row = cur.fetchone()
    return float(row[1]) if row and row[1] is not None else 0.0


def _segmented_totals(cur, metric: str, dim: str, period: str) -> dict[str | None, float]:
    sql = BREAKDOWN_SQL[(metric, dim)]
    n_params = sql.count("%s")
    cur.execute(sql, (period,) * n_params)
    return {
        (str(r[0]) if r[0] is not None else None): float(r[1]) if r[1] is not None else 0.0
        for r in cur.fetchall()
    }


def _pct_change(a: float, b: float) -> float | None:
    if a == 0:
        return None
    return round((b - a) / a * 100, 2)


def _error(
    metric: str, period_a: str, period_b: str, breakdown_by: str | None, msg: str
) -> CompareResult:
    return {
        "metric": metric,
        "period_a": period_a,
        "period_b": period_b,
        "breakdown_by": breakdown_by,
        "total_a": 0.0,
        "total_b": 0.0,
        "delta": 0.0,
        "delta_pct": None,
        "by_segment": [],
        "error": msg,
    }


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: tools.time_series METRIC PERIOD_A PERIOD_B [BREAKDOWN_BY]")
        sys.exit(1)
    metric = sys.argv[1]
    period_a = sys.argv[2]
    period_b = sys.argv[3]
    breakdown = sys.argv[4] if len(sys.argv) > 4 else None
    r = time_series_compare(metric, period_a, period_b, breakdown)
    print(
        f"metric={r['metric']} {r['period_a']} -> {r['period_b']} breakdown_by={r['breakdown_by']}"
    )
    if r["error"]:
        print(f"error: {r['error']}")
    else:
        print(
            f"  total: {r['total_a']} -> {r['total_b']}  delta={r['delta']}  delta_pct={r['delta_pct']}%"
        )
        for seg in r["by_segment"]:
            print(
                f"    {seg['segment']!s:14s}: {seg['value_a']!s:>10} -> {seg['value_b']!s:>10}  "
                f"delta={seg['delta']!s:>10}  ({seg['delta_pct']}%)"
            )

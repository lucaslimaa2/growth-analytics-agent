"""get_metric: canonical lookup for the company's growth metrics.

Instead of asking Claude to derive MRR / churn rate / ARPU from raw tables
every time, we expose a fixed catalog of metrics with their formulas locked
in code. This guarantees the same definition is used across every question.

Supported metric names:
    mrr               - monthly recurring revenue (USD)
    arr               - annualized recurring revenue (mrr * 12)
    mom_growth        - month-over-month MRR growth (percent)
    arpu              - average revenue per paying user (USD)
    gross_churn_rate  - gross MRR churn rate (percent)
    net_churn_rate    - net MRR churn rate (percent, can be negative)
    paying_customers  - count of active paying customers at end of month
    new_paying        - new paying customers this month
    ltv               - lifetime value (USD), computed as arpu / gross_churn_rate

Period can be:
    None              - return all 24 months
    "YYYY-MM"         - return that single month
    ("YYYY-MM", "YYYY-MM")  - inclusive range
    "latest"          - return only the most recent month

Run directly to test:
    uv run python -m tools.metrics mrr latest
    uv run python -m tools.metrics gross_churn_rate
"""

from __future__ import annotations

import sys
from typing import Any, TypedDict

from tools._db import readonly_cursor

# Map metric name to the monthly_metrics column it lives in, or None for derived metrics.
DIRECT_COLUMNS: dict[str, str] = {
    "mrr": "mrr_end",
    "arr": "arr",
    "mom_growth": "mom_growth_pct",
    "arpu": "arpu",
    "gross_churn_rate": "gross_churn_rate",
    "net_churn_rate": "net_churn_rate",
    "paying_customers": "paying_customers",
}

DERIVED = {"ltv", "new_paying"}

ALL_METRICS = set(DIRECT_COLUMNS) | DERIVED


class MetricPoint(TypedDict):
    month: str
    value: float | None


class MetricResult(TypedDict):
    metric: str
    unit: str
    points: list[MetricPoint]
    error: str | None


_UNITS: dict[str, str] = {
    "mrr": "USD",
    "arr": "USD",
    "mom_growth": "percent",
    "arpu": "USD",
    "gross_churn_rate": "percent",
    "net_churn_rate": "percent",
    "paying_customers": "count",
    "new_paying": "count",
    "ltv": "USD",
}


def _normalize_period(period: str | tuple[str, str] | None) -> tuple[str | None, str | None]:
    """Return (start_month, end_month) inclusive, or (None, None) for all months."""
    if period is None:
        return None, None
    if isinstance(period, tuple):
        return period[0], period[1]
    if period == "latest":
        return "latest", "latest"
    return period, period


def get_metric(name: str, period: str | tuple[str, str] | None = None) -> MetricResult:
    """Return the metric values for the given period(s).

    Output shape:
        {
            "metric": "mrr",
            "unit": "USD",
            "points": [{"month": "2024-06", "value": 5713.0}, ...],
            "error": None,
        }
    """
    if name not in ALL_METRICS:
        return {
            "metric": name,
            "unit": "",
            "points": [],
            "error": f"unknown metric: {name}. Supported: {sorted(ALL_METRICS)}",
        }

    start, end = _normalize_period(period)

    try:
        with readonly_cursor() as cur:
            if name in DIRECT_COLUMNS:
                col = DIRECT_COLUMNS[name]
                sql, args = _build_direct_sql(col, start, end)
                cur.execute(sql, args)
                points = [
                    {"month": r[0], "value": float(r[1]) if r[1] is not None else None}
                    for r in cur.fetchall()
                ]
            elif name == "new_paying":
                points = _compute_new_paying(cur, start, end)
            elif name == "ltv":
                points = _compute_ltv(cur, start, end)
            else:
                points = []
    except Exception as exc:  # noqa: BLE001
        return {
            "metric": name,
            "unit": _UNITS.get(name, ""),
            "points": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "metric": name,
        "unit": _UNITS.get(name, ""),
        "points": points,
        "error": None,
    }


def _build_direct_sql(
    column: str, start: str | None, end: str | None
) -> tuple[str, tuple[Any, ...]]:
    if start == "latest" or end == "latest":
        return (
            f"SELECT month, {column} FROM monthly_metrics ORDER BY month DESC LIMIT 1",
            (),
        )
    if start and end and start == end:
        return (
            f"SELECT month, {column} FROM monthly_metrics WHERE month = %s",
            (start,),
        )
    if start and end:
        return (
            f"SELECT month, {column} FROM monthly_metrics WHERE month BETWEEN %s AND %s ORDER BY month",
            (start, end),
        )
    return (f"SELECT month, {column} FROM monthly_metrics ORDER BY month", ())


def _compute_new_paying(cur: Any, start: str | None, end: str | None) -> list[MetricPoint]:
    """Count of customers whose first paid subscription started in each month."""
    where = ""
    args: tuple[Any, ...] = ()
    if start == "latest":
        # Latest month present in monthly_metrics
        cur.execute("SELECT MAX(month) FROM monthly_metrics")
        latest = cur.fetchone()[0]
        where = "WHERE TO_CHAR(first_started, 'YYYY-MM') = %s"
        args = (latest,)
    elif start and end and start == end:
        where = "WHERE TO_CHAR(first_started, 'YYYY-MM') = %s"
        args = (start,)
    elif start and end:
        where = "WHERE TO_CHAR(first_started, 'YYYY-MM') BETWEEN %s AND %s"
        args = (start, end)

    cur.execute(
        f"""
        WITH first_paid AS (
            SELECT customer_id, MIN(started_at) AS first_started
            FROM subscriptions
            WHERE mrr_usd > 0
            GROUP BY customer_id
        )
        SELECT TO_CHAR(first_started, 'YYYY-MM') AS month, COUNT(*) AS new_paying
        FROM first_paid
        {where}
        GROUP BY month
        ORDER BY month
        """,
        args,
    )
    return [{"month": r[0], "value": float(r[1])} for r in cur.fetchall()]


def _compute_ltv(cur: Any, start: str | None, end: str | None) -> list[MetricPoint]:
    """LTV = ARPU / gross_churn_rate (per month).

    Returns a small history. When gross_churn_rate is 0 or NULL, LTV is None.
    """
    where = ""
    args: tuple[Any, ...] = ()
    if start == "latest":
        where = "WHERE month = (SELECT MAX(month) FROM monthly_metrics)"
    elif start and end and start == end:
        where = "WHERE month = %s"
        args = (start,)
    elif start and end:
        where = "WHERE month BETWEEN %s AND %s"
        args = (start, end)

    cur.execute(
        f"""
        SELECT month, arpu, gross_churn_rate
        FROM monthly_metrics
        {where}
        ORDER BY month
        """,
        args,
    )
    points: list[MetricPoint] = []
    for month, arpu, churn_rate in cur.fetchall():
        if churn_rate is None or float(churn_rate) <= 0:
            ltv: float | None = None
        else:
            # churn_rate is in percent; convert to fraction
            ltv = round(float(arpu) / (float(churn_rate) / 100), 2)
        points.append({"month": month, "value": ltv})
    return points


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "mrr"
    period: str | None = sys.argv[2] if len(sys.argv) > 2 else None
    result = get_metric(name, period)
    print(f"metric: {result['metric']} ({result['unit']})")
    if result["error"]:
        print(f"error:  {result['error']}")
    else:
        for p in result["points"]:
            v = p["value"]
            print(f"  {p['month']}: {v}")

"""segment_breakdown: split a metric by a dimension for a single period.

Like time_series_compare but for a single point in time. Reuses the same
(metric, dimension) SQL recipes so output is consistent across tools.

Supported (metric, dimension) pairs are defined in tools.time_series.BREAKDOWN_SQL,
plus a couple of standalone segment-only ones below.

Run directly to test:
    uv run python -m tools.segment new_signups channel 2025-12
    uv run python -m tools.segment churn_mrr plan 2026-03
"""

from __future__ import annotations

import sys
from typing import TypedDict

from tools._db import readonly_cursor
from tools.time_series import BREAKDOWN_SQL


class SegmentValue(TypedDict):
    segment: str | None
    value: float


class SegmentResult(TypedDict):
    metric: str
    dimension: str
    period: str
    rows: list[SegmentValue]
    total: float
    error: str | None


def segment_breakdown(metric: str, dimension: str, period: str) -> SegmentResult:
    """Break a metric down by a dimension for the given period.

    Args:
        metric: e.g., "new_signups", "mrr_end", "churn_mrr".
        dimension: e.g., "channel", "country", "plan", "end_reason".
        period: "YYYY-MM" string.
    """
    if (metric, dimension) not in BREAKDOWN_SQL:
        return {
            "metric": metric,
            "dimension": dimension,
            "period": period,
            "rows": [],
            "total": 0.0,
            "error": (
                f"unsupported (metric={metric}, dimension={dimension}) pair. "
                f"Supported: {sorted(BREAKDOWN_SQL.keys())}"
            ),
        }

    sql = BREAKDOWN_SQL[(metric, dimension)]
    n_params = sql.count("%s")

    try:
        with readonly_cursor() as cur:
            cur.execute(sql, (period,) * n_params)
            data = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return {
            "metric": metric,
            "dimension": dimension,
            "period": period,
            "rows": [],
            "total": 0.0,
            "error": f"{type(exc).__name__}: {exc}",
        }

    rows: list[SegmentValue] = []
    for segment, value in data:
        rows.append(
            {
                "segment": str(segment) if segment is not None else None,
                "value": round(float(value), 2) if value is not None else 0.0,
            }
        )
    rows.sort(key=lambda r: r["value"], reverse=True)
    total = round(sum(r["value"] for r in rows), 2)

    return {
        "metric": metric,
        "dimension": dimension,
        "period": period,
        "rows": rows,
        "total": total,
        "error": None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: tools.segment METRIC DIMENSION PERIOD")
        sys.exit(1)
    metric = sys.argv[1]
    dimension = sys.argv[2]
    period = sys.argv[3]
    r = segment_breakdown(metric, dimension, period)
    print(f"{r['metric']} by {r['dimension']} for {r['period']}  total={r['total']}")
    if r["error"]:
        print(f"error: {r['error']}")
    else:
        for row in r["rows"]:
            pct = (row["value"] / r["total"] * 100) if r["total"] else 0
            print(f"  {row['segment']!s:18s}: {row['value']:>12,.2f}  ({pct:5.1f}%)")

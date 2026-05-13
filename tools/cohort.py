"""cohort_analysis: retention curve by signup-paid-month cohort.

Defaults to reading the `cohort_retention` rollup table (fast path).
If a segment dimension is requested, recomputes live from raw tables
joining customers + subscriptions so we can split each cohort by
channel, country, or initial_plan.

Run directly to test:
    uv run python -m tools.cohort
    uv run python -m tools.cohort channel
    uv run python -m tools.cohort initial_plan 6
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Literal, TypedDict

import pandas as pd

from tools._db import readonly_cursor

SegmentDim = Literal["channel", "country", "initial_plan", "company_size", "industry"]
SUPPORTED_SEGMENTS: set[str] = {"channel", "country", "initial_plan", "company_size", "industry"}


class CohortRow(TypedDict):
    cohort_month: str
    age_months: int
    segment: str | None
    retained_pct: float
    surviving_mrr: float


class CohortResult(TypedDict):
    cohort_by: str
    segment_by: str | None
    window_months: int
    rows: list[CohortRow]
    error: str | None


def cohort_analysis(
    cohort_by: Literal["first_paid_month"] = "first_paid_month",
    segment_by: SegmentDim | None = None,
    window_months: int = 12,
) -> CohortResult:
    """Return cohort retention rows (one per cohort_month × age_months × segment).

    Args:
        cohort_by: how to define the cohort. Only "first_paid_month" supported.
        segment_by: optional customer dimension to split each cohort by.
        window_months: cap on age_months (typical: 12).
    """
    if segment_by is not None and segment_by not in SUPPORTED_SEGMENTS:
        return {
            "cohort_by": cohort_by,
            "segment_by": segment_by,
            "window_months": window_months,
            "rows": [],
            "error": f"unsupported segment dimension: {segment_by}. Supported: {sorted(SUPPORTED_SEGMENTS)}",
        }

    try:
        with readonly_cursor() as cur:
            if segment_by is None:
                rows = _query_rollup(cur, window_months)
            else:
                rows = _compute_segmented(cur, segment_by, window_months)
    except Exception as exc:  # noqa: BLE001
        return {
            "cohort_by": cohort_by,
            "segment_by": segment_by,
            "window_months": window_months,
            "rows": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "cohort_by": cohort_by,
        "segment_by": segment_by,
        "window_months": window_months,
        "rows": rows,
        "error": None,
    }


def _query_rollup(cur, window_months: int) -> list[CohortRow]:
    cur.execute(
        """
        SELECT cohort_month, age_months, retained_pct, surviving_mrr
        FROM cohort_retention
        WHERE age_months <= %s
        ORDER BY cohort_month, age_months
        """,
        (window_months,),
    )
    return [
        {
            "cohort_month": r[0],
            "age_months": int(r[1]),
            "segment": None,
            "retained_pct": float(r[2]),
            "surviving_mrr": float(r[3]),
        }
        for r in cur.fetchall()
    ]


def _compute_segmented(cur, segment_by: str, window_months: int) -> list[CohortRow]:
    """Recompute cohort retention split by a customer dimension.

    Loads paid subscriptions + customer segment into pandas and does the cohort
    math in-memory. Much faster than a correlated SQL query for our row counts
    (~5k customers, ~3k subs).
    """
    cur.execute(
        f"""
        SELECT s.customer_id, s.mrr_usd, s.started_at, s.ended_at, c.{segment_by} AS segment
        FROM subscriptions s
        JOIN customers c ON c.id = s.customer_id
        WHERE s.mrr_usd > 0
        """
    )
    subs_data = cur.fetchall()
    if not subs_data:
        return []

    subs = pd.DataFrame(
        subs_data, columns=["customer_id", "mrr_usd", "started_at", "ended_at", "segment"]
    )
    subs["mrr_usd"] = subs["mrr_usd"].astype(float)

    # First paid subscription per customer defines cohort_month + segment.
    first_paid = subs.sort_values("started_at").groupby("customer_id", as_index=False).first()
    first_paid["cohort_month"] = pd.to_datetime(first_paid["started_at"]).dt.strftime("%Y-%m")
    first_paid = first_paid[["customer_id", "cohort_month", "segment"]]

    # Cohort sizes per (cohort_month, segment).
    cohort_size = first_paid.groupby(["cohort_month", "segment"]).size().reset_index(name="size")

    rows: list[CohortRow] = []
    for _, cohort_row in cohort_size.iterrows():
        cohort_month = cohort_row["cohort_month"]
        segment = cohort_row["segment"]
        size = int(cohort_row["size"])
        cohort_customers = first_paid[
            (first_paid["cohort_month"] == cohort_month) & (first_paid["segment"] == segment)
        ]["customer_id"].tolist()
        cohort_subs = subs[subs["customer_id"].isin(cohort_customers)]

        cohort_start = date.fromisoformat(f"{cohort_month}-01")
        for age in range(window_months + 1):
            check = _month_end(cohort_start, age)
            active_mask = (cohort_subs["started_at"] <= check) & (
                cohort_subs["ended_at"].isna() | (cohort_subs["ended_at"] > check)
            )
            active = cohort_subs[active_mask]
            retained = int(active["customer_id"].nunique())
            # For customers with multiple active subs on the check date (overlapping
            # records), take the most recent started_at as the current plan MRR.
            if not active.empty:
                latest_per_customer = (
                    active.sort_values("started_at").groupby("customer_id", as_index=False).last()
                )
                surviving_mrr = float(latest_per_customer["mrr_usd"].sum())
            else:
                surviving_mrr = 0.0

            rows.append(
                {
                    "cohort_month": cohort_month,
                    "age_months": age,
                    "segment": str(segment) if segment is not None else None,
                    "retained_pct": round(retained / size * 100, 2),
                    "surviving_mrr": round(surviving_mrr, 2),
                }
            )

    rows.sort(key=lambda r: (r["cohort_month"], r["segment"] or "", r["age_months"]))
    return rows


def _month_end(start: date, age_months: int) -> date:
    """Return the last day of (start + age_months)."""
    y = start.year + (start.month - 1 + age_months) // 12
    m = (start.month - 1 + age_months) % 12 + 1
    next_y = y + (m // 12)
    next_m = (m % 12) + 1
    return date(next_y, next_m, 1) - timedelta(days=1)


if __name__ == "__main__":
    segment = sys.argv[1] if len(sys.argv) > 1 else None
    window = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    result = cohort_analysis(segment_by=segment, window_months=window)
    print(
        f"cohort_by={result['cohort_by']} segment_by={result['segment_by']} window={result['window_months']}"
    )
    if result["error"]:
        print(f"error: {result['error']}")
    else:
        for r in result["rows"][:25]:
            seg = f" [{r['segment']}]" if r["segment"] else ""
            print(
                f"  {r['cohort_month']}{seg} age={r['age_months']}: {r['retained_pct']}% retained, ${r['surviving_mrr']:.0f} mrr"
            )
        if len(result["rows"]) > 25:
            print(f"  ... {len(result['rows']) - 25} more rows")

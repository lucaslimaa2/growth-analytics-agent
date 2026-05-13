"""funnel_analysis: conversion rates between a sequence of lifecycle steps.

Steps are evaluated in the temporal order they appear in the input list:
a customer counts at step N only if they hit every earlier step first
(timestamps in non-decreasing order).

Supported step names:
    signup            - customers.signup_date
    activated         - customers.activated_date (NOT NULL)
    first_paid        - earliest subscriptions.started_at with mrr_usd > 0
    page_view, feature_use, report_export, data_import,
    team_invite, billing_view, dashboard_load, api_call
                      - first occurrence in product_events

Optional `segment_by` splits the funnel by a customer column.

Run directly to test:
    uv run python -m tools.funnel
    uv run python -m tools.funnel channel
"""

from __future__ import annotations

import sys
from typing import Literal, TypedDict

import pandas as pd

from tools._db import readonly_cursor

SegmentDim = Literal["channel", "country", "initial_plan", "company_size", "industry"]
SUPPORTED_SEGMENTS: set[str] = {"channel", "country", "initial_plan", "company_size", "industry"}

# Step name -> SQL fragment returning (customer_id, event_at) for first occurrence.
STEP_QUERIES: dict[str, str] = {
    "signup": "SELECT id AS customer_id, signup_date::timestamp AS event_at FROM customers",
    "activated": "SELECT id AS customer_id, activated_date::timestamp AS event_at FROM customers WHERE activated_date IS NOT NULL",
    "first_paid": """
        SELECT customer_id, MIN(started_at)::timestamp AS event_at
        FROM subscriptions WHERE mrr_usd > 0
        GROUP BY customer_id
    """,
}
PRODUCT_EVENT_NAMES = {
    "page_view",
    "feature_use",
    "report_export",
    "data_import",
    "team_invite",
    "billing_view",
    "dashboard_load",
    "api_call",
}


class FunnelStep(TypedDict):
    step: str
    segment: str | None
    n_users: int
    conv_from_first_pct: float
    conv_from_previous_pct: float


class FunnelResult(TypedDict):
    steps: list[str]
    segment_by: str | None
    rows: list[FunnelStep]
    error: str | None


def funnel_analysis(
    steps: list[str],
    segment_by: SegmentDim | None = None,
) -> FunnelResult:
    """Return one row per (step, segment) with cumulative + step-over-step conversion."""
    if not steps or len(steps) < 2:
        return {
            "steps": steps,
            "segment_by": segment_by,
            "rows": [],
            "error": "Need at least 2 steps for a funnel.",
        }
    unsupported = [s for s in steps if s not in STEP_QUERIES and s not in PRODUCT_EVENT_NAMES]
    if unsupported:
        return {
            "steps": steps,
            "segment_by": segment_by,
            "rows": [],
            "error": f"unsupported step name(s): {unsupported}. Supported: {sorted(STEP_QUERIES | PRODUCT_EVENT_NAMES)}",
        }
    if segment_by is not None and segment_by not in SUPPORTED_SEGMENTS:
        return {
            "steps": steps,
            "segment_by": segment_by,
            "rows": [],
            "error": f"unsupported segment: {segment_by}. Supported: {sorted(SUPPORTED_SEGMENTS)}",
        }

    try:
        with readonly_cursor() as cur:
            step_dfs: dict[str, pd.DataFrame] = {}
            for step in steps:
                step_dfs[step] = _load_step(cur, step)

            # Customer → segment map
            seg_map: dict[int, str | None] = {}
            if segment_by is not None:
                cur.execute(f"SELECT id, {segment_by} FROM customers")
                seg_map = {
                    int(cid): str(seg) if seg is not None else None for cid, seg in cur.fetchall()
                }
    except Exception as exc:  # noqa: BLE001
        return {
            "steps": steps,
            "segment_by": segment_by,
            "rows": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    rows = _compute(step_dfs, steps, seg_map, segment_by)
    return {"steps": steps, "segment_by": segment_by, "rows": rows, "error": None}


def _load_step(cur, step: str) -> pd.DataFrame:
    if step in STEP_QUERIES:
        cur.execute(STEP_QUERIES[step])
    else:
        # product_events first-occurrence
        cur.execute(
            """
            SELECT customer_id, MIN(ts) AS event_at
            FROM product_events
            WHERE event_name = %s
            GROUP BY customer_id
            """,
            (step,),
        )
    data = cur.fetchall()
    df = pd.DataFrame(data, columns=["customer_id", "event_at"])
    df["customer_id"] = df["customer_id"].astype(int)
    return df


def _compute(
    step_dfs: dict[str, pd.DataFrame],
    steps: list[str],
    seg_map: dict[int, str | None],
    segment_by: str | None,
) -> list[FunnelStep]:
    """For each step in order, keep customers whose event_at >= previous step's event_at."""
    cumulative: pd.DataFrame | None = None
    rows: list[FunnelStep] = []

    for i, step in enumerate(steps):
        df = step_dfs[step].rename(columns={"event_at": f"event_at_{i}"})
        if cumulative is None:
            cumulative = df
        else:
            prev_col = f"event_at_{i - 1}"
            curr_col = f"event_at_{i}"
            cumulative = cumulative.merge(df, on="customer_id", how="inner")
            cumulative = cumulative[cumulative[curr_col] >= cumulative[prev_col]]

        if segment_by is None:
            n = int(cumulative["customer_id"].nunique())
            rows.append(
                {
                    "step": step,
                    "segment": None,
                    "n_users": n,
                    "conv_from_first_pct": 0.0,  # filled below
                    "conv_from_previous_pct": 0.0,  # filled below
                }
            )
        else:
            with_seg = cumulative.copy()
            with_seg["segment"] = with_seg["customer_id"].map(seg_map)
            grouped = with_seg.groupby("segment")["customer_id"].nunique().reset_index(name="n")
            for _, r in grouped.iterrows():
                rows.append(
                    {
                        "step": step,
                        "segment": str(r["segment"]) if r["segment"] is not None else None,
                        "n_users": int(r["n"]),
                        "conv_from_first_pct": 0.0,
                        "conv_from_previous_pct": 0.0,
                    }
                )

    # Fill conversion percentages
    if segment_by is None:
        first_n = rows[0]["n_users"]
        for i, r in enumerate(rows):
            r["conv_from_first_pct"] = round(r["n_users"] / first_n * 100, 2) if first_n else 0.0
            prev_n = rows[i - 1]["n_users"] if i > 0 else r["n_users"]
            r["conv_from_previous_pct"] = round(r["n_users"] / prev_n * 100, 2) if prev_n else 0.0
    else:
        # Per-segment conversions
        by_segment: dict[str | None, list[FunnelStep]] = {}
        for r in rows:
            by_segment.setdefault(r["segment"], []).append(r)
        for segment_rows in by_segment.values():
            # Note: segment_rows are in step order because we appended in order
            first_n = segment_rows[0]["n_users"]
            for i, r in enumerate(segment_rows):
                r["conv_from_first_pct"] = (
                    round(r["n_users"] / first_n * 100, 2) if first_n else 0.0
                )
                prev_n = segment_rows[i - 1]["n_users"] if i > 0 else r["n_users"]
                r["conv_from_previous_pct"] = (
                    round(r["n_users"] / prev_n * 100, 2) if prev_n else 0.0
                )

    return rows


if __name__ == "__main__":
    segment = sys.argv[1] if len(sys.argv) > 1 else None
    test_steps = ["signup", "activated", "first_paid"]
    result = funnel_analysis(test_steps, segment_by=segment)
    print(f"funnel: {' -> '.join(result['steps'])}  segment_by={result['segment_by']}")
    if result["error"]:
        print(f"error: {result['error']}")
    else:
        for r in result["rows"]:
            seg = f" [{r['segment']}]" if r["segment"] else ""
            print(
                f"  {r['step']:14s}{seg:15s} n={r['n_users']:5d}  "
                f"first={r['conv_from_first_pct']:5.1f}%  prev={r['conv_from_previous_pct']:5.1f}%"
            )

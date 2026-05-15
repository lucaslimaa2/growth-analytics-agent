"""anomaly_detection: rolling Z-score outliers on daily_metrics.

The agent calls this when the user asks "did anything unusual happen?",
"what was the worst day for X?", or wants to find outliers in a time series.

Algorithm: for each day in the lookback window, compute mean + std over the
prior 30 days and flag points where |z_score| exceeds the threshold. Robust
enough for a portfolio demo; production would use median + MAD.

Run directly to test:
    uv run python -m tools.anomaly dau 180
    uv run python -m tools.anomaly gross_new_mrr 365
"""

from __future__ import annotations

import sys
from typing import Literal, TypedDict

import pandas as pd

from tools._db import readonly_cursor

# Daily metrics columns the agent can scan. Restrict to numeric columns where
# outliers carry meaning.
SUPPORTED_METRICS: set[str] = {
    "new_signups",
    "new_activations",
    "new_paid",
    "churned",
    "gross_new_mrr",
    "expansion_mrr",
    "contraction_mrr",
    "churn_mrr",
    "net_new_mrr",
    "dau",
    "mau",
    "total_paying",
}

WINDOW_DAYS = 30
DEFAULT_THRESHOLD = 2.5
SEVERE_THRESHOLD = 4.0


class Outlier(TypedDict):
    date: str
    value: float
    expected: float
    z_score: float
    severity: Literal["moderate", "severe"]


class AnomalyResult(TypedDict):
    metric: str
    lookback_days: int
    method: str
    threshold: float
    n_points: int
    outliers: list[Outlier]
    summary: str
    error: str | None


def anomaly_detection(
    metric: str,
    lookback_days: int = 180,
    threshold: float = DEFAULT_THRESHOLD,
) -> AnomalyResult:
    """Find outlier days for `metric` over the last `lookback_days` days.

    Returns dates where the value deviates from the prior 30-day rolling mean
    by more than `threshold` standard deviations.
    """
    if metric not in SUPPORTED_METRICS:
        return _error(
            metric,
            lookback_days,
            f"unknown metric: {metric}. Supported: {sorted(SUPPORTED_METRICS)}",
        )
    if lookback_days < 30:
        return _error(
            metric, lookback_days, "lookback_days must be at least 30 (rolling window size)"
        )

    try:
        with readonly_cursor() as cur:
            cur.execute(
                f"""
                SELECT date, {metric}
                FROM daily_metrics
                ORDER BY date DESC
                LIMIT %s
                """,
                (lookback_days + WINDOW_DAYS,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return _error(metric, lookback_days, f"{type(exc).__name__}: {exc}")

    if not rows:
        return _error(metric, lookback_days, "no data returned from daily_metrics")

    df = pd.DataFrame(rows, columns=["date", "value"]).sort_values("date").reset_index(drop=True)
    df["value"] = df["value"].astype(float)

    # Rolling stats over the prior 30 days (excluding the current point).
    df["roll_mean"] = df["value"].shift(1).rolling(WINDOW_DAYS, min_periods=15).mean()
    df["roll_std"] = df["value"].shift(1).rolling(WINDOW_DAYS, min_periods=15).std()
    df["z"] = (df["value"] - df["roll_mean"]) / df["roll_std"]

    # Restrict to the requested lookback window (we fetched extra rows to seed the rolling stats).
    df = df.tail(lookback_days)

    outliers: list[Outlier] = []
    for _, r in df.iterrows():
        z = r["z"]
        if pd.isna(z) or abs(z) < threshold:
            continue
        severity: Literal["moderate", "severe"] = (
            "severe" if abs(z) >= SEVERE_THRESHOLD else "moderate"
        )
        outliers.append(
            {
                "date": str(r["date"]),
                "value": round(float(r["value"]), 2),
                "expected": round(float(r["roll_mean"]), 2),
                "z_score": round(float(z), 2),
                "severity": severity,
            }
        )

    # Order by severity desc, then by absolute z desc.
    outliers.sort(key=lambda o: (o["severity"] != "severe", -abs(o["z_score"])))

    summary = _build_summary(metric, lookback_days, outliers)

    return {
        "metric": metric,
        "lookback_days": lookback_days,
        "method": f"rolling_z_score (window={WINDOW_DAYS} days)",
        "threshold": threshold,
        "n_points": int(len(df)),
        "outliers": outliers,
        "summary": summary,
        "error": None,
    }


def _build_summary(metric: str, lookback_days: int, outliers: list[Outlier]) -> str:
    if not outliers:
        return f"No outliers found in {metric} over the last {lookback_days} days."
    severe = [o for o in outliers if o["severity"] == "severe"]
    moderate = [o for o in outliers if o["severity"] == "moderate"]
    parts = [f"Found {len(outliers)} outlier(s) in {metric} over last {lookback_days} days:"]
    if severe:
        parts.append(f"  {len(severe)} severe (|z| >= {SEVERE_THRESHOLD})")
    if moderate:
        parts.append(f"  {len(moderate)} moderate")
    parts.append(
        f"  worst: {outliers[0]['date']} value={outliers[0]['value']} (z={outliers[0]['z_score']})"
    )
    return "\n".join(parts)


def _error(metric: str, lookback_days: int, msg: str) -> AnomalyResult:
    return {
        "metric": metric,
        "lookback_days": lookback_days,
        "method": "",
        "threshold": DEFAULT_THRESHOLD,
        "n_points": 0,
        "outliers": [],
        "summary": "",
        "error": msg,
    }


if __name__ == "__main__":
    metric_arg = sys.argv[1] if len(sys.argv) > 1 else "dau"
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    result = anomaly_detection(metric_arg, lookback)
    if result["error"]:
        print(f"error: {result['error']}")
    else:
        print(result["summary"])
        print()
        for o in result["outliers"][:10]:
            print(
                f"  [{o['severity']:8s}] {o['date']}  value={o['value']:>10}  "
                f"expected={o['expected']:>10}  z={o['z_score']:+.2f}"
            )

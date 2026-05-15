"""Fixed dashboard data: KPIs, charts, and events timeline.

One function call, one round trip to Postgres, one JSON payload the frontend
renders on page load. Everything here is read from rollup tables; no agent
involved.
"""

from __future__ import annotations

from typing import Any, TypedDict

from tools._db import readonly_cursor


class KPI(TypedDict):
    label: str
    value_fmt: str
    period: str  # e.g. "May 2026" so the reader knows what window the value covers
    delta_pct: float | None
    good_direction: str  # "up" or "down" or "neutral"


class DashboardData(TypedDict):
    kpis: list[KPI]
    mrr_chart: dict[str, Any]
    cohort_chart: dict[str, Any]
    events: list[dict[str, Any]]
    error: str | None


def get_dashboard_data() -> DashboardData:
    """Single query batch for everything the dashboard renders."""
    try:
        with readonly_cursor() as cur:
            kpis = _build_kpis(cur)
            mrr_chart = _build_mrr_chart(cur)
            cohort_chart = _build_cohort_chart(cur)
            events = _fetch_events(cur)
    except Exception as exc:  # noqa: BLE001
        return {
            "kpis": [],
            "mrr_chart": {},
            "cohort_chart": {},
            "events": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "kpis": kpis,
        "mrr_chart": mrr_chart,
        "cohort_chart": cohort_chart,
        "events": events,
        "error": None,
    }


# ============================================================================
# KPIs
# ============================================================================


def _build_kpis(cur) -> list[KPI]:
    """Five KPIs from the last two months of monthly_metrics + cac_by_channel."""
    cur.execute(
        """
        SELECT month, mrr_end, arr, mom_growth_pct, gross_churn_rate, paying_customers, arpu
        FROM monthly_metrics
        ORDER BY month DESC
        LIMIT 2
        """
    )
    rows = cur.fetchall()
    if not rows:
        return []
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None

    # LTV (latest): arpu / (gross_churn_rate / 100). NULL-safe.
    latest_arpu = float(latest[6])
    latest_churn = float(latest[4]) if latest[4] else 0.0
    ltv_latest = latest_arpu / (latest_churn / 100) if latest_churn > 0 else None

    prior_ltv = None
    if prior:
        prior_arpu = float(prior[6])
        prior_churn = float(prior[4]) if prior[4] else 0.0
        prior_ltv = prior_arpu / (prior_churn / 100) if prior_churn > 0 else None

    # CAC: average across paid channels for the latest month.
    latest_month = latest[0]
    cur.execute(
        """
        SELECT AVG(cac_usd)
        FROM cac_by_channel
        WHERE month = %s AND cac_usd IS NOT NULL
        """,
        (latest_month,),
    )
    cac_latest_row = cur.fetchone()
    cac_latest = float(cac_latest_row[0]) if cac_latest_row and cac_latest_row[0] else None

    prior_cac = None
    if prior:
        cur.execute(
            "SELECT AVG(cac_usd) FROM cac_by_channel WHERE month = %s AND cac_usd IS NOT NULL",
            (prior[0],),
        )
        row = cur.fetchone()
        prior_cac = float(row[0]) if row and row[0] else None

    # LTV / CAC ratio
    ltv_cac_latest = ltv_latest / cac_latest if (ltv_latest and cac_latest) else None
    ltv_cac_prior = prior_ltv / prior_cac if (prior_ltv and prior_cac) else None

    period = _fmt_period(latest_month)
    return [
        _kpi(
            "MRR",
            float(latest[1]),
            float(prior[1]) if prior else None,
            period=period,
            fmt="usd",
            good="up",
        ),
        _kpi(
            "ARR",
            float(latest[2]),
            float(prior[2]) if prior else None,
            period=period,
            fmt="usd_compact",
            good="up",
        ),
        _kpi(
            "MoM Growth",
            float(latest[3]),
            float(prior[3]) if prior else None,
            period=period,
            fmt="pct",
            good="up",
            include_delta=False,  # MoM growth is itself already a delta
        ),
        _kpi(
            "Gross Churn",
            float(latest[4]),
            float(prior[4]) if prior else None,
            period=period,
            fmt="pct",
            good="down",
        ),
        _kpi("LTV / CAC", ltv_cac_latest, ltv_cac_prior, period=period, fmt="ratio", good="up"),
    ]


def _fmt_period(yyyy_mm: str) -> str:
    """Turn '2026-05' into 'May 2026'."""
    try:
        year, month = yyyy_mm.split("-")
        months = [
            "",
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        return f"{months[int(month)]} {year}"
    except (ValueError, IndexError):
        return yyyy_mm


def _kpi(
    label: str,
    value: float | None,
    prior: float | None,
    *,
    period: str,
    fmt: str,
    good: str,
    include_delta: bool = True,
) -> KPI:
    delta_pct: float | None = None
    if include_delta and value is not None and prior is not None and prior != 0:
        delta_pct = round((value - prior) / abs(prior) * 100, 2)
    return {
        "label": label,
        "value_fmt": _format_value(value, fmt),
        "period": period,
        "delta_pct": delta_pct,
        "good_direction": good,
    }


def _format_value(value: float | None, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "usd":
        return f"${value:,.0f}"
    if fmt == "usd_compact":
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"${value / 1_000:.1f}K"
        return f"${value:,.0f}"
    if fmt == "pct":
        return f"{value:.2f}%"
    if fmt == "ratio":
        return f"{value:.1f}x"
    return f"{value:,.2f}"


# ============================================================================
# MRR over time
# ============================================================================


def _build_mrr_chart(cur) -> dict[str, Any]:
    cur.execute("SELECT month, mrr_end FROM monthly_metrics ORDER BY month")
    rows = cur.fetchall()
    return {
        "title": "MRR over time (24 months)",
        "x": [r[0] for r in rows],
        "y": [float(r[1]) for r in rows],
        "x_label": "Month",
        "y_label": "MRR (USD)",
    }


# ============================================================================
# Cohort retention heatmap
# ============================================================================


def _build_cohort_chart(cur) -> dict[str, Any]:
    """Return a heatmap-shaped payload: rows = cohorts, columns = age_months."""
    cur.execute(
        """
        SELECT cohort_month, age_months, retained_pct
        FROM cohort_retention
        WHERE age_months <= 12
        ORDER BY cohort_month, age_months
        """
    )
    rows = cur.fetchall()
    if not rows:
        return {"title": "Cohort retention", "x": [], "y": []}

    # Group by cohort
    cohorts: dict[str, dict[int, float]] = {}
    for cohort_month, age, pct in rows:
        cohorts.setdefault(cohort_month, {})[int(age)] = float(pct)

    ages = sorted({a for c in cohorts.values() for a in c})
    rows_out = [{"name": c, "values": [cohorts[c].get(a) for a in ages]} for c in sorted(cohorts)]
    return {
        "title": "Cohort retention by paid-signup month",
        "x": [str(a) for a in ages],
        "y": rows_out,
        "x_label": "Age (months)",
        "y_label": "Cohort",
    }


# ============================================================================
# Events
# ============================================================================


def _fetch_events(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT date, event_type, description
        FROM company_events
        ORDER BY date
        """
    )
    return [
        {"date": r[0].isoformat(), "event_type": r[1], "description": r[2]} for r in cur.fetchall()
    ]


if __name__ == "__main__":
    import json

    data = get_dashboard_data()
    print(json.dumps(data, indent=2, default=str)[:1500], "...")

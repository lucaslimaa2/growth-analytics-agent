"""Output tools: package the agent's findings into structured frontend payloads.

Unlike the data tools (sql, schema, metrics, cohort, ...), these don't query
Postgres. They take values the agent already computed and shape them into JSON
the frontend will render: a Plotly chart spec, a styled table, or a summary
panel.

  - make_chart: emit a Plotly JSON spec the browser renders via Plotly.js.
  - make_table: emit a structured table the browser renders as HTML.
  - summarize_findings: emit the agent's closing insights + recommendations.

For Phase 6 (CLI), these return dicts the agent can echo. In Phase 7 the
streaming layer ships them to the browser; in Phase 9 the UI consumes them.

Run directly to test:
    uv run python -m tools.output
"""

from __future__ import annotations

import itertools
from typing import Any, TypedDict

# A monotonic per-process counter so chart/table IDs are unique within one
# agent run. The frontend uses these as DOM keys when rendering.
_chart_seq = itertools.count(1)
_table_seq = itertools.count(1)

SUPPORTED_CHART_TYPES = {"line", "bar", "area", "heatmap"}


class ChartResult(TypedDict):
    chart_id: str
    chart_type: str
    title: str
    spec: dict[str, Any]
    error: str | None


class TableResult(TypedDict):
    table_id: str
    title: str
    columns: list[str]
    rows: list[list[Any]]
    n_rows: int
    error: str | None


class SummaryResult(TypedDict):
    insights: list[str]
    recommendations: list[str]
    error: str | None


# ============================================================================
# make_chart
# ============================================================================


def make_chart(
    chart_type: str,
    title: str,
    x: list[Any],
    y: list[float] | list[dict[str, Any]],
    x_label: str | None = None,
    y_label: str | None = None,
) -> ChartResult:
    """Build a Plotly JSON spec for the frontend to render.

    Args:
        chart_type: one of "line", "bar", "area", "heatmap".
        title: chart title.
        x: x-axis values (categories or dates).
        y: either a flat list of numbers (single series) or a list of
           {"name": str, "values": list[float]} dicts (multi-series).
           For heatmap: a 2D list (matrix of values), with x as columns and
           y_label given separately as the row labels via the `y` field's
           dict form: [{"name": "<row label>", "values": [...]}, ...].

    Returns a Plotly-compatible dict:
        {"data": [...], "layout": {...}}
    plus a chart_id and metadata.
    """
    if chart_type not in SUPPORTED_CHART_TYPES:
        return _chart_error(
            chart_type,
            title,
            f"unsupported chart_type: {chart_type}. Supported: {sorted(SUPPORTED_CHART_TYPES)}",
        )

    try:
        if chart_type == "heatmap":
            traces = _build_heatmap_traces(x, y)
        else:
            traces = _build_xy_traces(chart_type, x, y)
    except (TypeError, ValueError, KeyError) as exc:
        return _chart_error(chart_type, title, f"{type(exc).__name__}: {exc}")

    layout: dict[str, Any] = {
        "title": {"text": title},
        "margin": {"l": 60, "r": 30, "t": 50, "b": 60},
        "plot_bgcolor": "white",
        "paper_bgcolor": "white",
        "font": {"family": "Inter, system-ui, sans-serif", "size": 12},
    }
    if x_label:
        layout["xaxis"] = {"title": {"text": x_label}}
    if y_label:
        layout["yaxis"] = {"title": {"text": y_label}}

    chart_id = f"chart_{next(_chart_seq)}"
    return {
        "chart_id": chart_id,
        "chart_type": chart_type,
        "title": title,
        "spec": {"data": traces, "layout": layout},
        "error": None,
    }


def _build_xy_traces(chart_type: str, x: list[Any], y: list[Any]) -> list[dict[str, Any]]:
    """For line / bar / area: convert y into one or more Plotly traces."""
    if not y:
        raise ValueError("y is empty")
    plotly_type = {"line": "scatter", "bar": "bar", "area": "scatter"}[chart_type]

    # Single series: y is a flat list of numbers.
    if isinstance(y[0], (int, float)) or y[0] is None:
        trace: dict[str, Any] = {"type": plotly_type, "x": x, "y": y}
        if chart_type in ("line", "area"):
            trace["mode"] = "lines+markers"
        if chart_type == "area":
            trace["fill"] = "tozeroy"
        return [trace]

    # Multi-series: y is a list of {"name", "values"}.
    traces: list[dict[str, Any]] = []
    for series in y:
        if not isinstance(series, dict) or "name" not in series or "values" not in series:
            raise ValueError("multi-series y must be a list of {name, values} dicts")
        trace = {"type": plotly_type, "name": series["name"], "x": x, "y": series["values"]}
        if chart_type in ("line", "area"):
            trace["mode"] = "lines+markers"
        if chart_type == "area":
            trace["fill"] = "tozeroy"
        traces.append(trace)
    return traces


def _build_heatmap_traces(x: list[Any], y: list[Any]) -> list[dict[str, Any]]:
    """For heatmap: y is [{name, values}, ...] giving rows; x is columns."""
    if not y or not isinstance(y[0], dict):
        raise ValueError("heatmap y must be a list of {name, values} dicts (one per row)")
    row_labels = [s["name"] for s in y]
    z = [s["values"] for s in y]
    return [{"type": "heatmap", "x": x, "y": row_labels, "z": z, "colorscale": "Blues"}]


def _chart_error(chart_type: str, title: str, msg: str) -> ChartResult:
    return {
        "chart_id": "",
        "chart_type": chart_type,
        "title": title,
        "spec": {},
        "error": msg,
    }


# ============================================================================
# make_table
# ============================================================================


def make_table(title: str, columns: list[str], rows: list[list[Any]]) -> TableResult:
    """Wrap tabular data so the frontend renders it as a real table."""
    if not columns:
        return {
            "table_id": "",
            "title": title,
            "columns": [],
            "rows": [],
            "n_rows": 0,
            "error": "columns is empty",
        }
    bad_rows = [i for i, r in enumerate(rows) if len(r) != len(columns)]
    if bad_rows:
        return {
            "table_id": "",
            "title": title,
            "columns": columns,
            "rows": [],
            "n_rows": 0,
            "error": (
                f"rows[{bad_rows[:3]}] have a different length than columns ({len(columns)})"
            ),
        }
    table_id = f"table_{next(_table_seq)}"
    return {
        "table_id": table_id,
        "title": title,
        "columns": columns,
        "rows": rows,
        "n_rows": len(rows),
        "error": None,
    }


# ============================================================================
# summarize_findings
# ============================================================================


def summarize_findings(insights: list[str], recommendations: list[str]) -> SummaryResult:
    """End-of-loop structured summary. The frontend renders this as a panel.

    insights: short factual findings, one per bullet.
    recommendations: concrete next actions, one per bullet.
    """
    if not insights and not recommendations:
        return {
            "insights": [],
            "recommendations": [],
            "error": "both insights and recommendations are empty",
        }
    return {
        "insights": [str(i).strip() for i in insights if str(i).strip()],
        "recommendations": [str(r).strip() for r in recommendations if str(r).strip()],
        "error": None,
    }


# ============================================================================
# Manual smoke test
# ============================================================================

if __name__ == "__main__":
    import json

    print("=== make_chart (line, multi-series) ===")
    chart = make_chart(
        chart_type="line",
        title="MRR vs Churn",
        x=["2024-06", "2024-07", "2024-08"],
        y=[
            {"name": "MRR", "values": [5713, 11826, 17600]},
            {"name": "Churn MRR", "values": [0, 230, 280]},
        ],
        x_label="Month",
        y_label="USD",
    )
    print(json.dumps(chart, indent=2)[:400], "...")

    print("\n=== make_table ===")
    table = make_table(
        title="Top Channels by CAC",
        columns=["channel", "cac_usd"],
        rows=[["paid_search", 52.36], ["outbound", 40.17], ["paid_social", 35.45]],
    )
    print(json.dumps(table, indent=2))

    print("\n=== summarize_findings ===")
    summary = summarize_findings(
        insights=[
            "MoM growth flatlined in month 22 due to a production outage.",
            "Pro plan price hike in month 12 spiked churn temporarily.",
        ],
        recommendations=[
            "Diversify acquisition: paid_search CAC is 30% above paid_social.",
            "Communicate price changes earlier to reduce price-related churn.",
        ],
    )
    print(json.dumps(summary, indent=2))

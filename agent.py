"""Growth Analytics Agent: tool-use loop driving Claude Haiku 4.5.

Workflow per question:
  1. Send the user message to Claude with the tool catalog.
  2. If Claude requests tools, execute each, append results to the conversation,
     and loop. If Claude returns final text, return it.
  3. Hard cap at MAX_ITERATIONS to prevent runaway agent loops.

Run from the CLI:
    uv run python agent.py "Why did MoM growth flatline in month 7?"
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from lib.logging import get_logger
from tools.anomaly import anomaly_detection
from tools.benchmarks import compare_to_benchmarks
from tools.cohort import cohort_analysis
from tools.funnel import funnel_analysis
from tools.metrics import get_metric
from tools.output import make_chart, make_table, summarize_findings
from tools.schema import get_schema
from tools.segment import segment_breakdown
from tools.sql import query_database
from tools.time_series import time_series_compare

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

log = get_logger("agent")

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096
MAX_ITERATIONS = 10

# Haiku 4.5 pricing (USD per million tokens), used for per-question cost logging.
PRICE_INPUT = 1.00
PRICE_OUTPUT = 5.00
PRICE_CACHE_WRITE = 1.25
PRICE_CACHE_READ = 0.10


def _cost_usd(in_tok: int, out_tok: int, cw_tok: int, cr_tok: int) -> float:
    return round(
        in_tok * PRICE_INPUT / 1_000_000
        + out_tok * PRICE_OUTPUT / 1_000_000
        + cw_tok * PRICE_CACHE_WRITE / 1_000_000
        + cr_tok * PRICE_CACHE_READ / 1_000_000,
        6,
    )


SYSTEM_PROMPT = """You are a growth analytics agent for a B2B SaaS company.
You answer questions about growth metrics by autonomously querying the company's
Postgres database, then returning concise, numbers-first insights.

Your tools, in preferred order:

  PRE-BUILT METRIC TOOLS (use these first when they fit; they're fast and the
  math is guaranteed consistent across questions):
    - get_metric(name, period): canonical lookup. Supported names: mrr, arr,
      mom_growth, arpu, gross_churn_rate, net_churn_rate, paying_customers,
      new_paying, ltv, cac, ltv_cac. Period: "YYYY-MM", "latest", or omit for
      full history. ALWAYS use this for ltv/cac/ltv_cac — never compute from
      raw tables. The math is locked here and matches the dashboard.
    - cohort_analysis(cohort_by, segment_by, window_months): retention curve
      by signup-paid-month cohort. segment_by can split by channel, country,
      initial_plan, company_size, industry.
    - funnel_analysis(steps, segment_by): conversion rates across an ordered
      list of step names. Recognized steps: signup, activated, first_paid,
      page_view, feature_use, report_export, data_import, team_invite,
      billing_view, dashboard_load, api_call.
    - time_series_compare(metric, period_a, period_b, breakdown_by):
      period-over-period delta with an optional dimensional breakdown
      showing which segment drove the change.
    - segment_breakdown(metric, dimension, period): split a single metric
      by a dimension for one period.

  ANALYTICAL TOOLS (reach for these when the question goes beyond a number):
    - anomaly_detection(metric, lookback_days): rolling Z-score outliers on
      daily_metrics. Call when the user asks about "unusual" days, outliers,
      "worst day", "what happened around X", or "any anomalies in Y".
    - compare_to_benchmarks(metric, value): pin a value against SaaS industry
      norms. Call when the user asks "is X good?", "how does this compare?",
      "what's a healthy/normal Y?". Supported metrics: gross_churn_rate,
      net_churn_rate, mom_growth, ltv_cac, cac_payback_months, arpu.

  GENERAL-PURPOSE TOOLS (fallback when no metric tool fits):
    - get_schema(): introspect tables/columns.
    - query_database(sql): read-only SELECT, up to 500 rows.

  OUTPUT TOOLS (use when the question benefits from a visual or structured
  closing):
    - make_chart(chart_type, title, x, y, ...): emit a Plotly JSON spec. Use
      when the answer has a clear temporal trend, comparison, or distribution.
      chart_type ∈ {line, bar, area, heatmap}. y can be a flat list of numbers
      OR a list of {"name", "values"} for multi-series.
    - make_table(title, columns, rows): emit a structured table when ranked
      data or many rows beat prose.
    - summarize_findings(insights, recommendations): your closing move.
      Use this whenever the question is investigative ("why...", "what
      happened...", "should we..."). Skip it for simple factual lookups
      ("what is our MRR?").

Workflow:
  1. Pick a pre-built metric tool when the question maps to one of them.
     Only fall back to query_database for ad-hoc questions the metric tools
     don't cover.
  2. If you need the schema first, call get_schema. Otherwise skip it.
  3. If a query result is truncated at 500 rows, refine (add WHERE, aggregate,
     narrow time range).
  4. When a chart or table would help the user understand, emit it before the
     final text. End investigative answers with summarize_findings.
  5. Lead with the number. One or two sentences of interpretation in your
     final text answer.
  6. ALWAYS state the time window for every number you cite. "$179K MRR"
     is ambiguous; "$179K MRR (May 2026)" is not. For ratios that depend on
     window choice (CAC, LTV, LTV/CAC, payback), say "latest month" or
     "trailing 6 months" or whatever you actually used. If a user pushes
     back on a number, the most common explanation is a different window
     was picked — say which.

Constraints:
  - Read-only database. No INSERT, UPDATE, DELETE, DROP.
  - No "let me check..." preamble.
  - Dataset covers 24 months from 2024-06 through 2026-05. When the user says
    "month N", treat it as the Nth month of the dataset unless context makes
    a calendar month clearer.
"""

# Anthropic tool definitions (JSON schemas Claude sees).
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_metric",
        "description": (
            "Look up a canonical company metric. Use this for any of: mrr, arr, "
            "mom_growth, arpu, gross_churn_rate, net_churn_rate, paying_customers, "
            "new_paying, ltv. Returns one or more {month, value} points."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "mrr",
                        "arr",
                        "mom_growth",
                        "arpu",
                        "gross_churn_rate",
                        "net_churn_rate",
                        "paying_customers",
                        "new_paying",
                        "ltv",
                        "cac",
                        "ltv_cac",
                    ],
                },
                "period": {
                    "type": "string",
                    "description": "YYYY-MM for a single month, 'latest' for the most recent month, or omit for all 24 months.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "cohort_analysis",
        "description": (
            "Compute retention curves by first-paid-month cohort. Returns rows of "
            "{cohort_month, age_months, segment, retained_pct, surviving_mrr}. "
            "Pass segment_by to split each cohort by a customer dimension."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cohort_by": {
                    "type": "string",
                    "enum": ["first_paid_month"],
                    "default": "first_paid_month",
                },
                "segment_by": {
                    "type": "string",
                    "enum": ["channel", "country", "initial_plan", "company_size", "industry"],
                    "description": "Optional customer dimension to split each cohort by.",
                },
                "window_months": {
                    "type": "integer",
                    "description": "Max cohort age to return (default 12).",
                    "default": 12,
                },
            },
            "required": [],
        },
    },
    {
        "name": "funnel_analysis",
        "description": (
            "Compute conversion rates across an ordered list of lifecycle steps. "
            "Steps are evaluated in temporal order: a customer counts at step N "
            "only if they hit every earlier step first. Recognized steps: signup, "
            "activated, first_paid, plus the product event names "
            "(page_view, feature_use, report_export, data_import, team_invite, "
            "billing_view, dashboard_load, api_call)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of step names. Need at least 2.",
                },
                "segment_by": {
                    "type": "string",
                    "enum": ["channel", "country", "initial_plan", "company_size", "industry"],
                    "description": "Optional customer dimension to split the funnel by.",
                },
            },
            "required": ["steps"],
        },
    },
    {
        "name": "time_series_compare",
        "description": (
            "Compare a metric between two months. Returns totals, delta, percent "
            "change, and optionally a breakdown by dimension showing which segment "
            "drove the change. Metrics: mrr_end, new_signups, new_activations, "
            "new_paying, churn_mrr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": [
                        "mrr_end",
                        "new_signups",
                        "new_activations",
                        "new_paying",
                        "churn_mrr",
                    ],
                },
                "period_a": {
                    "type": "string",
                    "description": "YYYY-MM (the earlier or baseline period).",
                },
                "period_b": {
                    "type": "string",
                    "description": "YYYY-MM (the later or comparison period).",
                },
                "breakdown_by": {
                    "type": "string",
                    "description": "Optional dimension. Valid pairs: (mrr_end, plan), (new_signups, channel|country|initial_plan), (new_activations, channel), (new_paying, channel), (churn_mrr, end_reason|plan).",
                },
            },
            "required": ["metric", "period_a", "period_b"],
        },
    },
    {
        "name": "segment_breakdown",
        "description": (
            "Split a metric by a dimension for one period. Returns rows of "
            "{segment, value} sorted descending. Supports the same metric/dimension "
            "pairs as time_series_compare's breakdown_by."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": [
                        "mrr_end",
                        "new_signups",
                        "new_activations",
                        "new_paying",
                        "churn_mrr",
                    ],
                },
                "dimension": {
                    "type": "string",
                    "description": "Dimension to split by (plan, channel, country, initial_plan, end_reason).",
                },
                "period": {"type": "string", "description": "YYYY-MM."},
            },
            "required": ["metric", "dimension", "period"],
        },
    },
    {
        "name": "anomaly_detection",
        "description": (
            "Find outlier days for a daily metric using rolling Z-scores. Use for "
            "questions about 'unusual' days, outliers, 'worst/best day for X', or "
            "'what happened around <date>'. Returns flagged days with severity, "
            "value, expected value, and z-score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": [
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
                    ],
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "Days back to scan (default 180, min 30).",
                    "default": 180,
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "compare_to_benchmarks",
        "description": (
            "Compare a metric value to industry SaaS benchmarks. Use when the user "
            "asks 'is X good?', 'how does Y compare?', or 'what's healthy/normal?'. "
            "Returns a tier (excellent/typical/concerning/etc.), a verdict, and the "
            "full benchmark table for context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": [
                        "gross_churn_rate",
                        "net_churn_rate",
                        "mom_growth",
                        "ltv_cac",
                        "cac_payback_months",
                        "arpu",
                    ],
                },
                "value": {
                    "type": "number",
                    "description": "The value to compare against benchmarks.",
                },
            },
            "required": ["metric", "value"],
        },
    },
    {
        "name": "get_schema",
        "description": (
            "Return the structure of the public schema: tables, columns, types, "
            "descriptions. Call when a pre-built metric tool doesn't fit and you "
            "need to write SQL via query_database."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "query_database",
        "description": (
            "Run a read-only SQL query. Returns columns, rows (max 500), and a "
            "truncated flag. Use only when the pre-built metric tools don't cover "
            "the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SQL SELECT statement."}
            },
            "required": ["sql"],
        },
    },
    {
        "name": "make_chart",
        "description": (
            "Emit a Plotly chart spec the frontend will render. Use when the "
            "answer benefits from a visual (trend, comparison, distribution). "
            "y can be a flat list of numbers OR a list of {name, values} for "
            "multi-series."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["line", "bar", "area", "heatmap"],
                },
                "title": {"type": "string"},
                "x": {
                    "type": "array",
                    "items": {"type": ["string", "number"]},
                    "description": "X-axis values (e.g. month strings, channel names).",
                },
                "y": {
                    "description": "Either a flat list of numbers (single series) or a list of {name, values} dicts (multi-series / heatmap rows).",
                },
                "x_label": {"type": "string"},
                "y_label": {"type": "string"},
            },
            "required": ["chart_type", "title", "x", "y"],
        },
    },
    {
        "name": "make_table",
        "description": (
            "Emit a structured table the frontend will render. Use when ranked "
            "rows or many columns beat prose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "List of rows, each row a list matching columns in order.",
                },
            },
            "required": ["title", "columns", "rows"],
        },
    },
    {
        "name": "summarize_findings",
        "description": (
            "Closing move for investigative questions: emit the structured "
            "insights and recommendations. Skip this for simple factual lookups. "
            "After calling this, return your final concise text answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "insights": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short factual findings, one per bullet.",
                },
                "recommendations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete next actions, one per bullet.",
                },
            },
            "required": ["insights", "recommendations"],
        },
    },
]

# Map tool names to the Python callable that implements them.
TOOL_DISPATCH: dict[str, Callable[..., Any]] = {
    "get_metric": get_metric,
    "cohort_analysis": cohort_analysis,
    "funnel_analysis": funnel_analysis,
    "time_series_compare": time_series_compare,
    "segment_breakdown": segment_breakdown,
    "anomaly_detection": anomaly_detection,
    "compare_to_benchmarks": compare_to_benchmarks,
    "get_schema": get_schema,
    "query_database": query_database,
    "make_chart": make_chart,
    "make_table": make_table,
    "summarize_findings": summarize_findings,
}


def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Run a tool by name with kwargs from Claude. Returns JSON-serialized result."""
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    t0 = time.monotonic()
    try:
        result = fn(**args)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "tool_ok",
            extra={"tool": name, "duration_ms": elapsed_ms, "args_preview": _preview(args)},
        )
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - we want to surface everything to Claude
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.error(
            "tool_failed",
            extra={
                "tool": name,
                "duration_ms": elapsed_ms,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _preview(args: dict[str, Any], max_len: int = 200) -> dict[str, Any]:
    """Shorten long string args for logs."""
    return {
        k: (v[:max_len] + "..." if isinstance(v, str) and len(v) > max_len else v)
        for k, v in args.items()
    }


# Tools whose results the frontend renders directly (chart, table, summary panel).
# Other tool outputs (sql, get_metric, etc.) feed Claude but are not surfaced to the user.
RENDERABLE_TOOLS = {"make_chart", "make_table", "summarize_findings"}


def run_agent(
    question: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run one full agentic turn, optionally with prior chat history.

    Args:
        question: the new user question.
        history: prior conversation as a list of {role, content} dicts where
                 role is "user" or "assistant" and content is plain text.
                 Tool calls/results from prior turns are NOT preserved (we only
                 keep final assistant text), so the agent will re-query if it
                 needs the same data again.

    Returns:
        {answer, outputs, iterations, stop_reason}
    """
    client = anthropic.Anthropic()
    messages: list[dict[str, Any]] = list(history or [])
    messages.append({"role": "user", "content": question})
    renderable_outputs: list[dict[str, Any]] = []
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }

    log.info("agent_start", extra={"question": question, "model": MODEL})

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # 1-hour TTL: stays cached across a typical user session.
                    # Write cost 2x base input, but pays off after ~3 reads.
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        cw = getattr(response.usage, "cache_creation_input_tokens", 0)
        cr = getattr(response.usage, "cache_read_input_tokens", 0)
        totals["input_tokens"] += response.usage.input_tokens
        totals["output_tokens"] += response.usage.output_tokens
        totals["cache_write_tokens"] += cw
        totals["cache_read_tokens"] += cr

        log.info(
            "agent_iteration",
            extra={
                "iteration": iteration,
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": cr,
                "cache_write_tokens": cw,
            },
        )

        if response.stop_reason == "end_turn":
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final = "\n".join(text_blocks).strip()
            cost = _cost_usd(
                totals["input_tokens"],
                totals["output_tokens"],
                totals["cache_write_tokens"],
                totals["cache_read_tokens"],
            )
            log.info(
                "agent_done",
                extra={
                    "iterations": iteration,
                    "answer_chars": len(final),
                    "renderable_count": len(renderable_outputs),
                    "cost_usd": cost,
                    **totals,
                },
            )
            return {
                "answer": final,
                "outputs": renderable_outputs,
                "iterations": iteration,
                "stop_reason": "end_turn",
                "cost_usd": cost,
                "tokens": totals,
            }

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info(
                    "tool_call",
                    extra={"tool": block.name, "args_preview": _preview(block.input)},
                )
                result_json = execute_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    }
                )
                # Capture chart/table/summary outputs for the frontend.
                if block.name in RENDERABLE_TOOLS:
                    try:
                        parsed = json.loads(result_json)
                        kind = {
                            "make_chart": "chart",
                            "make_table": "table",
                            "summarize_findings": "summary",
                        }[block.name]
                        renderable_outputs.append({"kind": kind, "data": parsed})
                    except (json.JSONDecodeError, KeyError):
                        pass
            messages.append({"role": "user", "content": tool_results})
            continue

        # Any other stop_reason (max_tokens, refusal, etc.) ends the loop.
        log.warning("agent_unexpected_stop", extra={"stop_reason": response.stop_reason})
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return {
            "answer": "\n".join(text_blocks).strip() or f"(stopped: {response.stop_reason})",
            "outputs": renderable_outputs,
            "iterations": iteration,
            "stop_reason": response.stop_reason,
        }

    log.warning("agent_iteration_cap", extra={"cap": MAX_ITERATIONS})
    return {
        "answer": f"(agent exceeded {MAX_ITERATIONS} iterations without a final answer)",
        "outputs": renderable_outputs,
        "iterations": MAX_ITERATIONS,
        "stop_reason": "iteration_cap",
    }


# ============================================================================
# Streaming variant: yields events to the caller instead of returning a dict.
# Used by POST /api/chat/stream.
# ============================================================================


def run_agent_streaming(
    question: str,
    history: list[dict[str, str]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Streaming variant of run_agent. Same args, yields events incrementally.

    Event shapes:
        {"kind": "iteration_start", "iteration": int}
        {"kind": "tool_call",       "tool": str, "args_preview": dict}
        {"kind": "tool_result",     "tool": str, "duration_ms": int, "ok": bool}
        {"kind": "output",          "output": {"kind": "chart"|"table"|"summary", "data": ...}}
        {"kind": "text_delta",      "text": str}
        {"kind": "done",            "iterations": int, "stop_reason": str}
        {"kind": "error",           "message": str}
    """
    client = anthropic.Anthropic()
    messages: list[dict[str, Any]] = list(history or [])
    messages.append({"role": "user", "content": question})
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }

    log.info("agent_stream_start", extra={"question": question, "model": MODEL})

    for iteration in range(1, MAX_ITERATIONS + 1):
        yield {"kind": "iteration_start", "iteration": iteration}

        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # 1-hour TTL: stays cached across a typical user session.
                    # Write cost 2x base input, but pays off after ~3 reads.
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        ) as stream:
            # Stream user-facing text tokens as they arrive.
            for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield {"kind": "text_delta", "text": event.delta.text}

            response = stream.get_final_message()

        cw = getattr(response.usage, "cache_creation_input_tokens", 0)
        cr = getattr(response.usage, "cache_read_input_tokens", 0)
        totals["input_tokens"] += response.usage.input_tokens
        totals["output_tokens"] += response.usage.output_tokens
        totals["cache_write_tokens"] += cw
        totals["cache_read_tokens"] += cr

        log.info(
            "agent_iteration",
            extra={
                "iteration": iteration,
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_write_tokens": cw,
                "cache_read_tokens": cr,
            },
        )

        if response.stop_reason == "end_turn":
            cost = _cost_usd(
                totals["input_tokens"],
                totals["output_tokens"],
                totals["cache_write_tokens"],
                totals["cache_read_tokens"],
            )
            log.info("agent_done", extra={"iterations": iteration, "cost_usd": cost, **totals})
            yield {
                "kind": "done",
                "iterations": iteration,
                "stop_reason": "end_turn",
                "cost_usd": cost,
                "tokens": totals,
            }
            return

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {
                    "kind": "tool_call",
                    "tool": block.name,
                    "args_preview": _preview(block.input),
                }
                t0 = time.monotonic()
                result_json = execute_tool(block.name, block.input)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                parsed_ok = True
                try:
                    parsed = json.loads(result_json)
                    parsed_ok = "error" not in parsed or not parsed["error"]
                except json.JSONDecodeError:
                    parsed = None
                yield {
                    "kind": "tool_result",
                    "tool": block.name,
                    "duration_ms": elapsed_ms,
                    "ok": parsed_ok,
                }
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    }
                )
                if parsed is not None and block.name in RENDERABLE_TOOLS:
                    kind = {
                        "make_chart": "chart",
                        "make_table": "table",
                        "summarize_findings": "summary",
                    }[block.name]
                    yield {"kind": "output", "output": {"kind": kind, "data": parsed}}

            messages.append({"role": "user", "content": tool_results})
            continue

        yield {"kind": "done", "iterations": iteration, "stop_reason": response.stop_reason}
        return

    yield {"kind": "done", "iterations": MAX_ITERATIONS, "stop_reason": "iteration_cap"}


def main() -> None:
    # Windows consoles default to cp1252 and crash on common Unicode characters
    # (em-dashes, arrows, etc.). Force UTF-8 for stdout/stderr.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        sys.exit('Usage: uv run python agent.py "your question"')
    question = " ".join(sys.argv[1:])
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set in .env")
    result = run_agent(question)
    print()
    print("=" * 70)
    print(result["answer"])
    print("=" * 70)
    if result["outputs"]:
        print(
            f"\n[{len(result['outputs'])} renderable output(s): "
            f"{', '.join(o['kind'] for o in result['outputs'])}]"
        )


if __name__ == "__main__":
    main()

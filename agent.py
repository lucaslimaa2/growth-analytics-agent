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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from lib.logging import get_logger
from tools.schema import get_schema
from tools.sql import query_database

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

log = get_logger("agent")

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096
MAX_ITERATIONS = 10

SYSTEM_PROMPT = """You are a growth analytics agent for a B2B SaaS company.
You answer questions about growth metrics by autonomously querying the company's
Postgres database, then returning concise, numbers-first insights.

You have two tools:
  - get_schema(): introspect the tables and columns available.
  - query_database(sql): run a read-only SQL query. Returns up to 500 rows.

Workflow:
  1. Call get_schema first if you don't already know the schema for this question.
  2. Write SQL to answer the user. Prefer the pre-computed rollup tables
     (monthly_metrics, daily_metrics, cac_by_channel, cohort_retention) over
     recomputing aggregates from raw tables when the rollup is sufficient.
  3. If a result is truncated at 500 rows, refine the query (add WHERE clauses,
     aggregate, narrow the time range).
  4. When you have the answer, return it concisely: lead with the number, then
     one or two sentences explaining what's happening.

Constraints:
  - The database is read-only. Don't attempt INSERT, UPDATE, DELETE, DROP.
  - No preamble. No "let me check..." filler. Lead with the answer.
"""

# Anthropic tool definitions (JSON schemas Claude sees).
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_schema",
        "description": (
            "Return the structure of the public schema: every table with its columns, "
            "types, nullability, and a plain-English description. Call this once at the "
            "start of a question if you don't already know the schema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "query_database",
        "description": (
            "Run a read-only SQL query against Postgres. Returns columns, rows (max 500), "
            "and a truncated flag. Use this for any data fetch. SELECT only; writes are "
            "rejected by the database role."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SQL SELECT statement.",
                }
            },
            "required": ["sql"],
        },
    },
]

# Map tool names to the Python callable that implements them.
TOOL_DISPATCH: dict[str, Callable[..., Any]] = {
    "get_schema": lambda: get_schema(),
    "query_database": lambda sql: query_database(sql),
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


def run_agent(question: str) -> str:
    """Run one full agentic turn: ask Claude, run tools, repeat until answer."""
    client = anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    log.info("agent_start", extra={"question": question, "model": MODEL})

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        log.info(
            "agent_iteration",
            extra={
                "iteration": iteration,
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_write_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
            },
        )

        if response.stop_reason == "end_turn":
            # Final answer. Concatenate any text blocks.
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final = "\n".join(text_blocks).strip()
            log.info("agent_done", extra={"iterations": iteration, "answer_chars": len(final)})
            return final

        if response.stop_reason == "tool_use":
            # Append the assistant turn (must include the tool_use blocks verbatim).
            messages.append({"role": "assistant", "content": response.content})

            # Execute every tool_use block and collect results.
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
            messages.append({"role": "user", "content": tool_results})
            continue

        # Any other stop_reason (max_tokens, refusal, etc.) ends the loop.
        log.warning("agent_unexpected_stop", extra={"stop_reason": response.stop_reason})
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return "\n".join(text_blocks).strip() or f"(stopped: {response.stop_reason})"

    log.warning("agent_iteration_cap", extra={"cap": MAX_ITERATIONS})
    return f"(agent exceeded {MAX_ITERATIONS} iterations without a final answer)"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: uv run python agent.py "your question"')
    question = " ".join(sys.argv[1:])
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set in .env")
    answer = run_agent(question)
    print()
    print("=" * 70)
    print(answer)
    print("=" * 70)


if __name__ == "__main__":
    main()

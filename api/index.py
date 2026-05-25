"""FastAPI entrypoint for Vercel's Python runtime.

Routes:
  GET  /api/ping  - smoke test (DB connectivity)
  POST /api/chat  - run the agent on a user question; returns answer + outputs
"""

import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the project root importable so we can `from agent import run_agent`
# both locally (via uvicorn) and on Vercel (where the api/ folder is the
# function entry).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from agent import run_agent, run_agent_streaming  # noqa: E402
from lib.dashboard import get_dashboard_data  # noqa: E402
from lib.rate_limit import check_rate_limit  # noqa: E402
from lib.scope_gate import REFUSAL_TEXT, check_scope  # noqa: E402

app = FastAPI()


def _client_ip(request: Request) -> str:
    """Get the originating client IP, honoring Vercel's proxy headers."""
    # Vercel/Cloudflare set x-forwarded-for; take the first hop.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_response(ip: str):
    """Return a 429 response if the IP is over the rate limit, else None."""
    result = check_rate_limit(ip)
    if not result.allowed:
        return JSONResponse(
            {
                "error": "rate_limited",
                "message": (
                    f"You've hit the per-IP rate limit ({result.count} requests in 60s). "
                    f"Try again in {result.retry_after} seconds."
                ),
                "retry_after": result.retry_after,
            },
            status_code=429,
            headers={"Retry-After": str(result.retry_after)},
        )
    return None


@app.get("/")
def root():
    """Redirect to the chat UI. On Vercel, /index.html is served by the edge
    from public/; locally, the StaticFiles mount below serves it from disk."""
    return RedirectResponse(url="/index.html", status_code=302)


class ChatTurn(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[ChatTurn] | None = None  # optional prior conversation


@app.get("/api/ping")
def ping():
    try:
        url = os.environ["SUPABASE_DB_URL_POOLED"]
        with psycopg.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT NOW()")
                db_time = cur.fetchone()[0].isoformat()
        return {"ok": True, "db_time": db_time}
    except KeyError:
        return JSONResponse(
            {"ok": False, "error": "SUPABASE_DB_URL_POOLED not set"}, status_code=500
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.get("/api/dashboard")
def dashboard():
    """Fixed dashboard payload: KPIs, charts, events. ~50ms; no agent involved."""
    try:
        return get_dashboard_data()
    except Exception as exc:
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    """Run the agent on a single user question.

    Returns:
        {
          "answer":     str,
          "outputs":    [{"kind": "chart"|"table"|"summary", "data": {...}}, ...],
          "iterations": int,
          "stop_reason": str
        }
    """
    q = (req.question or "").strip()
    if not q:
        return JSONResponse({"error": "question is empty"}, status_code=400)
    if len(q) > 2000:
        return JSONResponse({"error": "question is too long (max 2000 chars)"}, status_code=400)

    rl = _rate_limit_response(_client_ip(request))
    if rl is not None:
        return rl

    history_dicts = [h.model_dump() for h in (req.history or [])]

    # Scope gate: short-circuit before the agent fires.
    decision = check_scope(q, history=history_dicts)
    if not decision["result"].in_scope:
        return {
            "answer": REFUSAL_TEXT,
            "outputs": [],
            "iterations": 0,
            "stop_reason": "out_of_scope",
            "cost_usd": decision["telemetry"]["cost_usd"],
            "tokens": {
                "input_tokens": decision["telemetry"]["input_tokens"],
                "output_tokens": decision["telemetry"]["output_tokens"],
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
            },
            "refusal_reason": decision["result"].reason,
        }

    try:
        result = run_agent(q, history=history_dicts)
        # Roll the gate cost into the total so the user sees true total.
        result["cost_usd"] = round(result["cost_usd"] + decision["telemetry"]["cost_usd"], 6)
        return result
    except Exception as exc:
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """SSE variant of /api/chat: streams events as the agent works."""
    q = (req.question or "").strip()
    if not q:
        return JSONResponse({"error": "question is empty"}, status_code=400)
    if len(q) > 2000:
        return JSONResponse({"error": "question is too long (max 2000 chars)"}, status_code=400)

    rl = _rate_limit_response(_client_ip(request))
    if rl is not None:
        return rl

    history_dicts = [h.model_dump() for h in (req.history or [])]

    # Scope gate runs synchronously before we open the SSE stream. If the
    # gate refuses, we still stream a refusal so the frontend code path
    # is uniform (text_delta + done), but no agent loop fires.
    decision = check_scope(q, history=history_dicts)

    def sse_generator():
        if not decision["result"].in_scope:
            # Stream the refusal as text_delta events + done.
            yield f"data: {json.dumps({'kind': 'text_delta', 'text': REFUSAL_TEXT})}\n\n"
            done_event = {
                "kind": "done",
                "iterations": 0,
                "stop_reason": "out_of_scope",
                "cost_usd": decision["telemetry"]["cost_usd"],
                "tokens": {
                    "input_tokens": decision["telemetry"]["input_tokens"],
                    "output_tokens": decision["telemetry"]["output_tokens"],
                    "cache_write_tokens": 0,
                    "cache_read_tokens": 0,
                },
            }
            yield f"data: {json.dumps(done_event)}\n\n"
            return

        try:
            for event in run_agent_streaming(q, history=history_dicts):
                # Roll the gate cost into the final done event's total.
                if event.get("kind") == "done" and "cost_usd" in event:
                    event["cost_usd"] = round(
                        event["cost_usd"] + decision["telemetry"]["cost_usd"], 6
                    )
                yield f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"
        except Exception as exc:
            err = {"kind": "error", "message": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(err)}\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )


# Local-dev convenience: serve the static frontend from public/ so `uvicorn`
# preview works the same as Vercel (which serves public/ via the edge). The
# mount goes LAST so /api/* routes are matched first.
_PUBLIC = Path(__file__).resolve().parent.parent / "public"
if _PUBLIC.exists():
    app.mount("/", StaticFiles(directory=str(_PUBLIC), html=True), name="public")

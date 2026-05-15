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
from fastapi import FastAPI
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

app = FastAPI()


@app.get("/")
def root():
    """Redirect to the chat UI. On Vercel, /index.html is served by the edge
    from public/; locally, the StaticFiles mount below serves it from disk."""
    return RedirectResponse(url="/index.html", status_code=302)


class ChatRequest(BaseModel):
    question: str


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
def chat(req: ChatRequest):
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

    try:
        result = run_agent(q)
        return result
    except Exception as exc:
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE variant of /api/chat: streams events as the agent works."""
    q = (req.question or "").strip()
    if not q:
        return JSONResponse({"error": "question is empty"}, status_code=400)
    if len(q) > 2000:
        return JSONResponse({"error": "question is too long (max 2000 chars)"}, status_code=400)

    def sse_generator():
        try:
            for event in run_agent_streaming(q):
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

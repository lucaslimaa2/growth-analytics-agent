"""FastAPI entrypoint for Vercel's Python runtime.

Routes:
  GET  /api/ping  - smoke test (DB connectivity)
  POST /api/chat  - run the agent on a user question; returns answer + outputs
"""

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the project root importable so we can `from agent import run_agent`
# both locally (via uvicorn) and on Vercel (where the api/ folder is the
# function entry).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from agent import run_agent  # noqa: E402

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


# Local-dev convenience: serve the static frontend from public/ so `uvicorn`
# preview works the same as Vercel (which serves public/ via the edge). The
# mount goes LAST so /api/* routes are matched first.
_PUBLIC = Path(__file__).resolve().parent.parent / "public"
if _PUBLIC.exists():
    app.mount("/", StaticFiles(directory=str(_PUBLIC), html=True), name="public")

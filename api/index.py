"""FastAPI entrypoint for Vercel's Python runtime. All /api/* routes live here."""

import os

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

load_dotenv()

app = FastAPI()


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
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
        )

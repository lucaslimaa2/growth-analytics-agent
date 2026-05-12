"""Load synthetic data into Supabase: run schema, COPY each CSV, verify row counts.

Uses SUPABASE_DB_URL_POOLED. The direct (port 5432) Supabase host is IPv6-only
on the free tier, so we go through the IPv4-friendly transaction pooler instead.
COPY and DDL run in a single transaction, which the pooler handles fine.
Prepared statements are disabled (prepare_threshold=None) because the transaction
pooler routes each transaction to a potentially different server connection.

Run: uv run python scripts/load_data.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "db" / "schema.sql"
DATA_DIR = ROOT / "data"

# (csv filename, table name, list of columns IN CSV ORDER)
TABLES: list[tuple[str, str, list[str]]] = [
    ("company_profile.csv", "company_profile", ["founded", "industry", "product_type"]),
    (
        "customers.csv",
        "customers",
        [
            "id",
            "signup_date",
            "activated_date",
            "source",
            "channel",
            "country",
            "industry",
            "company_size",
            "initial_plan",
        ],
    ),
    (
        "subscriptions.csv",
        "subscriptions",
        ["id", "customer_id", "plan", "mrr_usd", "started_at", "ended_at", "end_reason"],
    ),
    (
        "product_events.csv",
        "product_events",
        ["id", "customer_id", "event_name", "ts", "properties"],
    ),
    (
        "marketing_spend.csv",
        "marketing_spend",
        ["date", "channel", "campaign", "spend_usd", "impressions", "clicks", "conversions"],
    ),
    ("referrals.csv", "referrals", ["referrer_id", "referred_id", "referred_at"]),
    ("company_events.csv", "company_events", ["date", "event_type", "description"]),
    (
        "daily_metrics.csv",
        "daily_metrics",
        [
            "date",
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
    ),
    (
        "monthly_metrics.csv",
        "monthly_metrics",
        [
            "month",
            "mrr_start",
            "mrr_added",
            "expansion",
            "contraction",
            "churn",
            "mrr_end",
            "mom_growth_pct",
            "arr",
            "paying_customers",
            "arpu",
            "gross_churn_rate",
            "net_churn_rate",
        ],
    ),
    (
        "cac_by_channel.csv",
        "cac_by_channel",
        ["month", "channel", "spend_usd", "new_paying_customers", "cac_usd"],
    ),
    (
        "cohort_retention.csv",
        "cohort_retention",
        ["cohort_month", "age_months", "retained_pct", "surviving_mrr"],
    ),
]


def run_schema(conn: psycopg.Connection) -> None:
    print(f"Applying schema from {SCHEMA_FILE.relative_to(ROOT)}...")
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def copy_csv(conn: psycopg.Connection, csv_path: Path, table: str, columns: list[str]) -> int:
    cols_sql = ", ".join(columns)
    copy_sql = f"COPY {table} ({cols_sql}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')"
    with conn.cursor() as cur:
        with csv_path.open("rb") as f, cur.copy(copy_sql) as cpy:
            while chunk := f.read(64 * 1024):
                cpy.write(chunk)
        # Cursor is no longer in COPY mode; safe to query again.
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
    return n


def main() -> None:
    url = os.environ.get("SUPABASE_DB_URL_POOLED")
    if not url:
        sys.exit("ERROR: SUPABASE_DB_URL_POOLED not set in .env")

    if not DATA_DIR.exists():
        sys.exit(f"ERROR: {DATA_DIR} not found. Run scripts/generate_data.py first.")

    print("Connecting via transaction pooler...")
    with psycopg.connect(url, connect_timeout=15, prepare_threshold=None) as conn:
        run_schema(conn)

        totals = []
        for csv_name, table, columns in TABLES:
            csv_path = DATA_DIR / csv_name
            if not csv_path.exists():
                print(f"  SKIP {table}: {csv_path.name} not found")
                continue
            print(f"  COPY {csv_name} -> {table} ...", end="", flush=True)
            n = copy_csv(conn, csv_path, table, columns)
            print(f" {n:,} rows")
            totals.append((table, n))
        conn.commit()

    print()
    print("Loaded summary:")
    grand_total = 0
    for table, n in totals:
        print(f"  {table:18s} {n:>8,} rows")
        grand_total += n
    print(f"  {'TOTAL':18s} {grand_total:>8,} rows")


if __name__ == "__main__":
    main()

"""Schema introspection tool. Returns structure of the public schema, with
hand-written semantic descriptions layered on top of information_schema.

Used by the agent loop (Phase 4 onward) to plan correct SQL queries.

Run directly to print the schema as markdown:
    uv run python -m tools.schema
"""

from __future__ import annotations

import os
from typing import TypedDict

import psycopg
from dotenv import load_dotenv

load_dotenv()


# ============================================================================
# Hand-written context that information_schema cannot give us
# ============================================================================

TABLE_DESCRIPTIONS: dict[str, str] = {
    "company_profile": "Single row describing the demo company itself.",
    "customers": "One row per customer who has ever signed up. About 5,000 rows over 24 months.",
    "subscriptions": "Subscription records (paid plans only). One customer can have multiple rows over time (upgrade, downgrade, churn-and-resubscribe). Free customers do not appear here.",
    "product_events": "In-app events keyed by customer. About 50,000 rows. The properties column is JSONB.",
    "marketing_spend": "Daily spend per acquisition channel and campaign.",
    "referrals": "Records of one customer referring another.",
    "company_events": "Annotated business moments (product launches, outages, price changes, marketing milestones).",
    "daily_metrics": "Pre-computed daily rollup over the raw tables. Use this when daily granularity is enough rather than recomputing from raw.",
    "monthly_metrics": "Pre-computed monthly rollup. MRR motion, ARR, MoM growth, churn rates, ARPU.",
    "cac_by_channel": "Monthly CAC per acquisition channel. cac_usd = spend_usd / new_paying_customers.",
    "cohort_retention": "Retention curves by signup-paid-month cohort.",
}

COLUMN_DESCRIPTIONS: dict[tuple[str, str], str] = {
    # company_profile
    ("company_profile", "founded"): "Date the demo company was founded.",
    ("company_profile", "industry"): "Industry classification.",
    ("company_profile", "product_type"): "Short description of the product.",
    # customers
    ("customers", "id"): "Surrogate primary key.",
    ("customers", "signup_date"): "Date the customer created an account.",
    (
        "customers",
        "activated_date",
    ): "Date the customer first completed onboarding. NULL if never activated.",
    (
        "customers",
        "source",
    ): "Acquisition source recorded at signup. Values: paid_search, paid_social, organic, referral, outbound.",
    ("customers", "channel"): "Acquisition channel. Same values as source in this dataset.",
    ("customers", "country"): "ISO 2-letter country code.",
    ("customers", "industry"): "Industry self-reported at signup.",
    (
        "customers",
        "company_size",
    ): "Headcount band self-reported at signup. Values: 1-10, 11-50, 51-200, 201-1000, 1000+.",
    ("customers", "initial_plan"): "Plan chosen at signup. Values: Free, Starter, Pro, Business.",
    # subscriptions
    ("subscriptions", "id"): "Surrogate primary key.",
    ("subscriptions", "customer_id"): "References customers.id.",
    ("subscriptions", "plan"): "Plan name. Values: Starter, Pro, Business.",
    ("subscriptions", "mrr_usd"): "Monthly recurring revenue for this subscription in USD.",
    ("subscriptions", "started_at"): "Date this subscription record became active.",
    ("subscriptions", "ended_at"): "Date this subscription ended. NULL if still active.",
    (
        "subscriptions",
        "end_reason",
    ): "Why the subscription ended. Values: voluntary, involuntary_payment, upgrade, downgrade, price_increase, price_increase_migration, outage_followup. NULL if still active.",
    # product_events
    ("product_events", "id"): "Surrogate primary key.",
    ("product_events", "customer_id"): "References customers.id.",
    (
        "product_events",
        "event_name",
    ): "Event type. Values: page_view, feature_use, report_export, data_import, team_invite, billing_view, dashboard_load, api_call.",
    ("product_events", "ts"): "Timestamp (UTC) when the event occurred.",
    ("product_events", "properties"): "Arbitrary event properties (JSONB).",
    # marketing_spend
    ("marketing_spend", "date"): "Date of spend.",
    (
        "marketing_spend",
        "channel",
    ): "Channel the money was spent on. Values: paid_search, paid_social, outbound.",
    ("marketing_spend", "campaign"): "Campaign name.",
    ("marketing_spend", "spend_usd"): "Spend in USD on this channel and date.",
    ("marketing_spend", "impressions"): "Impressions delivered.",
    ("marketing_spend", "clicks"): "Clicks received.",
    (
        "marketing_spend",
        "conversions",
    ): "Conversions attributed by the ad platform. Not always equal to new_paying_customers in cac_by_channel.",
    # referrals
    ("referrals", "referrer_id"): "Customer who made the referral. References customers.id.",
    (
        "referrals",
        "referred_id",
    ): "Customer who was referred and signed up. References customers.id.",
    ("referrals", "referred_at"): "Date of the referral.",
    # company_events
    ("company_events", "date"): "Date the event occurred.",
    ("company_events", "event_type"): "Short tag for the event type.",
    ("company_events", "description"): "Human-readable description.",
    # daily_metrics
    ("daily_metrics", "date"): "Day this row summarizes.",
    ("daily_metrics", "new_signups"): "Customers who signed up on this day.",
    ("daily_metrics", "new_activations"): "Customers whose activated_date is this day.",
    (
        "daily_metrics",
        "new_paid",
    ): "Customers who started their first paid subscription on this day.",
    (
        "daily_metrics",
        "churned",
    ): "Paid subscriptions that ended this day (excluding upgrades, downgrades, and price-increase migrations).",
    ("daily_metrics", "gross_new_mrr"): "MRR added from new subscriptions on this day.",
    ("daily_metrics", "expansion_mrr"): "MRR added from plan upgrades on this day.",
    ("daily_metrics", "contraction_mrr"): "MRR lost from plan downgrades on this day.",
    ("daily_metrics", "churn_mrr"): "MRR lost to churn on this day.",
    (
        "daily_metrics",
        "net_new_mrr",
    ): "gross_new_mrr + expansion_mrr - contraction_mrr - churn_mrr.",
    ("daily_metrics", "dau"): "Distinct customers who fired any product_event on this day.",
    (
        "daily_metrics",
        "mau",
    ): "Distinct customers who fired any product_event in the calendar month containing this day.",
    ("daily_metrics", "total_paying"): "Active paying customers at end of day.",
    # monthly_metrics
    ("monthly_metrics", "month"): "Month in YYYY-MM format.",
    ("monthly_metrics", "mrr_start"): "MRR at start of month.",
    ("monthly_metrics", "mrr_added"): "New MRR added during the month.",
    ("monthly_metrics", "expansion"): "Expansion MRR during the month.",
    ("monthly_metrics", "contraction"): "Contraction MRR during the month.",
    ("monthly_metrics", "churn"): "Churn MRR during the month.",
    ("monthly_metrics", "mrr_end"): "MRR at end of month.",
    ("monthly_metrics", "mom_growth_pct"): "Month-over-month MRR growth percentage.",
    ("monthly_metrics", "arr"): "Annualized recurring revenue (mrr_end * 12).",
    ("monthly_metrics", "paying_customers"): "Active paying customers at end of month.",
    ("monthly_metrics", "arpu"): "Average revenue per paying user (mrr_end / paying_customers).",
    ("monthly_metrics", "gross_churn_rate"): "churn / mrr_start * 100.",
    ("monthly_metrics", "net_churn_rate"): "(churn - expansion) / mrr_start * 100.",
    # cac_by_channel
    ("cac_by_channel", "month"): "Month in YYYY-MM format.",
    ("cac_by_channel", "channel"): "Acquisition channel.",
    ("cac_by_channel", "spend_usd"): "Total spend on this channel during the month.",
    (
        "cac_by_channel",
        "new_paying_customers",
    ): "Customers acquired via this channel who started a paid subscription during the month.",
    ("cac_by_channel", "cac_usd"): "spend_usd / new_paying_customers. NULL when no conversions.",
    # cohort_retention
    (
        "cohort_retention",
        "cohort_month",
    ): "Cohort definition. Month in which the customer first started a paid subscription.",
    ("cohort_retention", "age_months"): "Months since cohort_month. 0 = first month.",
    (
        "cohort_retention",
        "retained_pct",
    ): "Percent of the original cohort still on a paid subscription at this age.",
    ("cohort_retention", "surviving_mrr"): "Total MRR from this cohort still active at this age.",
}


# ============================================================================
# Schema introspection
# ============================================================================


class Column(TypedDict):
    name: str
    type: str
    nullable: bool
    description: str


class Table(TypedDict):
    name: str
    description: str
    columns: list[Column]


class Schema(TypedDict):
    tables: list[Table]


def get_schema() -> Schema:
    """Return the public schema as a structured dict.

    Shape:
        {
            "tables": [
                {
                    "name": "customers",
                    "description": "...",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False, "description": "..."},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    url = os.environ.get("SUPABASE_DB_URL_POOLED")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL_POOLED not set in environment")

    with psycopg.connect(url, connect_timeout=15, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            table_names = [r[0] for r in cur.fetchall()]

            tables: list[Table] = []
            for t in table_names:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (t,),
                )
                columns: list[Column] = []
                for col_name, data_type, is_nullable in cur.fetchall():
                    columns.append(
                        {
                            "name": col_name,
                            "type": data_type,
                            "nullable": is_nullable == "YES",
                            "description": COLUMN_DESCRIPTIONS.get((t, col_name), ""),
                        }
                    )
                tables.append(
                    {
                        "name": t,
                        "description": TABLE_DESCRIPTIONS.get(t, ""),
                        "columns": columns,
                    }
                )

    return {"tables": tables}


def format_as_markdown(schema: Schema) -> str:
    """Format a schema dict as markdown suitable for an LLM prompt or printout."""
    lines: list[str] = []
    for t in schema["tables"]:
        lines.append(f"### `{t['name']}`")
        if t["description"]:
            lines.append(t["description"])
        lines.append("")
        lines.append("| column | type | nullable | description |")
        lines.append("|---|---|---|---|")
        for c in t["columns"]:
            nullable = "yes" if c["nullable"] else "no"
            desc = c["description"] or ""
            lines.append(f"| `{c['name']}` | {c['type']} | {nullable} | {desc} |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    schema = get_schema()
    print(format_as_markdown(schema))
    print()
    print(f"Total tables: {len(schema['tables'])}")
    print(f"Total columns: {sum(len(t['columns']) for t in schema['tables'])}")

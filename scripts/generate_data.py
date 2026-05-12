"""Generate synthetic SaaS growth data for the Growth Analytics Agent.

Output: 11 CSV files in data/, reproducible via the SEED constant.
Read in the next phase by a loader script that populates Supabase.

Run: uv run python scripts/generate_data.py
"""

from __future__ import annotations

import calendar
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================================
# Config
# ============================================================================

SEED = 42

START_DATE = date(2024, 6, 1)
MONTHS = 24
END_DATE = date(2026, 5, 31)

N_CUSTOMERS_TARGET = 5000
N_PRODUCT_EVENTS_TARGET = 50_000

CHANNELS = ["paid_search", "paid_social", "organic", "referral", "outbound"]
CHANNEL_SHARE = [0.30, 0.25, 0.25, 0.05, 0.15]
PAID_CHANNELS = ["paid_search", "paid_social", "outbound"]

COUNTRIES = ["US", "BR", "UK", "DE", "CA", "AU", "FR", "IN"]
COUNTRY_WEIGHTS = [0.42, 0.12, 0.10, 0.08, 0.07, 0.06, 0.05, 0.10]

INDUSTRIES = ["SaaS", "E-commerce", "Marketing", "Finance", "Healthcare", "Education", "Other"]
COMPANY_SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"]
SIZE_WEIGHTS = [0.45, 0.30, 0.15, 0.07, 0.03]

PLAN_PRICE = {"Free": 0, "Starter": 29, "Pro": 99, "Business": 299}
PLAN_INITIAL_WEIGHTS = {"Free": 0.55, "Starter": 0.22, "Pro": 0.18, "Business": 0.05}

# Planted patterns
PRO_PRICE_HIKE_MONTH = 12
PRO_PRICE_NEW = 119
ACTIVATION_FEATURE_MONTH = 19
ACTIVATION_RATE_PRE = 0.35
ACTIVATION_RATE_POST = 0.52
REFERRAL_PROGRAM_MONTH = 16
OUTAGE_MONTH = 22
OUTAGE_DAY_OF_MONTH = 14
CAC_SPIKE_MONTH = 7

PAID_SEARCH_CAC_BASE = 42.0
PAID_SEARCH_CAC_SPIKE_RANGE = (60.0, 63.0)
PAID_SEARCH_CAC_PARTIAL_RECOVERY_MONTH = 10
PAID_SEARCH_CAC_RECOVERED = 48.0

COMPANY_MOMENTS = [
    (
        CAC_SPIKE_MONTH,
        1,
        "paid_search_cac_spike",
        "Paid-search CAC rose ~45% MoM due to bid inflation.",
    ),
    (
        PRO_PRICE_HIKE_MONTH,
        1,
        "pro_plan_price_increase",
        "Pro plan price raised from $99 to $119 (+20%).",
    ),
    (
        REFERRAL_PROGRAM_MONTH,
        5,
        "referral_program_launch",
        "Public referral program launched (give $50, get $50).",
    ),
    (
        ACTIVATION_FEATURE_MONTH,
        8,
        "activation_feature_ship",
        "In-app onboarding revamp shipped; activation rose from ~35% to ~52%.",
    ),
    (
        OUTAGE_MONTH,
        OUTAGE_DAY_OF_MONTH,
        "production_outage",
        "Day-long production outage; downstream churn for several weeks.",
    ),
]

EVENT_NAMES = [
    "page_view",
    "feature_use",
    "report_export",
    "data_import",
    "team_invite",
    "billing_view",
    "dashboard_load",
    "api_call",
]

OUT = Path("data")

# ============================================================================
# RNG and date helpers
# ============================================================================

py_rng: random.Random = random.Random(SEED)
np_rng: np.random.Generator = np.random.default_rng(SEED)


def setup_rng(seed: int) -> None:
    global py_rng, np_rng
    py_rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)


def month_start(m: int) -> date:
    y = START_DATE.year + (START_DATE.month - 1 + m - 1) // 12
    mo = (START_DATE.month - 1 + m - 1) % 12 + 1
    return date(y, mo, 1)


def month_end(m: int) -> date:
    if m >= MONTHS:
        return END_DATE
    return month_start(m + 1) - timedelta(days=1)


def days_in_month(m: int) -> int:
    s = month_start(m)
    return calendar.monthrange(s.year, s.month)[1]


def date_to_month_idx(d: date) -> int:
    return (d.year - START_DATE.year) * 12 + (d.month - START_DATE.month) + 1


def next_plan_up(plan: str) -> str:
    return {"Starter": "Pro", "Pro": "Business"}.get(plan, plan)


def next_plan_down(plan: str) -> str:
    return {"Business": "Pro", "Pro": "Starter"}.get(plan, plan)


# ============================================================================
# Raw table generators
# ============================================================================


def generate_company_profile() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "founded": (START_DATE - timedelta(days=730)).isoformat(),
                "industry": "B2B SaaS",
                "product_type": "Customer analytics platform",
            }
        ]
    )


def generate_customers() -> pd.DataFrame:
    base = np.linspace(150, 280, MONTHS)
    noise = np_rng.normal(0, 10, MONTHS)
    monthly = np.clip(base + noise, 50, None).astype(int)
    monthly = (monthly * (N_CUSTOMERS_TARGET / monthly.sum())).astype(int)

    rows = []
    cid = 1
    for m in range(1, MONTHS + 1):
        ms = month_start(m)
        dim = days_in_month(m)
        for _ in range(int(monthly[m - 1])):
            signup_date = ms + timedelta(days=py_rng.randint(0, dim - 1))

            channel = py_rng.choices(CHANNELS, weights=CHANNEL_SHARE, k=1)[0]
            country = py_rng.choices(COUNTRIES, weights=COUNTRY_WEIGHTS, k=1)[0]
            industry = py_rng.choice(INDUSTRIES)
            company_size = py_rng.choices(COMPANY_SIZES, weights=SIZE_WEIGHTS, k=1)[0]
            initial_plan = py_rng.choices(
                list(PLAN_INITIAL_WEIGHTS.keys()),
                weights=list(PLAN_INITIAL_WEIGHTS.values()),
                k=1,
            )[0]

            act_rate = (
                ACTIVATION_RATE_POST if m >= ACTIVATION_FEATURE_MONTH else ACTIVATION_RATE_PRE
            )
            if py_rng.random() < act_rate:
                lag = py_rng.randint(1, 14)
                activated_date = signup_date + timedelta(days=lag)
                if activated_date > END_DATE:
                    activated_date = None
            else:
                activated_date = None

            rows.append(
                {
                    "id": cid,
                    "signup_date": signup_date,
                    "activated_date": activated_date,
                    "source": channel,
                    "channel": channel,
                    "country": country,
                    "industry": industry,
                    "company_size": company_size,
                    "initial_plan": initial_plan,
                }
            )
            cid += 1

    return pd.DataFrame(rows)


def _churn_event(plan: str, m_idx: int) -> tuple[float, dict[str, float]]:
    """Return (probability of any lifecycle event this month, weights for {churn,upgrade,downgrade})."""
    base = {"Starter": 0.045, "Pro": 0.035, "Business": 0.020}.get(plan, 0.030)

    if plan == "Pro" and m_idx in (PRO_PRICE_HIKE_MONTH, PRO_PRICE_HIKE_MONTH + 1):
        base += 0.06
    if m_idx == OUTAGE_MONTH:
        base += 0.04

    weights = {"churn": 0.70, "upgrade": 0.20, "downgrade": 0.10}
    if plan == "Pro" and m_idx in (PRO_PRICE_HIKE_MONTH, PRO_PRICE_HIKE_MONTH + 1):
        weights = {"churn": 0.92, "upgrade": 0.03, "downgrade": 0.05}
    if m_idx == OUTAGE_MONTH:
        weights = {"churn": 0.92, "upgrade": 0.04, "downgrade": 0.04}
    if plan == "Business":
        weights = {"churn": 0.55, "upgrade": 0.0, "downgrade": 0.45}
    if plan == "Starter":
        weights = {"churn": 0.55, "upgrade": 0.40, "downgrade": 0.05}
    return base, weights


def generate_subscriptions(customers: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    sid = 1

    customers_sorted = customers.sort_values("signup_date").reset_index(drop=True)

    for _, c in customers_sorted.iterrows():
        plan = c["initial_plan"]
        if plan == "Free":
            # ~10% of Free customers convert to a paid plan within first 6 months
            if py_rng.random() < 0.10:
                lag_days = py_rng.randint(30, 180)
                start = c["signup_date"] + timedelta(days=lag_days)
                if start > END_DATE:
                    continue
                plan = py_rng.choices(["Starter", "Pro"], weights=[0.7, 0.3], k=1)[0]
            else:
                continue
        else:
            start = c["signup_date"]

        max_chain = 4
        for _step in range(max_chain):
            current = start
            ended_at = None
            end_reason = None
            new_plan = None

            while current <= END_DATE:
                m_idx = date_to_month_idx(current)
                if m_idx > MONTHS:
                    break
                p_event, weights = _churn_event(plan, m_idx)
                if py_rng.random() < p_event:
                    dim = days_in_month(m_idx)
                    day_offset = py_rng.randint(0, dim - 1)
                    candidate = month_start(m_idx) + timedelta(days=day_offset)
                    if candidate < current:
                        candidate = current + timedelta(days=py_rng.randint(1, 14))
                    if candidate > END_DATE:
                        break
                    event_type = py_rng.choices(
                        list(weights.keys()), weights=list(weights.values()), k=1
                    )[0]
                    ended_at = candidate
                    if event_type == "churn":
                        if plan == "Pro" and m_idx in (
                            PRO_PRICE_HIKE_MONTH,
                            PRO_PRICE_HIKE_MONTH + 1,
                        ):
                            end_reason = "price_increase"
                        elif m_idx == OUTAGE_MONTH:
                            end_reason = "outage_followup"
                        else:
                            end_reason = py_rng.choices(
                                ["voluntary", "involuntary_payment"], weights=[0.85, 0.15], k=1
                            )[0]
                    elif event_type == "upgrade":
                        end_reason = "upgrade"
                        new_plan = next_plan_up(plan)
                    else:
                        end_reason = "downgrade"
                        new_plan = next_plan_down(plan)
                    break
                current = month_start(m_idx + 1) if m_idx < MONTHS else END_DATE + timedelta(days=1)

            mrr = (
                PRO_PRICE_NEW
                if (plan == "Pro" and date_to_month_idx(start) >= PRO_PRICE_HIKE_MONTH)
                else PLAN_PRICE[plan]
            )
            rows.append(
                {
                    "id": sid,
                    "customer_id": int(c["id"]),
                    "plan": plan,
                    "mrr_usd": mrr,
                    "started_at": start,
                    "ended_at": ended_at,
                    "end_reason": end_reason,
                }
            )
            sid += 1

            if (
                end_reason in ("upgrade", "downgrade")
                and new_plan is not None
                and ended_at is not None
            ):
                plan = new_plan
                start = ended_at + timedelta(days=1)
                if start > END_DATE:
                    break
            else:
                break

    subs = pd.DataFrame(rows)

    # Pro price-hike migration: any Pro sub active across the m12 boundary gets
    # split into (old @ $99 closes at m11_end, new @ $119 starts at m12_start).
    m12_start = month_start(PRO_PRICE_HIKE_MONTH)
    m11_end = m12_start - timedelta(days=1)

    new_migration_rows: list[dict] = []
    for idx in subs.index:
        r = subs.loc[idx]
        if r["plan"] != "Pro":
            continue
        if r["mrr_usd"] >= PRO_PRICE_NEW:
            continue
        if r["started_at"] > m11_end:
            continue
        if r["ended_at"] is not None and r["ended_at"] <= m11_end:
            continue
        original_end = r["ended_at"]
        original_reason = r["end_reason"]
        subs.at[idx, "ended_at"] = m11_end
        subs.at[idx, "end_reason"] = "price_increase_migration"
        new_migration_rows.append(
            {
                "id": None,
                "customer_id": int(r["customer_id"]),
                "plan": "Pro",
                "mrr_usd": PRO_PRICE_NEW,
                "started_at": m12_start,
                "ended_at": original_end,
                "end_reason": original_reason
                if original_reason != "price_increase_migration"
                else None,
            }
        )

    if new_migration_rows:
        next_id = int(subs["id"].max()) + 1
        for mr in new_migration_rows:
            mr["id"] = next_id
            next_id += 1
        subs = pd.concat([subs, pd.DataFrame(new_migration_rows)], ignore_index=True)

    return subs.sort_values(["customer_id", "started_at"]).reset_index(drop=True)


def generate_product_events(customers: pd.DataFrame, subs: pd.DataFrame) -> pd.DataFrame:
    """For each day, sample some active customers and emit 1-3 events each."""
    # Build "active on day" lookup using subscriptions and free-but-activated customers.
    activated_customers = customers.dropna(subset=["activated_date"])

    # For each customer, the window [activated_date, churn_date or END_DATE]
    # We pool all activated customers as a flat (customer_id, activated_date) list.
    pool = activated_customers[["id", "activated_date"]].rename(columns={"id": "customer_id"})
    pool["customer_id"] = pool["customer_id"].astype(int)

    days = pd.date_range(START_DATE, END_DATE, freq="D").date

    rows = []
    eid = 1

    # Baseline events per day grows with active user base (cumulative activated customers).
    pool_sorted = pool.sort_values("activated_date").reset_index(drop=True)
    activated_dates = pool_sorted["activated_date"].tolist()
    activated_ids = pool_sorted["customer_id"].tolist()

    # Precompute "active set" cumulative count per day for sampling sizing.
    n_active_by_day: dict[date, int] = {}
    j = 0
    n = 0
    for d in days:
        while j < len(activated_dates) and activated_dates[j] <= d:
            j += 1
            n += 1
        n_active_by_day[d] = n

    total_active_user_days = sum(n_active_by_day.values())
    target_events = N_PRODUCT_EVENTS_TARGET
    avg_events_per_active_day = target_events / max(total_active_user_days, 1)

    outage_date = month_start(OUTAGE_MONTH) + timedelta(days=OUTAGE_DAY_OF_MONTH - 1)

    for d in days:
        n_active = n_active_by_day[d]
        if n_active == 0:
            continue
        mult = 0.05 if d == outage_date else 1.0
        # Expected number of events today
        lam = n_active * avg_events_per_active_day * mult
        n_events = int(np_rng.poisson(lam))
        if n_events <= 0:
            continue
        # Sample customer_ids from the active pool (indices 0..j-1 at this point in time)
        active_count = n_active
        if active_count <= 0:
            continue
        cust_indices = np_rng.integers(0, active_count, size=n_events)
        for ci in cust_indices:
            customer_id = activated_ids[ci]
            event_name = py_rng.choice(EVENT_NAMES)
            # Random time within the day
            secs = py_rng.randint(0, 24 * 3600 - 1)
            ts = datetime.combine(d, time(0, 0)) + timedelta(seconds=secs)
            props = {"path": f"/{event_name.replace('_', '-')}"}
            if event_name == "feature_use":
                props["feature"] = py_rng.choice(["cohorts", "funnels", "dashboards", "alerts"])
            rows.append(
                {
                    "id": eid,
                    "customer_id": int(customer_id),
                    "event_name": event_name,
                    "ts": ts,
                    "properties": json.dumps(props),
                }
            )
            eid += 1

    return pd.DataFrame(rows)


def generate_marketing_spend() -> pd.DataFrame:
    """Daily spend by paid channel. Paid-search spend lifts in m7-9 with no commensurate conversions."""
    days = pd.date_range(START_DATE, END_DATE, freq="D").date
    rows = []
    for d in days:
        m_idx = date_to_month_idx(d)
        for channel in PAID_CHANNELS:
            if channel == "paid_search":
                base = 1300.0 / 30  # $43/day baseline
                if m_idx in (CAC_SPIKE_MONTH, CAC_SPIKE_MONTH + 1, CAC_SPIKE_MONTH + 2):
                    base *= 1.45
                elif m_idx >= PAID_SEARCH_CAC_PARTIAL_RECOVERY_MONTH:
                    base *= 1.15
            elif channel == "paid_social":
                base = 28.0
            else:  # outbound
                base = 18.0

            spend = max(0.0, base + np_rng.normal(0, base * 0.10))
            impressions = int(spend * py_rng.uniform(70, 110))
            clicks = int(impressions * py_rng.uniform(0.015, 0.035))
            conversions = int(clicks * py_rng.uniform(0.04, 0.09))

            campaign = {
                "paid_search": "google_brand_and_keywords",
                "paid_social": "linkedin_lookalike",
                "outbound": "sdr_outbound_q",
            }[channel]

            rows.append(
                {
                    "date": d,
                    "channel": channel,
                    "campaign": campaign,
                    "spend_usd": round(spend, 2),
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": conversions,
                }
            )

    return pd.DataFrame(rows)


def generate_referrals(customers: pd.DataFrame) -> pd.DataFrame:
    days = pd.date_range(START_DATE, END_DATE, freq="D").date
    rows = []
    for d in days:
        m_idx = date_to_month_idx(d)
        # ~6/month pre-program-launch, ~80/month after
        lam = 0.2 if m_idx < REFERRAL_PROGRAM_MONTH else 2.7
        n_refs = int(np_rng.poisson(lam))
        for _ in range(n_refs):
            eligible_referrers = customers[customers["signup_date"] <= d - timedelta(days=14)]
            if len(eligible_referrers) == 0:
                continue
            referrer = eligible_referrers.sample(
                1, random_state=int(np_rng.integers(0, 1_000_000))
            ).iloc[0]
            referred = customers[customers["signup_date"] >= d - timedelta(days=14)]
            referred = referred[referred["id"] != referrer["id"]]
            if len(referred) == 0:
                continue
            referred_row = referred.sample(1, random_state=int(np_rng.integers(0, 1_000_000))).iloc[
                0
            ]
            rows.append(
                {
                    "referrer_id": int(referrer["id"]),
                    "referred_id": int(referred_row["id"]),
                    "referred_at": d,
                }
            )

    return pd.DataFrame(rows)


def generate_company_events() -> pd.DataFrame:
    rows = []
    for m, dom, event_type, description in COMPANY_MOMENTS:
        rows.append(
            {
                "date": month_start(m) + timedelta(days=dom - 1),
                "event_type": event_type,
                "description": description,
            }
        )

    # A few quieter background events for realism
    background = [
        (2, 5, "team_milestone", "Crossed 1,000 signups."),
        (4, 15, "hiring", "Hired first growth analyst."),
        (9, 20, "investor_update", "Closed seed extension."),
        (14, 10, "product_launch", "Integrations marketplace shipped."),
        (17, 3, "rebrand", "Public rebrand and new homepage."),
        (20, 22, "partnership", "Co-marketing partnership with Acme."),
    ]
    for m, dom, event_type, description in background:
        rows.append(
            {
                "date": month_start(m) + timedelta(days=dom - 1),
                "event_type": event_type,
                "description": description,
            }
        )

    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


# ============================================================================
# Rollup tables
# ============================================================================


def _active_subs_on(subs: pd.DataFrame, d: date) -> pd.DataFrame:
    started = subs["started_at"] <= d
    ended = subs["ended_at"].isna() | (subs["ended_at"] > d)
    return subs[started & ended]


def roll_daily_metrics(
    customers: pd.DataFrame,
    subs: pd.DataFrame,
    events: pd.DataFrame,
) -> pd.DataFrame:
    days = pd.date_range(START_DATE, END_DATE, freq="D").date

    new_signups = customers.groupby("signup_date").size().to_dict()
    new_activations = (
        customers.dropna(subset=["activated_date"]).groupby("activated_date").size().to_dict()
    )

    paid_subs = subs[subs["mrr_usd"] > 0]
    first_paid_by_customer = paid_subs.sort_values("started_at").groupby("customer_id").first()
    new_paid_by_day = first_paid_by_customer.groupby("started_at").size().to_dict()

    churned_subs = paid_subs[
        paid_subs["end_reason"].isin(
            ["voluntary", "involuntary_payment", "price_increase", "outage_followup"]
        )
    ]
    churned_by_day = churned_subs.groupby("ended_at").size().to_dict()
    churn_mrr_by_day = churned_subs.groupby("ended_at")["mrr_usd"].sum().to_dict()

    new_mrr_by_day = paid_subs[paid_subs["end_reason"] != "price_increase_migration"]
    # Exclude migration-replacement subs from "new" MRR (they were already counted as Pro)
    is_migration_replacement = (paid_subs["mrr_usd"] == PRO_PRICE_NEW) & (
        paid_subs["started_at"] == month_start(PRO_PRICE_HIKE_MONTH)
    )
    new_mrr_by_day = (
        paid_subs[~is_migration_replacement].groupby("started_at")["mrr_usd"].sum().to_dict()
    )

    # Expansion / contraction: pair (closed sub with reason=upgrade/downgrade) with
    # the next sub for the same customer starting the day after.
    expansion_by_day: dict[date, float] = {}
    contraction_by_day: dict[date, float] = {}
    for _cust_id, group in paid_subs.sort_values(["customer_id", "started_at"]).groupby(
        "customer_id"
    ):
        rows = group.to_dict("records")
        for i in range(len(rows) - 1):
            cur, nxt = rows[i], rows[i + 1]
            if cur["end_reason"] == "upgrade":
                d = nxt["started_at"]
                expansion_by_day[d] = expansion_by_day.get(d, 0) + (nxt["mrr_usd"] - cur["mrr_usd"])
            elif cur["end_reason"] == "downgrade":
                d = nxt["started_at"]
                contraction_by_day[d] = contraction_by_day.get(d, 0) + (
                    cur["mrr_usd"] - nxt["mrr_usd"]
                )

    # DAU: distinct customers with an event on day d
    events_with_day = events.copy()
    events_with_day["day"] = pd.to_datetime(events_with_day["ts"]).dt.date
    dau_by_day = events_with_day.groupby("day")["customer_id"].nunique().to_dict()

    # MAU: distinct customers with events in the calendar month of day d
    events_with_day["month"] = pd.to_datetime(events_with_day["ts"]).dt.to_period("M")
    mau_by_month = events_with_day.groupby("month")["customer_id"].nunique().to_dict()

    rows = []
    for d in days:
        active = _active_subs_on(paid_subs, d)
        total_paying = int(active["customer_id"].nunique())

        gross_new = float(new_mrr_by_day.get(d, 0.0))
        expansion = float(expansion_by_day.get(d, 0.0))
        contraction = float(contraction_by_day.get(d, 0.0))
        churn_mrr = float(churn_mrr_by_day.get(d, 0.0))

        month_period = pd.Period(d, freq="M")
        rows.append(
            {
                "date": d,
                "new_signups": int(new_signups.get(d, 0)),
                "new_activations": int(new_activations.get(d, 0)),
                "new_paid": int(new_paid_by_day.get(d, 0)),
                "churned": int(churned_by_day.get(d, 0)),
                "gross_new_mrr": round(gross_new, 2),
                "expansion_mrr": round(expansion, 2),
                "contraction_mrr": round(contraction, 2),
                "churn_mrr": round(churn_mrr, 2),
                "net_new_mrr": round(gross_new + expansion - contraction - churn_mrr, 2),
                "dau": int(dau_by_day.get(d, 0)),
                "mau": int(mau_by_month.get(month_period, 0)),
                "total_paying": total_paying,
            }
        )

    return pd.DataFrame(rows)


def roll_monthly_metrics(subs: pd.DataFrame) -> pd.DataFrame:
    paid_subs = subs[subs["mrr_usd"] > 0]
    rows = []
    for m in range(1, MONTHS + 1):
        ms = month_start(m)
        me = month_end(m)

        active_start = _active_subs_on(paid_subs, ms - timedelta(days=1))
        active_end = _active_subs_on(paid_subs, me)

        mrr_start = float(active_start["mrr_usd"].sum())
        mrr_end = float(active_end["mrr_usd"].sum())

        # MRR added: sum of new subs started in this month (excluding migration replacements)
        is_migration_replacement = (paid_subs["mrr_usd"] == PRO_PRICE_NEW) & (
            paid_subs["started_at"] == month_start(PRO_PRICE_HIKE_MONTH)
        )
        new_in_month = paid_subs[
            (paid_subs["started_at"] >= ms)
            & (paid_subs["started_at"] <= me)
            & (~is_migration_replacement)
        ]
        mrr_added = float(new_in_month["mrr_usd"].sum())

        churned_in_month = paid_subs[
            paid_subs["ended_at"].between(ms, me)
            & paid_subs["end_reason"].isin(
                ["voluntary", "involuntary_payment", "price_increase", "outage_followup"]
            )
        ]
        churn_mrr = float(churned_in_month["mrr_usd"].sum())

        # Expansion / contraction from upgrade/downgrade pairs in this month
        expansion_mrr = 0.0
        contraction_mrr = 0.0
        for _cust_id, group in paid_subs.sort_values(["customer_id", "started_at"]).groupby(
            "customer_id"
        ):
            recs = group.to_dict("records")
            for i in range(len(recs) - 1):
                cur, nxt = recs[i], recs[i + 1]
                if ms <= nxt["started_at"] <= me:
                    if cur["end_reason"] == "upgrade":
                        expansion_mrr += nxt["mrr_usd"] - cur["mrr_usd"]
                    elif cur["end_reason"] == "downgrade":
                        contraction_mrr += cur["mrr_usd"] - nxt["mrr_usd"]

        paying_customers = int(active_end["customer_id"].nunique())
        arpu = mrr_end / paying_customers if paying_customers else 0.0
        gross_churn_rate = (churn_mrr / mrr_start * 100) if mrr_start else 0.0
        net_churn_rate = ((churn_mrr - expansion_mrr) / mrr_start * 100) if mrr_start else 0.0
        mom_growth_pct = ((mrr_end - mrr_start) / mrr_start * 100) if mrr_start else 0.0

        rows.append(
            {
                "month": ms.strftime("%Y-%m"),
                "mrr_start": round(mrr_start, 2),
                "mrr_added": round(mrr_added, 2),
                "expansion": round(expansion_mrr, 2),
                "contraction": round(contraction_mrr, 2),
                "churn": round(churn_mrr, 2),
                "mrr_end": round(mrr_end, 2),
                "mom_growth_pct": round(mom_growth_pct, 2),
                "arr": round(mrr_end * 12, 2),
                "paying_customers": paying_customers,
                "arpu": round(arpu, 2),
                "gross_churn_rate": round(gross_churn_rate, 2),
                "net_churn_rate": round(net_churn_rate, 2),
            }
        )

    return pd.DataFrame(rows)


def roll_cac_by_channel(
    customers: pd.DataFrame,
    subs: pd.DataFrame,
    marketing: pd.DataFrame,
) -> pd.DataFrame:
    paid_subs = subs[subs["mrr_usd"] > 0]
    first_paid = paid_subs.sort_values("started_at").groupby("customer_id").first().reset_index()
    first_paid = first_paid.merge(
        customers[["id", "channel"]], left_on="customer_id", right_on="id", how="left"
    )
    first_paid["month"] = first_paid["started_at"].apply(
        lambda d: month_start(date_to_month_idx(d)).strftime("%Y-%m")
    )

    marketing = marketing.copy()
    marketing["month"] = marketing["date"].apply(
        lambda d: month_start(date_to_month_idx(d)).strftime("%Y-%m")
    )
    spend_by = marketing.groupby(["month", "channel"])["spend_usd"].sum().reset_index()

    new_paid_by = (
        first_paid.groupby(["month", "channel"]).size().reset_index(name="new_paying_customers")
    )

    out = spend_by.merge(new_paid_by, on=["month", "channel"], how="left")
    out["new_paying_customers"] = out["new_paying_customers"].fillna(0).astype(int)
    out["cac_usd"] = out.apply(
        lambda r: (
            round(r["spend_usd"] / r["new_paying_customers"], 2)
            if r["new_paying_customers"] > 0
            else None
        ),
        axis=1,
    )
    out["spend_usd"] = out["spend_usd"].round(2)
    return out.sort_values(["month", "channel"]).reset_index(drop=True)


def roll_cohort_retention(customers: pd.DataFrame, subs: pd.DataFrame) -> pd.DataFrame:
    paid_subs = subs[subs["mrr_usd"] > 0]
    first_paid = paid_subs.sort_values("started_at").groupby("customer_id").first().reset_index()
    first_paid["cohort_month_idx"] = first_paid["started_at"].apply(date_to_month_idx)

    rows = []
    for cohort_m in range(1, MONTHS + 1):
        cohort_customers = first_paid[first_paid["cohort_month_idx"] == cohort_m]
        cohort_size = len(cohort_customers)
        if cohort_size == 0:
            continue
        max_age = MONTHS - cohort_m
        for age in range(0, max_age + 1):
            check_m = cohort_m + age
            check_d = month_end(check_m)
            active = _active_subs_on(paid_subs, check_d)
            active_in_cohort = active[active["customer_id"].isin(cohort_customers["customer_id"])]
            retained = int(active_in_cohort["customer_id"].nunique())
            surviving_mrr = float(active_in_cohort["mrr_usd"].sum())
            rows.append(
                {
                    "cohort_month": month_start(cohort_m).strftime("%Y-%m"),
                    "age_months": age,
                    "retained_pct": round(retained / cohort_size * 100, 2),
                    "surviving_mrr": round(surviving_mrr, 2),
                }
            )

    return pd.DataFrame(rows)


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    setup_rng(SEED)
    OUT.mkdir(exist_ok=True)

    print("Generating company_profile...")
    cp = generate_company_profile()
    cp.to_csv(OUT / "company_profile.csv", index=False)

    print("Generating customers...")
    customers = generate_customers()
    customers.to_csv(OUT / "customers.csv", index=False, date_format="%Y-%m-%d")

    print("Generating subscriptions...")
    subs = generate_subscriptions(customers)
    subs.to_csv(OUT / "subscriptions.csv", index=False, date_format="%Y-%m-%d")

    print("Generating product_events...")
    events = generate_product_events(customers, subs)
    events.to_csv(OUT / "product_events.csv", index=False)

    print("Generating marketing_spend...")
    marketing = generate_marketing_spend()
    marketing.to_csv(OUT / "marketing_spend.csv", index=False, date_format="%Y-%m-%d")

    print("Generating referrals...")
    referrals = generate_referrals(customers)
    referrals.to_csv(OUT / "referrals.csv", index=False, date_format="%Y-%m-%d")

    print("Generating company_events...")
    company_events = generate_company_events()
    company_events.to_csv(OUT / "company_events.csv", index=False, date_format="%Y-%m-%d")

    print("Rolling daily_metrics...")
    daily = roll_daily_metrics(customers, subs, events)
    daily.to_csv(OUT / "daily_metrics.csv", index=False, date_format="%Y-%m-%d")

    print("Rolling monthly_metrics...")
    monthly = roll_monthly_metrics(subs)
    monthly.to_csv(OUT / "monthly_metrics.csv", index=False)

    print("Rolling cac_by_channel...")
    cac = roll_cac_by_channel(customers, subs, marketing)
    cac.to_csv(OUT / "cac_by_channel.csv", index=False)

    print("Rolling cohort_retention...")
    cohort = roll_cohort_retention(customers, subs)
    cohort.to_csv(OUT / "cohort_retention.csv", index=False)

    print()
    print("Done. Summary:")
    for table, df in [
        ("company_profile", cp),
        ("customers", customers),
        ("subscriptions", subs),
        ("product_events", events),
        ("marketing_spend", marketing),
        ("referrals", referrals),
        ("company_events", company_events),
        ("daily_metrics", daily),
        ("monthly_metrics", monthly),
        ("cac_by_channel", cac),
        ("cohort_retention", cohort),
    ]:
        size_kb = (OUT / f"{table}.csv").stat().st_size / 1024
        print(f"  {table:18s}  {len(df):>7,} rows  {size_kb:>8.1f} KB")


if __name__ == "__main__":
    main()

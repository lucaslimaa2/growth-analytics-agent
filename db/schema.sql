-- Schema for the Growth Analytics Agent demo dataset.
-- Idempotent: drops everything and recreates from scratch.
-- Loaded by scripts/load_data.py via COPY from data/*.csv.

DROP TABLE IF EXISTS cohort_retention   CASCADE;
DROP TABLE IF EXISTS cac_by_channel     CASCADE;
DROP TABLE IF EXISTS monthly_metrics    CASCADE;
DROP TABLE IF EXISTS daily_metrics      CASCADE;
DROP TABLE IF EXISTS company_events     CASCADE;
DROP TABLE IF EXISTS referrals          CASCADE;
DROP TABLE IF EXISTS marketing_spend    CASCADE;
DROP TABLE IF EXISTS product_events     CASCADE;
DROP TABLE IF EXISTS subscriptions      CASCADE;
DROP TABLE IF EXISTS customers          CASCADE;
DROP TABLE IF EXISTS company_profile    CASCADE;

-- ============================================================================
-- Raw tables
-- ============================================================================

CREATE TABLE company_profile (
    founded       DATE        NOT NULL,
    industry      TEXT        NOT NULL,
    product_type  TEXT        NOT NULL
);

CREATE TABLE customers (
    id             INTEGER PRIMARY KEY,
    signup_date    DATE    NOT NULL,
    activated_date DATE,
    source         TEXT    NOT NULL,
    channel        TEXT    NOT NULL,
    country        TEXT    NOT NULL,
    industry       TEXT    NOT NULL,
    company_size   TEXT    NOT NULL,
    initial_plan   TEXT    NOT NULL
);

CREATE TABLE subscriptions (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    plan        TEXT    NOT NULL,
    mrr_usd     NUMERIC(10, 2) NOT NULL,
    started_at  DATE    NOT NULL,
    ended_at    DATE,
    end_reason  TEXT
);

CREATE TABLE product_events (
    id          INTEGER     PRIMARY KEY,
    customer_id INTEGER     NOT NULL,
    event_name  TEXT        NOT NULL,
    ts          TIMESTAMP   NOT NULL,
    properties  JSONB
);

CREATE TABLE marketing_spend (
    date         DATE    NOT NULL,
    channel      TEXT    NOT NULL,
    campaign     TEXT    NOT NULL,
    spend_usd    NUMERIC(12, 2) NOT NULL,
    impressions  INTEGER NOT NULL,
    clicks       INTEGER NOT NULL,
    conversions  INTEGER NOT NULL
);

CREATE TABLE referrals (
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    referred_at DATE    NOT NULL
);

CREATE TABLE company_events (
    date        DATE NOT NULL,
    event_type  TEXT NOT NULL,
    description TEXT NOT NULL
);

-- ============================================================================
-- Rollup tables
-- ============================================================================

CREATE TABLE daily_metrics (
    date              DATE PRIMARY KEY,
    new_signups       INTEGER NOT NULL,
    new_activations   INTEGER NOT NULL,
    new_paid          INTEGER NOT NULL,
    churned           INTEGER NOT NULL,
    gross_new_mrr     NUMERIC(12, 2) NOT NULL,
    expansion_mrr     NUMERIC(12, 2) NOT NULL,
    contraction_mrr   NUMERIC(12, 2) NOT NULL,
    churn_mrr         NUMERIC(12, 2) NOT NULL,
    net_new_mrr       NUMERIC(12, 2) NOT NULL,
    dau               INTEGER NOT NULL,
    mau               INTEGER NOT NULL,
    total_paying      INTEGER NOT NULL
);

CREATE TABLE monthly_metrics (
    month             TEXT PRIMARY KEY,
    mrr_start         NUMERIC(12, 2) NOT NULL,
    mrr_added         NUMERIC(12, 2) NOT NULL,
    expansion         NUMERIC(12, 2) NOT NULL,
    contraction       NUMERIC(12, 2) NOT NULL,
    churn             NUMERIC(12, 2) NOT NULL,
    mrr_end           NUMERIC(12, 2) NOT NULL,
    mom_growth_pct    NUMERIC(6, 2)  NOT NULL,
    arr               NUMERIC(14, 2) NOT NULL,
    paying_customers  INTEGER        NOT NULL,
    arpu              NUMERIC(10, 2) NOT NULL,
    gross_churn_rate  NUMERIC(6, 2)  NOT NULL,
    net_churn_rate    NUMERIC(6, 2)  NOT NULL
);

CREATE TABLE cac_by_channel (
    month                  TEXT    NOT NULL,
    channel                TEXT    NOT NULL,
    spend_usd              NUMERIC(12, 2) NOT NULL,
    new_paying_customers   INTEGER NOT NULL,
    cac_usd                NUMERIC(10, 2),
    PRIMARY KEY (month, channel)
);

CREATE TABLE cohort_retention (
    cohort_month   TEXT    NOT NULL,
    age_months     INTEGER NOT NULL,
    retained_pct   NUMERIC(6, 2)  NOT NULL,
    surviving_mrr  NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (cohort_month, age_months)
);

-- ============================================================================
-- Rate limiting log: tracks recent per-IP requests so we can throttle abuse.
-- Created separately (IF NOT EXISTS) because the data-DROP block above must
-- not wipe the rate-limit history every time we reload synthetic data.
-- ============================================================================

CREATE TABLE IF NOT EXISTS rate_limits (
    ip TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS rate_limits_ip_ts ON rate_limits(ip, ts DESC);

-- ============================================================================
-- Row Level Security: enabled with no policies, blocks PostgREST anon access.
-- The postgres role used by our loader and agent bypasses RLS automatically.
-- ============================================================================

ALTER TABLE company_profile  ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers        ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketing_spend  ENABLE ROW LEVEL SECURITY;
ALTER TABLE referrals        ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_metrics    ENABLE ROW LEVEL SECURITY;
ALTER TABLE monthly_metrics  ENABLE ROW LEVEL SECURITY;
ALTER TABLE cac_by_channel   ENABLE ROW LEVEL SECURITY;
ALTER TABLE cohort_retention ENABLE ROW LEVEL SECURITY;

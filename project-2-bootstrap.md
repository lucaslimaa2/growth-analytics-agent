# Growth Analytics Agent | Project #2 of my AI portfolio

I'm Antonio Lucas Lima. Growth + Data + AI professional based in Brazil.
Co-founder of VEGA, a Web3 growth and go-to-market agency. Previously
Analytics Engineer (HR) at Coca-Cola and Community & Growth Analyst at FTX.
Personal site: https://lucaslima.xyz. AI portfolio:
https://lucaslima.xyz/ai-portfolio.

# What this project is

A single-page web app combining two things:

1. A **fixed dashboard** that shows the demo company's key growth metrics
   the moment the page loads (MRR, ARR, MoM growth, gross churn, LTV / CAC,
   plus 2-3 charts and an annotated events timeline).

2. A **chat agent** below the dashboard, where someone asks plain-English
   questions about that growth data and the agent autonomously queries the
   database, runs analyses (cohort retention, funnel conversion,
   period-over-period comparison, segment breakdown), generates interactive
   Plotly charts, and returns insights with concrete recommendations.

The agent is the new part. It is a real agent (multi-step reasoning with
tool use), not a RAG chatbot. The agent decides which tools to call: query
the SQL database, inspect the schema, run a cohort analysis, generate a
chart, escalate to a stronger model, and so on.

This is project #2 of my AI portfolio. It will be deployed at a subdomain
like agent.lucaslima.xyz and listed as a card on
lucaslima.xyz/ai-portfolio next to project #1.

# Project #1 reference (already built and deployed)

I built a RAG chatbot grounded in my resume and VEGA case studies. Live
at https://chat.lucaslima.xyz. Code at
https://github.com/lucaslimaa2/lucas-personal-rag. Read the README and
repo structure for patterns I want to mirror.

Patterns from project #1 I want to reuse for project #2:
- Vanilla HTML / CSS / JS frontend (no React, no framework)
- Python serverless functions on Vercel for the backend
- Anthropic SDK directly, no LangChain / LlamaIndex / agent frameworks
- Single Vercel deploy, single repo
- Custom subdomain via Vercel + DNS
- Card on lucaslima.xyz/ai-portfolio linking to the live agent
- Production-grade hygiene: per-IP rate limiting, prompt caching on the
  system prompt, structured request logging, dev / prod env separation
- README in the repo with: live demo link, architecture diagram, stack
  reasoning, repo layout, local setup, design decisions
- GitHub Actions for any scheduled jobs (instead of Vercel cron)
- Same visual style as my portfolio: white bg, black text, accent
  #1a1a2e, Inter for body, Playfair Display for display

# What's new in project #2 (vs project #1)

| Concept | Project #1 | Project #2 |
|---|---|---|
| UI | Chat only | Dashboard above + chat below, single page |
| Loop shape | Single retrieve then answer | Multi-step agent loop with tool use |
| Knowledge source | Pinecone vectors of Notion docs | Supabase Postgres (synthetic SaaS data) |
| LLM interaction | Stuff context, get answer | Tool calling: Claude returns "call X with args Y", I execute, return results, loop |
| Frontend rendering | Text bubbles only | KPI tiles + Plotly charts + chat bubbles + collapsible tool-call steps + inline charts in chat |
| Streaming | Optional | Required for the chat: SSE streams tool calls AND final tokens |
| Embeddings | Yes, OpenAI | Not needed in v1 (skipped) |

# How I want to work (IMPORTANT)

I'm a non-coder learner. My #1 goal is to LEARN, not to ship fast.

- BEFORE each phase: explain what we're about to build, why this approach,
  what alternatives exist with tradeoffs.
- AFTER writing code: walk me through it conceptually, what each block
  does and why it's structured that way.
- Don't dump big code blocks without teaching scaffolding.
- I can read code but can't write much from scratch. Assume basics, explain
  anything domain-specific.
- For pure execution tasks (rename this file, run this command), skip the
  teaching scaffolding and just do it.
- Build first, optimize on signal. Premature optimization for a portfolio
  project is engineering theater. Add hardening (rate limit, caching, eval,
  reranking, hybrid search, etc.) when there's a felt reason, not upfront.

# Tech stack (locked in, don't second-guess)

| Layer | Tool |
|---|---|
| Runtime | Python 3.12 managed by uv |
| LLM | Claude Haiku 4.5 (claude-haiku-4-5-20251001), with optional Sonnet escalation as a tool the agent can call |
| Main DB + vector DB | Supabase (PostgreSQL with pgvector extension) |
| Charts | Plotly. Server generates Plotly JSON spec, frontend renders with Plotly.js |
| Frontend | Vanilla HTML + CSS + JS, no framework |
| Streaming (chat only) | Server-Sent Events (SSE) |
| Agent framework | None. Anthropic SDK's tool use directly |
| Hosting | Vercel (static frontend + Python serverless functions in /api/) |
| Domain | agent.lucaslima.xyz (or growth-agent.lucaslima.xyz) |

I do NOT want TypeScript / React / Next.js / LangChain / LlamaIndex
anywhere in this project.

# Project location

Start the new project OUTSIDE OneDrive (e.g., `C:\dev\growth-analytics-agent\`).
Cloud-synced folders + dev workflows = corruption risk on the `.git` folder
and sync storms on `.venv`. Backup happens via GitHub, not OneDrive.

# Architecture

```
                Browser (vanilla HTML/JS + Plotly.js)
                          |
        +-----------------+-------------------+
        |                                     |
        |  GET /api/dashboard                 |  POST /api/chat
        |  (once on page load)                |  (per question,
        |                                     |   SSE stream back)
        v                                     v
  Vercel Python serverless          Vercel Python serverless
    api/dashboard.py                   api/chat.py
        |                                     |
        |                                     v
        |                            AGENT LOOP (agent.py + chat.py)
        |                                     |
        |                          Claude Haiku 4.5 (main loop)
        |                          decides which tool to call next
        |                                     |
        |                                     v
        |                              Tools (catalog below)
        |                                     |
        +--------------+----------------------+
                       v
                   Supabase
            PostgreSQL + pgvector
            raw tables + rollup tables
```

# Sample dataset

Synthetic SaaS growth data, generated by a Python script and loaded into
Supabase. Synthetic chosen over a real dataset so I have full control
over what's in the data and can plant interesting patterns for the agent
to discover.

## Raw tables

```sql
company_profile (1 row: founded, industry, product_type)

customers (~5,000 rows)
  id, signup_date, activated_date, source, channel, country,
  industry, company_size, initial_plan

subscriptions (~6,000 rows)
  id, customer_id, plan, mrr_usd, started_at, ended_at, end_reason

product_events (~50,000 rows)
  id, customer_id, event_name, ts, properties (JSONB)

marketing_spend (~3,000 rows)
  date, channel, campaign, spend_usd, impressions, clicks, conversions

referrals (~500 rows)
  referrer_id, referred_id, referred_at

company_events (~25 rows)
  date, event_type, description
```

## Pre-computed rollup tables

So the agent and the dashboard don't recompute everything from raw.

```sql
daily_metrics (~720 rows: 24 months x 30 days)
  date, new_signups, new_activations, new_paid, churned,
  gross_new_mrr, expansion_mrr, contraction_mrr, churn_mrr,
  net_new_mrr, dau, mau, total_paying

monthly_metrics (24 rows)
  month, mrr_start, mrr_added, expansion, contraction, churn,
  mrr_end, mom_growth_pct, arr, paying_customers, arpu,
  gross_churn_rate, net_churn_rate

cac_by_channel (~96 rows)
  month, channel, spend, new_paying_customers, cac_usd

cohort_retention (~288 rows)
  cohort_month, age_months, retained_pct, surviving_mrr
```

Total: ~65,000 rows. About 13 MB. Well under Supabase free-tier limits.

## Planted patterns (the demo magic)

Five interesting moments the agent can find:

1. Month 7: paid-search CAC spikes from $42 to $61. MoM growth flatlines.
2. Month 12: Pro plan price raised 20%. Expansion revenue jumps, Pro plan
   churn spikes for 2 months.
3. Month 16: referral program launched. Referrals 4x, K-factor climbs.
4. Month 19: activation-improving feature launch. Activation rate jumps
   from 35% to 52%.
5. Month 22: 1-day outage on March 14. DAU collapses, brief churn spike.

# Fixed dashboard

Shown above the chat on every page load. Single GET request to
/api/dashboard returns the data; frontend renders the layout.

## What's on the dashboard

KPI tiles (current value + delta vs prior period):
- MRR
- ARR
- MoM growth %
- Gross churn rate
- LTV / CAC ratio

Charts:
- MRR over time (24-month line)
- Cohort retention heatmap

Annotated events timeline:
- The 5 planted business moments listed with their dates and descriptions

Dashboard data is **live** from Supabase rollup tables on each page load.
Single round-trip, ~50ms response.

# Tool catalog (chat agent)

10 core tools plus 4 optional. Anthropic SDK tool use, defined as JSON
schemas, executed by the backend, results fed back to the model.

## Tier 1: data access

| Tool | Purpose |
|---|---|
| get_schema() | Returns table/column descriptions. Agent uses this to plan queries. |
| query_database(sql) | Runs a read-only SQL query against Supabase. Capped at 500 rows. |

## Tier 2: pre-computed metrics ("growth analytics vocabulary")

These embody growth-analytics expertise so the agent doesn't reinvent
cohort math from raw SQL every time.

| Tool | What it does |
|---|---|
| get_metric(name, period) | Pre-defined: mrr, arr, cac, ltv, mom_growth, arpu, gross_churn, net_churn, payback_months. Returns time series or scalar. |
| cohort_analysis(cohort_by, segment_by, window) | Auto-cohorts customers, returns retention matrix. |
| funnel_analysis(steps, segment) | Given a sequence of event names, computes conversion rates between each step. |
| time_series_compare(metric, period_a, period_b) | Period-over-period: total delta, percent change, dimension breakdown showing which segment drove the change. |
| segment_breakdown(metric, dimension, period) | Break a metric down by a dimension (source, country, plan). |

## Tier 3: output

| Tool | What it does |
|---|---|
| make_chart(plotly_spec) | Validates and emits a Plotly JSON spec for the frontend to render. |
| make_table(rows, columns, title) | Emits a formatted data table for the frontend. |
| summarize_findings(insights[], recommendations[]) | Structured final output. Ends the agent loop. Renders as a styled summary section. |

## Tier 4: optional (add in later phases, not Phase 4)

| Tool | What it does |
|---|---|
| anomaly_detection(metric, lookback) | Statistical outlier detection. Returns dates + severity. |
| compare_to_benchmarks(metric, value) | Compares to SaaS industry benchmarks. e.g., "Your 5% gross churn is above the 3-7% SMB SaaS benchmark." |
| generate_growth_plan(finding) | Given an insight, produces 3 concrete recommended actions. |
| escalate_to_sonnet(reason) | Hand the next step to Sonnet for harder reasoning. |

# Sample agent interactions

## Q: "Why did MoM growth flatline in month 7?"

```
1. get_metric("mom_growth", "monthly") -> sees the flatline
2. time_series_compare("new_signups", "month 7", "month 6")
   -> signups down 5% only
3. segment_breakdown("new_signups", "channel", "month 7")
   -> paid_search signups down 28%
4. query_database("SELECT month, cac_usd FROM cac_by_channel
                   WHERE channel='paid_search'")
   -> CAC on paid_search jumped from $42 to $61 in month 7
5. make_chart(line chart: CAC by channel over time)
6. summarize_findings(
     insights: ["MoM growth flatlined because paid-search CAC spiked
                from $42 to $61, dropping efficient acquisition 28%."],
     recommendations: ["Diversify to paid_social (CAC $34, room to scale)",
                       "Audit paid_search keywords for bid inflation",
                       "Double down on referrals while paid_search resets"])
```

## Q: "Show me retention by acquisition channel"

```
1. cohort_analysis(cohort_by="signup_month", segment_by="source", window=12)
2. make_chart(heatmap)
3. summarize_findings(insights, recommendations)
```

## Q: "What's our funnel from signup to paid?"

```
1. funnel_analysis(["signup", "activated", "first_feature_use",
                    "upgraded_to_paid"])
2. segment_breakdown("upgrade_rate", "source")
3. make_table(funnel by source)
4. summarize_findings(insights, recommendations)
```

# Phases (12-phase roadmap)

```
Phase 0    Setup            uv, Vercel project, Supabase, secrets, smoke test
Phase 1    Sample data      Python generator script + load into Supabase
                            (raw + rollup tables + planted patterns)
Phase 2    Schema tool      agent can introspect tables and columns
Phase 3    SQL tool         agent can run read-only queries
Phase 4    Single-tool agent loop   end-to-end CLI with one tool
Phase 5    Tier 2 metric tools      get_metric, cohort, funnel,
                                    time_series_compare, segment_breakdown
Phase 6    Output tools     make_chart, make_table, summarize_findings
Phase 7    Streaming + tool-call rendering   SSE backend, frontend renders
                                             tool-call steps progressively
Phase 8    Static dashboard         /api/dashboard endpoint, KPI tiles,
                                    MRR chart, cohort heatmap, events timeline
Phase 9    Chat UI integrated with dashboard on the same page
Phase 10   System prompt + agent personality + Tier 4 tools
Phase 11   Cost & safety hardening  rate limit, iteration cap (max 10 tool
                                    calls per question), prompt caching,
                                    structured logs, dev/prod namespace
Phase 12   Deploy + custom domain + AI Portfolio card + polish README
```

Eval is intentionally skipped unless I ask for it.

# Critical design decisions (locked)

1. Read-only Supabase role for the agent. Bad SQL can't write or drop.
2. Tool-call iteration cap of 10 per question. Prevents runaway costs.
3. Schema in system prompt plus get_schema() tool as fallback.
4. Streaming via SSE for tool calls AND final text tokens.
5. No agent frameworks. Anthropic SDK has tool use natively.
6. Plotly JSON specs, not images. Browser renders interactive charts.
7. In-memory conversation history per session. No persistence in v1.
8. Tier 2 metric tools are pre-built SQL templates / Python helpers, not
   "agent writes cohort SQL from scratch". This is the difference between
   an agent that has growth-analytics expertise versus a SQL generator.
9. Dashboard data is live from Supabase rollup tables on each page load.

# Style preferences

- No em dashes anywhere in code, comments, content, or docs. Use
  commas, periods, parentheses, or rephrase.
- Match my portfolio's visual style: white background, black text, muted
  text in #6b6b7b, accent #1a1a2e, Inter for body, Playfair Display for
  display. Same CSS variables I use on lucaslima.xyz.
- Concise output. No "great question!" preamble, no padding, no
  unnecessary recap of what I just said.
- Mirror project #1's repo layout and naming.

# What I want you to do now

1. Confirm you've read and understood this brief.
2. Walk me through Phase 0 (concepts first: what's getting set up and
   why, alternatives) before any code is written.
3. Don't move to Phase 1 until I explicitly ask.
4. When you do write code, follow the patterns from project #1: small,
   readable files; clear separation of library code (importable) vs
   entry scripts; structured logging; explicit metadata everywhere it
   matters.

For reference, the project #1 repo at
https://github.com/lucaslimaa2/lucas-personal-rag is open and you can
read its structure for any patterns to mirror.

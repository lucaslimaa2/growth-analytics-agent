"""compare_to_benchmarks: pin a metric against industry SaaS norms.

The agent calls this when the user asks "is X good?", "how does this compare?",
or wants context for a number. Returns a tier ("excellent", "typical", etc.),
a one-line verdict, and the full benchmark table for transparency.

Sources blended into the ranges below:
  - OpenView SaaS Benchmarks 2024
  - Bessemer State of the Cloud
  - KeyBanc SaaS Survey
  - SaaS Capital quarterly reports

These are rough working ranges for B2B SaaS; segment (SMB vs Enterprise) and
stage (early vs scaled) shift them. The verdict text flags caveats.

Run directly to test:
    uv run python -m tools.benchmarks gross_churn_rate 3.17
    uv run python -m tools.benchmarks ltv_cac 6.1
    uv run python -m tools.benchmarks mom_growth 5.84
"""

from __future__ import annotations

import sys
from typing import Any, TypedDict


class Tier(TypedDict):
    name: str
    range: tuple[float, float]
    label: str


class BenchmarkResult(TypedDict):
    metric: str
    value: float
    tier: str
    verdict: str
    tiers: list[dict[str, Any]]
    context: str
    error: str | None


# Each metric: ordered list of tiers from "best" to "worst" (or any directional
# ordering that makes sense for that metric). The first tier whose range
# contains the value wins. Ranges are [low, high] inclusive of low, exclusive
# of high.
BENCHMARKS: dict[str, dict[str, Any]] = {
    "gross_churn_rate": {
        "unit": "% per month",
        "context": (
            "Monthly gross MRR churn. SMB SaaS: 3-7% typical. Enterprise SaaS: "
            "0.5-2% typical. Healthy figures depend on segment."
        ),
        "tiers": [
            {"name": "excellent", "range": (0, 1), "label": "Enterprise-grade (<1%)"},
            {"name": "good", "range": (1, 3), "label": "Strong for any segment"},
            {"name": "typical", "range": (3, 7), "label": "Normal range for SMB SaaS"},
            {"name": "concerning", "range": (7, 15), "label": "Investigate cohort and onboarding"},
            {"name": "severe", "range": (15, 100), "label": "Product-market-fit risk"},
        ],
    },
    "net_churn_rate": {
        "unit": "% per month",
        "context": (
            "Net churn = (churn - expansion) / mrr_start. Negative means expansion "
            "outweighs churn (best-in-class). Public SaaS top quartile: -1 to -10%."
        ),
        "tiers": [
            {
                "name": "best_in_class",
                "range": (-100, -5),
                "label": "Net negative churn (expansion-led growth)",
            },
            {"name": "excellent", "range": (-5, 0), "label": "Net retention >100%"},
            {"name": "typical", "range": (0, 5), "label": "Normal SaaS range"},
            {"name": "concerning", "range": (5, 15), "label": "Net contraction; address retention"},
            {
                "name": "severe",
                "range": (15, 100),
                "label": "Customer base is shrinking in revenue terms",
            },
        ],
    },
    "mom_growth": {
        "unit": "% per month",
        "context": (
            "Month-over-month MRR growth. Hyper-growth (15%+) is early-stage rare. "
            "Post-PMF SaaS: 5-10% per month is strong. 2-5% is typical for $1M-10M ARR."
        ),
        "tiers": [
            {"name": "hyper_growth", "range": (15, 100), "label": "Hyper-growth pace"},
            {"name": "strong", "range": (5, 15), "label": "Strong SaaS growth"},
            {"name": "typical", "range": (2, 5), "label": "Typical post-PMF range"},
            {"name": "slowing", "range": (0, 2), "label": "Slowing; investigate"},
            {"name": "shrinking", "range": (-100, 0), "label": "Net contraction"},
        ],
    },
    "ltv_cac": {
        "unit": "ratio",
        "context": (
            "LTV / CAC. 3x is widely cited as the minimum viable; 5x is healthy. "
            ">10x can signal under-investment in growth (you could afford to scale "
            "acquisition harder). LTV calculation methodology varies widely."
        ),
        "tiers": [
            {
                "name": "under_investing",
                "range": (10, 1000),
                "label": "Possibly under-investing in growth",
            },
            {"name": "healthy", "range": (3, 10), "label": "Healthy SaaS range"},
            {"name": "ok", "range": (1, 3), "label": "Above payback but tight unit economics"},
            {
                "name": "unprofitable",
                "range": (0, 1),
                "label": "Spending more to acquire than you'll earn",
            },
        ],
    },
    "cac_payback_months": {
        "unit": "months",
        "context": (
            "Months to recover CAC from gross margin contribution. SaaS gold standard "
            "is <12 months. 12-18 months is typical. >24 months warrants attention."
        ),
        "tiers": [
            {"name": "excellent", "range": (0, 6), "label": "Excellent payback"},
            {"name": "good", "range": (6, 12), "label": "SaaS gold standard"},
            {"name": "typical", "range": (12, 18), "label": "Typical SaaS range"},
            {"name": "concerning", "range": (18, 36), "label": "Sales efficiency under pressure"},
            {"name": "severe", "range": (36, 999), "label": "Acquisition economics unsustainable"},
        ],
    },
    "arpu": {
        "unit": "USD per month",
        "context": (
            "ARPU varies enormously by segment. SMB SaaS: $30-200. Mid-market: "
            "$500-2000. Enterprise: $5000+. The number alone isn't 'good' or 'bad'."
        ),
        "tiers": [
            {"name": "smb_low", "range": (0, 50), "label": "Self-serve SMB pricing"},
            {"name": "smb_typical", "range": (50, 200), "label": "Typical SMB SaaS"},
            {"name": "mid_market", "range": (200, 2000), "label": "Mid-market range"},
            {"name": "enterprise", "range": (2000, 1_000_000), "label": "Enterprise pricing"},
        ],
    },
}


def compare_to_benchmarks(metric: str, value: float) -> BenchmarkResult:
    """Compare a metric value against an industry benchmark table."""
    if metric not in BENCHMARKS:
        return {
            "metric": metric,
            "value": value,
            "tier": "",
            "verdict": "",
            "tiers": [],
            "context": "",
            "error": f"no benchmark available for '{metric}'. Supported: {sorted(BENCHMARKS)}",
        }

    bench = BENCHMARKS[metric]
    tier_match: dict[str, Any] | None = None
    for t in bench["tiers"]:
        lo, hi = t["range"]
        if lo <= value < hi:
            tier_match = t
            break

    if tier_match is None:
        verdict = f"{value} {bench['unit']} is outside all defined ranges for {metric}."
        tier_name = "out_of_range"
    else:
        verdict = f"{value} {bench['unit']} — {tier_match['label']}. (tier: {tier_match['name']})"
        tier_name = tier_match["name"]

    return {
        "metric": metric,
        "value": value,
        "tier": tier_name,
        "verdict": verdict,
        "tiers": bench["tiers"],
        "context": bench["context"],
        "error": None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: tools.benchmarks METRIC VALUE")
        sys.exit(1)
    m = sys.argv[1]
    v = float(sys.argv[2])
    r = compare_to_benchmarks(m, v)
    if r["error"]:
        print(f"error: {r['error']}")
    else:
        print(r["verdict"])
        print()
        print(f"Context: {r['context']}")
        print()
        print("Tiers:")
        for t in r["tiers"]:
            print(f"  {t['name']:18s} {t['range'][0]:>6}-{t['range'][1]:<6}  {t['label']}")

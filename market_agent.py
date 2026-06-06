"""
market_agent.py — Market Trend Agent
=====================================
Analyzes jobs.db to surface trends, insights, and market intelligence
for Devon's TPM/PM job search.

Outputs:
  - Keyword frequency trends (what skills are in demand)
  - New vs removed jobs by company
  - Salary distribution and trends
  - Location / work-type breakdown
  - Weekly digest summary via LLM

Usage:
    python market_agent.py              # full analysis
    python market_agent.py --days 14    # look back 14 days
    python market_agent.py --save       # save report to digests/
"""

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("[!] pip install requests")
    sys.exit(1)

DB_PATH    = Path(__file__).parent / "jobs.db"
DIGEST_DIR = Path(__file__).parent / "digests"
DIGEST_DIR.mkdir(exist_ok=True)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL      = "llama3.1:8b"  # fast model for analysis

# Skills/keywords to track
TRACKED_KEYWORDS = [
    # Technical
    "python", "sql", "kubernetes", "terraform", "aws", "azure", "gcp",
    "ci/cd", "devops", "agile", "scrum", "jira", "confluence",
    "roadmap", "okr", "kpi", "data center", "networking", "cloud",
    "infrastructure", "platform", "api", "ml", "ai", "machine learning",
    # Soft/role
    "cross-functional", "stakeholder", "executive", "budget",
    "vendor", "compliance", "security", "npi", "hardware",
    "launch", "go-to-market", "gtm", "escalation", "incident",
]


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_jobs(days_back: int = 7) -> list[dict]:
    """Get recent jobs from DB."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT * FROM jobs WHERE found_date >= ? ORDER BY found_date DESC",
        (cutoff,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_all_jobs() -> list[dict]:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM jobs ORDER BY found_date DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_removed_jobs(days_back: int = 7) -> list[dict]:
    """Jobs marked as removed or with status='removed'."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT * FROM jobs WHERE status='removed' AND found_date >= ?",
        (cutoff,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Analysis functions ─────────────────────────────────────────────────────────

def _s(val) -> str:
    """Coerce None/non-string DB values to empty string for safe .lower() etc."""
    if val is None:
        return ""
    return str(val)


def analyze_keywords(jobs: list[dict]) -> dict:
    """Count keyword frequency in job descriptions and titles."""
    title_counts = Counter()
    desc_counts  = Counter()

    for job in jobs:
        title = _s(job.get('title'))
        desc  = _s(job.get('description'))
        text  = f"{title} {desc}".lower()
        for kw in TRACKED_KEYWORDS:
            if kw.lower() in text:
                if kw.lower() in title.lower():
                    title_counts[kw] += 1
                else:
                    desc_counts[kw] += 1

    return {
        "in_titles": title_counts.most_common(15),
        "in_descriptions": desc_counts.most_common(15),
        "total_jobs_analyzed": len(jobs),
    }


def analyze_companies(jobs: list[dict], days_back: int = 7) -> dict:
    """Jobs added per company in the period."""
    by_company = Counter(_s(j.get("company")) or "Unknown" for j in jobs)
    all_jobs   = get_all_jobs()
    total_by_company = Counter(_s(j.get("company")) or "Unknown" for j in all_jobs)

    return {
        "new_this_period": by_company.most_common(20),
        "total_in_db": total_by_company.most_common(20),
        "companies_with_new_jobs": len(by_company),
    }


def analyze_salaries(jobs: list[dict]) -> dict:
    """Salary distribution analysis."""
    salaries = [
        (j.get("salary_min") or 0, j.get("salary_max") or 0)
        for j in jobs
        if j.get("salary_min") or j.get("salary_max")
    ]

    if not salaries:
        return {"note": "No salary data in this period"}

    mins = [s[0] for s in salaries if s[0]]
    maxs = [s[1] for s in salaries if s[1]]
    all_vals = mins + maxs

    ranges = {
        "under_100k": sum(1 for v in maxs if v and v < 100000),
        "100k_130k":  sum(1 for v in maxs if v and 100000 <= v < 130000),
        "130k_160k":  sum(1 for v in maxs if v and 130000 <= v < 160000),
        "160k_200k":  sum(1 for v in maxs if v and 160000 <= v < 200000),
        "over_200k":  sum(1 for v in maxs if v and v >= 200000),
    }

    return {
        "jobs_with_salary": len(salaries),
        "avg_min": int(sum(mins)/len(mins)) if mins else 0,
        "avg_max": int(sum(maxs)/len(maxs)) if maxs else 0,
        "median_max": sorted(maxs)[len(maxs)//2] if maxs else 0,
        "ranges": ranges,
        "sample_postings": [j.get("salary_raw","") for j in jobs if j.get("salary_raw")][:5],
    }


def analyze_locations(jobs: list[dict]) -> dict:
    """Location and work-type breakdown."""
    work_types = Counter(_s(j.get("work_type")) or "Unknown" for j in jobs)
    locations  = Counter()

    for j in jobs:
        loc = _s(j.get("location")).lower()
        if "remote" in loc or _s(j.get("work_type")).lower() == "remote":
            locations["Remote"] += 1
        elif "boston" in loc or "cambridge" in loc or " ma" in loc or "massachusetts" in loc:
            locations["Boston / MA"] += 1
        elif "new york" in loc or " ny" in loc or "nyc" in loc:
            locations["New York"] += 1
        elif "san francisco" in loc or "bay area" in loc or " ca" in loc:
            locations["California"] += 1
        elif "seattle" in loc or "bellevue" in loc or " wa" in loc:
            locations["Seattle / WA"] += 1
        elif "austin" in loc or " tx" in loc:
            locations["Texas"] += 1
        elif loc:
            locations["Other"] += 1
        else:
            locations["Unknown"] += 1

    return {
        "work_type_breakdown": dict(work_types.most_common()),
        "location_breakdown":  dict(locations.most_common()),
        "total_jobs": len(jobs),
    }


def analyze_levels(jobs: list[dict]) -> dict:
    """Seniority level distribution."""
    levels = Counter(_s(j.get("level")) or "Unknown" for j in jobs)
    return dict(levels.most_common())


def analyze_trends_over_time() -> dict:
    """Daily job counts over the past 30 days."""
    con = sqlite3.connect(str(DB_PATH))
    rows = con.execute(
        "SELECT found_date, COUNT(*) as n FROM jobs "
        "WHERE found_date >= date('now', '-30 days') "
        "GROUP BY found_date ORDER BY found_date"
    ).fetchall()
    con.close()

    by_date = {r[0]: r[1] for r in rows}
    # Calculate 7-day rolling average
    dates = sorted(by_date.keys())
    rolling = {}
    for i, d in enumerate(dates):
        window = dates[max(0, i-6):i+1]
        rolling[d] = round(sum(by_date[w] for w in window) / len(window), 1)

    return {
        "daily_counts": by_date,
        "rolling_7day_avg": rolling,
        "total_30_days": sum(by_date.values()),
        "peak_day": max(by_date, key=by_date.get) if by_date else "N/A",
        "peak_count": max(by_date.values()) if by_date else 0,
    }


def analyze_sources(jobs: list[dict]) -> dict:
    """Which sources are producing the most jobs."""
    sources = Counter(_s(j.get("source")) or "unknown" for j in jobs)
    return dict(sources.most_common())


# ── LLM summary ───────────────────────────────────────────────────────────────

def generate_llm_summary(analysis: dict) -> str:
    """Ask the LLM to write a plain-English market summary."""
    try:
        prompt = f"""You are a career intelligence analyst for Devon O'Rourke, a Senior TPM in Boston MA
targeting $130k-$180k+ roles at top tech companies or remote.

Analyze this job market data from Devon's job scraper and write a concise 200-word market brief.
Focus on: what's hot right now, salary trends, where the jobs are, and any actionable advice.

DATA:
{json.dumps(analysis, indent=2)[:3000]}

Write a punchy market brief with:
1. One sentence market summary
2. Top 3 keyword/skill trends
3. Salary observation
4. Location/remote trend
5. One actionable recommendation for Devon

Keep it under 200 words, direct and practical:"""

        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 400},
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("message",{}).get("content","").strip()
    except Exception as e:
        return f"LLM summary unavailable: {e}"
    return "LLM summary unavailable"


# ── Report builder ─────────────────────────────────────────────────────────────

def run_market_analysis(days_back: int = 7, save: bool = False, silent: bool = False) -> dict:
    """Run full market analysis. Returns the report dict."""
    if not DB_PATH.exists():
        print("[!] jobs.db not found")
        return {}

    if not silent:
        print(f"\n  Market Trend Agent — analyzing last {days_back} days")
        print(f"  DB: {DB_PATH}\n")

    recent_jobs = get_jobs(days_back)
    all_jobs    = get_all_jobs()

    if not recent_jobs and not silent:
        print(f"  No jobs found in the last {days_back} days")
        print(f"  Total jobs in DB: {len(all_jobs)}")
        print("  Run python scraper.py first to populate the DB")
        return {}

    report = {
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "period_days":   days_back,
        "new_jobs":      len(recent_jobs),
        "total_in_db":   len(all_jobs),
        "keywords":      analyze_keywords(recent_jobs),
        "companies":     analyze_companies(recent_jobs, days_back),
        "salaries":      analyze_salaries(recent_jobs),
        "locations":     analyze_locations(recent_jobs),
        "levels":        analyze_levels(recent_jobs),
        "sources":       analyze_sources(recent_jobs),
        "trends":        analyze_trends_over_time(),
    }

    if not silent:
        print(f"  Generating LLM market brief...")
    report["llm_summary"] = generate_llm_summary(report)

    if not silent:
        _print_report(report)

    if save:
        fname = DIGEST_DIR / f"market_trend_{datetime.now():%Y%m%d_%H%M}.json"
        fname.write_text(json.dumps(report, indent=2))
        txt_fname = fname.with_suffix(".txt")
        txt_fname.write_text(_format_text_report(report))
        if not silent:
            print(f"\n  Saved: {fname.name}")

    return report


def _print_report(r: dict):
    """Print report to console."""
    print(f"\n{'═'*55}")
    print(f"  MARKET BRIEF — {r['generated_at']}")
    print(f"{'═'*55}")
    print(f"  New jobs (last {r['period_days']}d): {r['new_jobs']}  |  Total in DB: {r['total_in_db']}")

    print(f"\n  TOP KEYWORDS (in titles):")
    for kw, n in r["keywords"]["in_titles"][:8]:
        bar = "█" * min(n, 20)
        print(f"    {kw:<20} {bar} {n}")

    sal = r["salaries"]
    if "avg_max" in sal:
        print(f"\n  SALARY:")
        print(f"    Jobs with salary data: {sal['jobs_with_salary']}")
        print(f"    Avg max: ${sal['avg_max']:,}  |  Median max: ${sal['median_max']:,}")
        for bucket, count in sal.get("ranges",{}).items():
            if count:
                print(f"    {bucket}: {count} jobs")

    print(f"\n  LOCATION BREAKDOWN:")
    for loc, n in r["locations"]["location_breakdown"].items():
        print(f"    {loc:<20} {n}")

    print(f"\n  WORK TYPE:")
    for wt, n in r["locations"]["work_type_breakdown"].items():
        print(f"    {wt:<15} {n}")

    print(f"\n  BY COMPANY (new this period):")
    for co, n in r["companies"]["new_this_period"][:8]:
        print(f"    {co:<25} {n} new")

    print(f"\n  BY SOURCE:")
    for src, n in r["sources"].items():
        print(f"    {src:<30} {n}")

    trends = r["trends"]
    if trends.get("daily_counts"):
        print(f"\n  30-DAY TREND:")
        print(f"    Total:    {trends['total_30_days']} jobs")
        print(f"    Peak day: {trends['peak_day']} ({trends['peak_count']} jobs)")

    print(f"\n{'─'*55}")
    print(f"  LLM MARKET BRIEF:")
    print(f"{'─'*55}")
    for line in r["llm_summary"].split("\n"):
        print(f"  {line}")
    print(f"{'═'*55}\n")


def _format_text_report(r: dict) -> str:
    lines = [
        f"Market Trend Report — {r['generated_at']}",
        f"Period: last {r['period_days']} days",
        f"New jobs: {r['new_jobs']} | Total in DB: {r['total_in_db']}",
        "",
        "LLM SUMMARY:",
        r["llm_summary"],
        "",
        "TOP KEYWORDS:",
    ]
    for kw, n in r["keywords"]["in_titles"][:10]:
        lines.append(f"  {kw}: {n}")
    lines += ["", "SALARY:", json.dumps(r["salaries"], indent=2)]
    lines += ["", "LOCATIONS:", json.dumps(r["locations"], indent=2)]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    run_market_analysis(days_back=args.days, save=args.save)

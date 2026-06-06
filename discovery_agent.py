"""
discovery_agent.py — Company & Role Discovery Agent
=====================================================
Goes beyond static job board scraping to actively discover opportunities:

1. MA Tech Hub Research — companies near Bellingham/Westborough/Waltham/Boston
2. Adjacent Role Discovery — titles Devon's skills transfer to
3. Company Intelligence — funding, hiring surges, office expansions
4. Feeds discovered companies into custom_targets.py for automated scraping

Usage:
    python discovery_agent.py                    # full discovery run
    python discovery_agent.py --ma-companies     # MA company research only
    python discovery_agent.py --expand-roles     # role title expansion only
    python discovery_agent.py --market-signals   # market/funding signals only
    python discovery_agent.py --dry-run          # show what would be added

Requires: pip install ollama requests beautifulsoup4
"""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[!] pip install requests beautifulsoup4")
    sys.exit(1)

DB_PATH       = Path(__file__).parent / "jobs.db"
TARGETS_FILE  = Path(__file__).parent / "custom_targets.json"
DISCOVERY_LOG = Path(__file__).parent / "discovery_log.json"
OLLAMA_URL    = "http://localhost:11434/api/chat"
MODEL         = "llama3.1:8b"

# ── Devon's Geography ─────────────────────────────────────────────────────────

# Bellingham MA is ~35mi SW of Boston. Major tech corridors within commute:
MA_TECH_HUBS = {
    "Westborough":  {"distance_mi": 12, "corridor": "I-495 West"},
    "Marlborough":  {"distance_mi": 10, "corridor": "I-495 West"},
    "Framingham":   {"distance_mi": 15, "corridor": "Mass Pike / I-90"},
    "Natick":       {"distance_mi": 18, "corridor": "Mass Pike / I-90"},
    "Waltham":      {"distance_mi": 25, "corridor": "Route 128 / I-95"},
    "Burlington":   {"distance_mi": 35, "corridor": "Route 128 / I-95"},
    "Needham":      {"distance_mi": 20, "corridor": "Route 128 / I-95"},
    "Cambridge":    {"distance_mi": 35, "corridor": "Boston Metro"},
    "Boston":       {"distance_mi": 38, "corridor": "Boston Metro"},
    "Woburn":       {"distance_mi": 35, "corridor": "Route 128 / I-95"},
    "Chelmsford":   {"distance_mi": 30, "corridor": "I-495 North"},
    "Lowell":       {"distance_mi": 35, "corridor": "I-495 North"},
    "Andover":      {"distance_mi": 40, "corridor": "I-495 North / I-93"},
    "Worcester":    {"distance_mi": 25, "corridor": "Mass Pike / I-90"},
    "Hopkinton":    {"distance_mi": 10, "corridor": "I-495 West"},
    "Milford":      {"distance_mi": 5,  "corridor": "I-495 Local"},
    "Franklin":     {"distance_mi": 8,  "corridor": "I-495 South"},
}

# Known MA tech companies (seed list — agent will expand this)
KNOWN_MA_COMPANIES = [
    # I-495 Corridor (closest to Bellingham)
    {"name": "Bose", "hq": "Framingham", "sector": "Consumer Electronics / Audio"},
    {"name": "MathWorks", "hq": "Natick", "sector": "Engineering Software"},
    {"name": "Dell/EMC", "hq": "Hopkinton", "sector": "Storage / Cloud Infrastructure"},
    {"name": "Boston Scientific", "hq": "Marlborough", "sector": "Medical Devices"},
    {"name": "TJX Companies", "hq": "Framingham", "sector": "Retail Tech"},
    {"name": "Staples", "hq": "Framingham", "sector": "Retail / E-commerce"},
    {"name": "Waters Corporation", "hq": "Milford", "sector": "Scientific Instruments"},
    {"name": "SharkNinja", "hq": "Needham", "sector": "Consumer Products / IoT"},

    # Route 128 / I-95 Corridor
    {"name": "Raytheon", "hq": "Waltham", "sector": "Defense / Aerospace"},
    {"name": "Rapid7", "hq": "Burlington", "sector": "Cybersecurity"},
    {"name": "Nuance/Microsoft", "hq": "Burlington", "sector": "AI / Speech"},
    {"name": "Oracle", "hq": "Burlington", "sector": "Cloud / Enterprise"},
    {"name": "National Grid", "hq": "Waltham", "sector": "Energy / Utilities"},
    {"name": "Carbon Black/VMware", "hq": "Waltham", "sector": "Security / Virtualization"},
    {"name": "Rocket Software", "hq": "Waltham", "sector": "Enterprise Software"},
    {"name": "Constant Contact", "hq": "Waltham", "sector": "Marketing Tech"},

    # Boston / Cambridge
    {"name": "HubSpot", "hq": "Cambridge", "sector": "Marketing / CRM"},
    {"name": "Akamai", "hq": "Cambridge", "sector": "CDN / Edge Computing"},
    {"name": "Wayfair", "hq": "Boston", "sector": "E-commerce"},
    {"name": "Toast", "hq": "Boston", "sector": "Restaurant Tech / Fintech"},
    {"name": "DraftKings", "hq": "Boston", "sector": "Sports Tech / Gaming"},
    {"name": "Klaviyo", "hq": "Boston", "sector": "Marketing Automation"},
    {"name": "Snyk", "hq": "Boston", "sector": "Developer Security"},
    {"name": "DataRobot", "hq": "Boston", "sector": "AI / ML Platform"},
    {"name": "Recorded Future", "hq": "Boston", "sector": "Threat Intelligence"},
    {"name": "Imprivata", "hq": "Waltham", "sector": "Healthcare IT / Identity"},
    {"name": "Pegasystems", "hq": "Cambridge", "sector": "Process Automation"},
    {"name": "PTC", "hq": "Boston", "sector": "IoT / CAD / PLM"},
    {"name": "Vertex Pharmaceuticals", "hq": "Boston", "sector": "Biotech / IT"},
    {"name": "Moderna", "hq": "Cambridge", "sector": "Biotech / IT"},

    # I-495 North
    {"name": "Analog Devices", "hq": "Wilmington", "sector": "Semiconductors"},
    {"name": "Mercury Systems", "hq": "Andover", "sector": "Defense Electronics"},
    {"name": "Brooks Automation", "hq": "Chelmsford", "sector": "Semiconductor Equipment"},

    # Major remote-friendly with MA offices
    {"name": "Cisco", "hq": "Boxborough", "sector": "Networking / Telecom"},
    {"name": "IBM", "hq": "Cambridge", "sector": "Enterprise / Cloud / AI"},
    {"name": "Procter & Gamble", "hq": "Boston (office)", "sector": "Consumer Goods / Tech"},
]

# ── Expanded Role Titles ──────────────────────────────────────────────────────
# Devon's TPM background transfers to many adjacent roles

CORE_TITLES = [
    "technical program manager",
    "technical project manager",
    "senior tpm",
    "engineering program manager",
    "infrastructure program manager",
]

ADJACENT_TITLES = [
    # Program/Project management (broader)
    "program manager",
    "senior program manager",
    "principal program manager",
    "program director",

    # Product-adjacent (Devon has roadmap + delivery experience)
    "technical product manager",
    "product operations manager",
    "platform product manager",

    # Operations / Strategy (Devon has exec reporting + cross-functional)
    "business operations manager",
    "technical operations manager",
    "engineering operations",
    "chief of staff engineering",
    "chief of staff technology",

    # Infrastructure-specific (Devon's core domain)
    "infrastructure manager",
    "cloud program manager",
    "network operations manager",
    "telecom program manager",
    "platform engineering manager",
    "site reliability program manager",
    "devops program manager",

    # Release / Launch (Devon has pilot, UAT, staging experience)
    "release manager",
    "launch manager",
    "deployment manager",
    "release train engineer",

    # Transformation / Strategy
    "digital transformation manager",
    "technology transformation lead",
    "IT program manager",
    "systems integration manager",
]

ALL_TITLES = CORE_TITLES + ADJACENT_TITLES


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg, level="·"):
    icons = {"·": "·", "ok": "✓", "warn": "⚠", "fix": "⚡", "run": "▶", "err": "✗", "new": "+"}
    print(f"  [{datetime.now():%H:%M:%S}] {icons.get(level, '·')} {msg}")


def ask_llm(prompt: str, temperature: float = 0.3) -> str:
    """Quick LLM call via Ollama REST API."""
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": 1024},
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        log(f"LLM error: {e}", "err")
    return ""


def load_targets() -> dict:
    if TARGETS_FILE.exists():
        return json.loads(TARGETS_FILE.read_text())
    return {}


def save_targets(targets: dict):
    TARGETS_FILE.write_text(json.dumps(targets, indent=2))


def load_discovery_log() -> dict:
    if DISCOVERY_LOG.exists():
        return json.loads(DISCOVERY_LOG.read_text())
    return {"discoveries": [], "last_run": None}


def save_discovery_log(log_data: dict):
    DISCOVERY_LOG.write_text(json.dumps(log_data, indent=2))


def add_to_custom_targets(companies: list[dict], dry_run: bool = False) -> int:
    """Add discovered companies to custom_targets.json. Returns count added."""
    targets = load_targets()
    added = 0
    for co in companies:
        name = co.get("name", "").strip()
        if not name:
            continue
        key = name.lower().replace(" ", "_")
        if key in targets:
            continue  # already tracked

        if dry_run:
            log(f"[DRY RUN] Would add: {name} ({co.get('sector', '')})", "new")
            added += 1
            continue

        targets[key] = {
            "name":         name,
            "added":        datetime.now().strftime("%Y-%m-%d"),
            "career_url":   co.get("career_url"),
            "platform":     None,
            "platform_cfg": None,
            "last_scraped": None,
            "job_count":    0,
            "discovery":    {
                "source":   co.get("source", "discovery_agent"),
                "sector":   co.get("sector", ""),
                "hq":       co.get("hq", ""),
                "reason":   co.get("reason", ""),
            },
        }
        added += 1
        log(f"Added: {name} — {co.get('hq', '')} ({co.get('sector', '')})", "new")

    if not dry_run and added:
        save_targets(targets)
    return added


# ── Discovery: MA Companies ──────────────────────────────────────────────────

def discover_ma_companies(dry_run: bool = False) -> int:
    """
    Add known MA tech companies to custom targets.
    Then ask LLM to suggest more based on the tech corridors.
    """
    log("Discovering MA tech companies near Bellingham...", "run")

    # Step 1: Add known companies
    added = add_to_custom_targets(
        [{"name": c["name"], "hq": c["hq"], "sector": c["sector"],
          "source": "known_ma_companies", "reason": f"MA company in {c['hq']}"}
         for c in KNOWN_MA_COMPANIES],
        dry_run=dry_run
    )
    log(f"Known MA companies: {added} new additions", "ok")

    # Step 2: Ask LLM for more companies we might have missed
    log("Asking LLM for additional MA tech companies...", "run")
    known_names = [c["name"] for c in KNOWN_MA_COMPANIES]
    prompt = f"""You are a business intelligence analyst focused on the Massachusetts technology ecosystem.

I live in Bellingham, MA and am looking for tech companies within commuting distance (I-495 corridor, Route 128/I-95 corridor, and Boston metro).

I already track these companies:
{', '.join(known_names)}

List 15-20 MORE technology companies with significant offices in Massachusetts that I'm missing.
Focus on:
- Companies with 200+ employees in MA
- Companies that would hire Technical Program Managers or similar roles
- Companies across sectors: SaaS, fintech, biotech IT, defense, hardware, telecom, cloud
- Include the city/town where their MA office is located

Format each as: CompanyName | City | Sector
Return ONLY the list, one per line, no explanations."""

    response = ask_llm(prompt)
    if response:
        llm_companies = []
        known_lower = set(n.lower().split()[0] for n in known_names)  # match on first word
        for line in response.strip().split("\n"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                name = re.sub(r'^\d+[\.\)]\s*', '', parts[0]).strip()  # strip numbering
                # Strip parenthetical notes like "(now part of Microsoft, ...)"
                name = re.sub(r'\s*\(.*?\)\s*$', '', name).strip()
                # Strip "also has offices" type suffixes
                name = re.sub(r'\s*,?\s*also\s+has\s+.*$', '', name, flags=re.I).strip()
                if not name or len(name) < 3:
                    continue
                # Check against known names (fuzzy — match first word)
                first_word = name.lower().split()[0]
                if first_word in known_lower:
                    continue
                known_lower.add(first_word)
                llm_companies.append({
                    "name": name,
                    "hq": parts[1] if len(parts) > 1 else "MA",
                    "sector": parts[2] if len(parts) > 2 else "",
                    "source": "llm_discovery",
                    "reason": f"LLM-suggested MA company in {parts[1] if len(parts)>1 else 'MA'}",
                })

        if llm_companies:
            llm_added = add_to_custom_targets(llm_companies, dry_run=dry_run)
            log(f"LLM-discovered MA companies: {llm_added} new additions", "ok")
            added += llm_added

    return added


# ── Discovery: Role Title Expansion ──────────────────────────────────────────

def expand_role_titles() -> list[str]:
    """
    Return the full expanded list of role titles to search for.
    Optionally ask LLM for more suggestions based on Devon's profile.
    """
    log("Expanding role title search scope...", "run")

    # Ask LLM for even more adjacent titles
    prompt = """You are a career strategist. A candidate has this background:
- Technical Program Manager (TPM) at telecom/infrastructure companies
- E2E architecture design and delivery
- Infrastructure planning, lab testing, UAT, staging, pilot rollouts
- Executive reporting, cross-functional leadership
- Professional debugging across hardware and software

What are 10 additional job titles (beyond TPM/PM) this person should search for?
Think about roles in operations, strategy, release engineering, platform teams, and transformational leadership.

Return ONLY the job titles, one per line, lowercase, no numbering."""

    response = ask_llm(prompt)
    extra_titles = []
    if response:
        for line in response.strip().split("\n"):
            title = line.strip().lower().strip("-•* ")
            if title and len(title) > 5 and title not in ALL_TITLES:
                extra_titles.append(title)

    all_titles = ALL_TITLES + extra_titles[:10]
    log(f"Total search titles: {len(all_titles)} ({len(CORE_TITLES)} core + {len(ADJACENT_TITLES)} adjacent + {len(extra_titles[:10])} LLM)", "ok")

    return all_titles


# ── Discovery: Market Signals ────────────────────────────────────────────────

def analyze_market_signals(dry_run: bool = False) -> dict:
    """
    Ask LLM to identify companies with recent hiring signals:
    - Recent funding rounds
    - IPO prep
    - Major product launches
    - Office expansions in MA
    - Leadership changes that signal growth
    """
    log("Analyzing market signals for MA tech...", "run")

    prompt = """You are a tech industry analyst focused on the Massachusetts/Boston area job market in 2025.

Identify 10 companies that are likely ACTIVELY HIRING Technical Program Managers or similar roles right now, based on:
1. Recent funding rounds (Series B+ or major growth funding)
2. Product launches or platform expansions
3. Office expansions in the Boston/I-495/Route 128 corridor
4. Recent acquisitions that need integration program managers
5. Companies known for large engineering orgs that need TPMs

For each company, explain WHY they're likely hiring in 1 sentence.

Format: CompanyName | City | Signal
Return ONLY the list, one per line."""

    response = ask_llm(prompt, temperature=0.4)
    signals = []
    if response:
        for line in response.strip().split("\n"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                name = re.sub(r'^\d+[\.\)]\s*', '', parts[0]).strip()
                if name and len(name) > 2:
                    signals.append({
                        "name": name,
                        "hq": parts[1] if len(parts) > 1 else "",
                        "signal": parts[2] if len(parts) > 2 else "",
                        "source": "market_signals",
                        "sector": "",
                        "reason": parts[2] if len(parts) > 2 else "Market signal",
                    })

    if signals:
        added = add_to_custom_targets(signals, dry_run=dry_run)
        log(f"Market signal companies: {added} new additions", "ok")

    return {"signals": signals, "added": len(signals)}


# ── Location Scoring ─────────────────────────────────────────────────────────

def score_location_proximity(location: str) -> dict:
    """
    Score a job location based on proximity to Bellingham, MA.
    Returns {score: 0-10, commute: str, hub: str}.
    """
    if not location:
        return {"score": 5, "commute": "Unknown", "hub": ""}

    loc = location.lower()

    # Remote is always top tier
    if "remote" in loc:
        return {"score": 10, "commute": "Remote", "hub": "Remote"}

    # Check MA tech hubs
    for hub, info in MA_TECH_HUBS.items():
        if hub.lower() in loc:
            d = info["distance_mi"]
            if d <= 15:
                return {"score": 9, "commute": f"~{d}mi ({info['corridor']})", "hub": hub}
            elif d <= 25:
                return {"score": 8, "commute": f"~{d}mi ({info['corridor']})", "hub": hub}
            elif d <= 40:
                return {"score": 7, "commute": f"~{d}mi ({info['corridor']})", "hub": hub}
            else:
                return {"score": 6, "commute": f"~{d}mi ({info['corridor']})", "hub": hub}

    # General MA
    if any(kw in loc for kw in ["massachusetts", " ma,", ", ma ", "ma 0"]):
        return {"score": 7, "commute": "MA (distance unknown)", "hub": "MA"}

    # Hybrid in major metro
    if "hybrid" in loc:
        return {"score": 6, "commute": "Hybrid", "hub": ""}

    # Other US
    if any(s in loc for s in [", ny", "new york", ", ca", ", wa", ", tx"]):
        return {"score": 3, "commute": "Out of state", "hub": ""}

    return {"score": 4, "commute": "US (unknown)", "hub": ""}


# ── Main Discovery Run ───────────────────────────────────────────────────────

def run_discovery(
    ma_companies: bool = True,
    expand_roles: bool = True,
    market_signals: bool = True,
    dry_run: bool = False,
) -> dict:
    """Run full discovery. Returns summary dict."""
    print(f"\n{'═' * 55}")
    print(f"  Discovery Agent — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  Base: Bellingham, MA")
    print(f"  Dry run: {dry_run}")
    print(f"{'═' * 55}\n")

    results = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ma_companies_added": 0,
        "market_signals": [],
        "expanded_titles": [],
    }

    # 1. MA Company Research
    if ma_companies:
        results["ma_companies_added"] = discover_ma_companies(dry_run=dry_run)

    # 2. Role Title Expansion
    if expand_roles:
        results["expanded_titles"] = expand_role_titles()
        # Save expanded titles for use by scraper
        titles_path = Path(__file__).parent / "expanded_titles.json"
        if not dry_run:
            titles_path.write_text(json.dumps({
                "core": CORE_TITLES,
                "adjacent": ADJACENT_TITLES,
                "all": results["expanded_titles"],
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }, indent=2))
            log(f"Saved {len(results['expanded_titles'])} titles to expanded_titles.json", "ok")

    # 3. Market Signals
    if market_signals:
        sig = analyze_market_signals(dry_run=dry_run)
        results["market_signals"] = sig.get("signals", [])

    # Save discovery log
    if not dry_run:
        dl = load_discovery_log()
        dl["last_run"] = results["timestamp"]
        dl["discoveries"].append(results)
        # Keep last 30 runs
        dl["discoveries"] = dl["discoveries"][-30:]
        save_discovery_log(dl)

    # Summary
    print(f"\n{'═' * 55}")
    print(f"  Discovery Summary")
    print(f"{'═' * 55}")
    print(f"  MA companies added to targets: {results['ma_companies_added']}")
    print(f"  Expanded title count: {len(results.get('expanded_titles', []))}")
    print(f"  Market signals found: {len(results.get('market_signals', []))}")
    targets = load_targets()
    print(f"  Total custom targets now: {len(targets)}")
    print(f"{'═' * 55}\n")

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Company & Role Discovery Agent")
    parser.add_argument("--ma-companies",   action="store_true", help="MA company research only")
    parser.add_argument("--expand-roles",   action="store_true", help="Role title expansion only")
    parser.add_argument("--market-signals", action="store_true", help="Market signals only")
    parser.add_argument("--dry-run",        action="store_true", help="Show what would be added")
    parser.add_argument("--model",          type=str, default=MODEL, help="Ollama model")
    args = parser.parse_args()

    MODEL = args.model

    # Check Ollama
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
    except Exception:
        print("[!] Ollama not running — open the Ollama app first")
        sys.exit(1)

    # If specific flags given, run only those; otherwise run all
    specific = args.ma_companies or args.expand_roles or args.market_signals
    run_discovery(
        ma_companies=args.ma_companies if specific else True,
        expand_roles=args.expand_roles if specific else True,
        market_signals=args.market_signals if specific else True,
        dry_run=args.dry_run,
    )

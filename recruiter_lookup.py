"""
recruiter_lookup.py — Find recruiters & hiring managers per job posting
=======================================================================
Two-source approach:
  1. Hunter.io API   — domain search for HR/recruiting contacts (email + name)
  2. Google search   — "site:linkedin.com/in" queries for hiring managers

Results stored in `recruiters` table in jobs.db, linked by company name.
Run standalone or called from run_full_pipeline.py.

Usage:
    python recruiter_lookup.py              # enrich all un-enriched companies
    python recruiter_lookup.py --company "Klaviyo"
    python recruiter_lookup.py --limit 20
"""

import argparse
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

HUNTER_API_KEY = "7d165d04debf0fa32e34813160e4a715c87a65ae"
HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_FIND_URL   = "https://api.hunter.io/v2/email-finder"

DB_PATH = Path(__file__).parent / "jobs.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
}

# Roles we care about — recruiters and hiring managers
RECRUITER_ROLES = {
    "recruiter", "recruiting", "talent acquisition", "talent partner",
    "hr", "human resources", "people operations", "people partner",
    "hiring manager", "engineering manager", "director of engineering",
    "vp of engineering", "head of engineering", "program manager",
    "technical program", "tpm", "staffing",
}

# Known domain overrides for companies with non-obvious domains
DOMAIN_OVERRIDES = {
    "google": "google.com",
    "alphabet": "google.com",
    "amazon": "amazon.com",
    "aws": "amazon.com",
    "microsoft": "microsoft.com",
    "meta": "meta.com",
    "apple": "apple.com",
    "netflix": "netflix.com",
    "nvidia": "nvidia.com",
    "red hat": "redhat.com",
    "red hat, inc": "redhat.com",
    "servicenow": "servicenow.com",
    "salesforce": "salesforce.com",
    "hubspot": "hubspot.com",
    "klaviyo": "klaviyo.com",
    "mongodb": "mongodb.com",
    "datadog": "datadoghq.com",
    "splunk": "splunk.com",
    "pagerduty": "pagerduty.com",
    "crowdstrike": "crowdstrike.com",
    "cloudflare": "cloudflare.com",
    "cohere": "cohere.com",
    "wayfair": "wayfair.com",
    "draftkings": "draftkings.com",
    "chewy": "chewy.com",
    "rapid7": "rapid7.com",
    "veracode": "veracode.com",
    "massmutual": "massmutual.com",
    "fidelity": "fidelity.com",
    "fidelity investments": "fidelity.com",
    "state street": "statestreet.com",
    "pwc": "pwc.com",
    "iron mountain": "ironmountain.com",
    "toast": "toasttab.com",
    "formlabs": "formlabs.com",
    "insulet": "insulet.com",
    "hologic": "hologic.com",
    "vertex pharmaceuticals": "vrtx.com",
    "sarepta therapeutics": "sarepta.com",
    "thermo fisher": "thermofisher.com",
    "analog devices": "analog.com",
    "boston scientific": "bostonscientific.com",
    "raytheon": "rtx.com",
    "ramp": "ramp.com",
    "affirm": "affirm.com",
    "zillow": "zillow.com",
    "whoop": "whoop.com",
    "sharkninja": "sharkninja.com",
    "dynatrace": "dynatrace.com",
    "nexthink": "nexthink.com",
}


# ── DB Setup ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS recruiters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            company       TEXT NOT NULL,
            domain        TEXT,
            name          TEXT,
            first_name    TEXT,
            last_name     TEXT,
            email         TEXT,
            email_status  TEXT,
            title         TEXT,
            linkedin_url  TEXT,
            source        TEXT,
            confidence    INTEGER,
            found_date    TEXT,
            UNIQUE(company, email)
        )
    """)
    # Add recruiter_found column to jobs if missing
    try:
        con.execute("ALTER TABLE jobs ADD COLUMN recruiter_found INTEGER DEFAULT 0")
    except Exception:
        pass
    con.commit()
    con.close()


# ── Domain Resolution ─────────────────────────────────────────────────────────

def company_to_domain(company: str) -> str | None:
    """Best-effort company name -> domain mapping."""
    key = company.lower().strip()

    # Check overrides first
    for override_key, domain in DOMAIN_OVERRIDES.items():
        if override_key in key:
            return domain

    # Strip common suffixes and guess domain
    clean = re.sub(
        r"\b(inc\.?|corp\.?|llc\.?|ltd\.?|co\.?|company|technologies|technology|"
        r"systems|solutions|software|services|group|global|international|"
        r"the\s+|&\s*\w+)\b",
        "", key, flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r"[^a-z0-9]", "", clean)

    if len(clean) >= 3:
        return f"{clean}.com"
    return None


# ── Hunter.io ─────────────────────────────────────────────────────────────────

def hunter_domain_search(domain: str, company: str) -> list[dict]:
    """Search Hunter.io for contacts at a domain, filtered to recruiting/HR roles."""
    try:
        r = requests.get(HUNTER_DOMAIN_URL, params={
            "domain": domain,
            "api_key": HUNTER_API_KEY,
            "limit": 10,
            "type": "personal",
        }, timeout=10)

        if r.status_code == 401:
            print(f"    [Hunter] Invalid API key")
            return []
        if r.status_code == 429:
            print(f"    [Hunter] Rate limited — pausing 60s")
            time.sleep(60)
            return []
        if r.status_code != 200:
            print(f"    [Hunter] {domain}: HTTP {r.status_code}")
            return []

        data = r.json().get("data", {})
        emails = data.get("emails", [])

        results = []
        for e in emails:
            title = (e.get("position") or "").lower()
            # Include if role matches recruiter/hiring manager keywords
            if not any(kw in title for kw in RECRUITER_ROLES):
                continue

            first = e.get("first_name", "") or ""
            last  = e.get("last_name", "")  or ""
            results.append({
                "company":      company,
                "domain":       domain,
                "name":         f"{first} {last}".strip(),
                "first_name":   first,
                "last_name":    last,
                "email":        e.get("value", ""),
                "email_status": e.get("verification", {}).get("status", "unknown") if isinstance(e.get("verification"), dict) else e.get("confidence_score", ""),
                "title":        e.get("position", ""),
                "linkedin_url": e.get("linkedin", ""),
                "source":       "hunter.io",
                "confidence":   e.get("confidence", 0),
            })

        return results

    except Exception as ex:
        print(f"    [Hunter] {domain}: {ex}")
        return []


# ── Google -> LinkedIn search ─────────────────────────────────────────────────

def google_linkedin_search(company: str, role_hint: str = "technical program manager") -> list[dict]:
    """
    Search Google for LinkedIn profiles of hiring managers at a company.
    Uses: site:linkedin.com/in "company" ("hiring" OR "recruiting" OR "TPM")
    """
    results = []
    try:
        query = (
            f'site:linkedin.com/in "{company}" '
            f'("hiring" OR "recruiter" OR "talent acquisition" OR "engineering manager")'
        )
        r = requests.get(
            "https://www.google.com/search",
            params={"q": query, "num": 5},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return []

        # Extract LinkedIn URLs and names from search results
        urls = re.findall(r'https://www\.linkedin\.com/in/[a-zA-Z0-9\-]+', r.text)
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            # Try to extract name from URL slug (best-effort)
            slug = url.split("/in/")[-1].rstrip("/")
            name_guess = slug.replace("-", " ").title()
            results.append({
                "company":      company,
                "domain":       None,
                "name":         name_guess,
                "first_name":   name_guess.split()[0] if name_guess else "",
                "last_name":    " ".join(name_guess.split()[1:]) if len(name_guess.split()) > 1 else "",
                "email":        "",
                "email_status": "unknown",
                "title":        "LinkedIn profile",
                "linkedin_url": url,
                "source":       "google_linkedin",
                "confidence":   30,
            })
            if len(results) >= 3:
                break

    except Exception as ex:
        print(f"    [Google/LinkedIn] {company}: {ex}")

    return results


# ── Save to DB ────────────────────────────────────────────────────────────────

def save_recruiters(con: sqlite3.Connection, contacts: list[dict]) -> int:
    saved = 0
    for c in contacts:
        try:
            con.execute("""
                INSERT OR IGNORE INTO recruiters
                (company, domain, name, first_name, last_name, email, email_status,
                 title, linkedin_url, source, confidence, found_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                c["company"], c.get("domain"), c["name"],
                c.get("first_name"), c.get("last_name"),
                c.get("email", ""), c.get("email_status", ""),
                c.get("title", ""), c.get("linkedin_url", ""),
                c["source"], c.get("confidence", 0),
                datetime.now().strftime("%Y-%m-%d"),
            ))
            if con.execute("SELECT changes()").fetchone()[0]:
                saved += 1
        except Exception:
            pass
    return saved


# ── Main Enrichment Loop ──────────────────────────────────────────────────────

def run(company_filter: str = None, limit: int = 25, verbose: bool = True):
    init_db()
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    # Get distinct companies not yet enriched (or filtered to one company)
    if company_filter:
        companies = [{"company": company_filter}]
    else:
        rows = con.execute("""
            SELECT DISTINCT company
            FROM jobs
            WHERE (salary_max IS NULL OR salary_max = 0 OR salary_max >= 170000)
              AND status = 'new'
              AND company NOT IN (SELECT DISTINCT company FROM recruiters)
              AND company != '' AND company != 'Unknown'
            ORDER BY score DESC NULLS LAST
            LIMIT ?
        """, (limit,)).fetchall()
        companies = [{"company": r["company"]} for r in rows]

    if verbose:
        print(f"\n  Recruiter lookup: {len(companies)} companies to enrich")

    total_saved = 0
    hunter_quota_hit = False

    for i, row in enumerate(companies):
        company = row["company"]
        domain  = company_to_domain(company)

        if verbose:
            print(f"  [{i+1}/{len(companies)}] {company}", end="")
            if domain:
                print(f" ({domain})", end="")
            print()

        contacts = []

        # Source 1: Hunter.io (if we have a domain and quota remains)
        if domain and not hunter_quota_hit:
            hunter_results = hunter_domain_search(domain, company)
            if hunter_results:
                contacts.extend(hunter_results)
                if verbose:
                    print(f"    Hunter: {len(hunter_results)} contacts")
            time.sleep(1)  # rate limit

        # Source 2: Google -> LinkedIn (always, as fallback/supplement)
        google_results = google_linkedin_search(company)
        if google_results:
            contacts.extend(google_results)
            if verbose:
                print(f"    Google/LinkedIn: {len(google_results)} profiles")
        time.sleep(2)  # be polite to Google

        # Save and mark job as enriched
        if contacts:
            saved = save_recruiters(con, contacts)
            total_saved += saved
            if verbose:
                print(f"    Saved {saved} new contacts")
        else:
            if verbose:
                print(f"    No contacts found")

        # Mark company's jobs as recruiter_found
        con.execute(
            "UPDATE jobs SET recruiter_found = 1 WHERE company = ?", (company,)
        )
        con.commit()

    con.close()
    if verbose:
        print(f"\n  Recruiter lookup complete: {total_saved} new contacts saved")
    return total_saved


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recruiter & hiring manager lookup")
    parser.add_argument("--company", help="Enrich a single company")
    parser.add_argument("--limit", type=int, default=25, help="Max companies to enrich")
    args = parser.parse_args()
    run(company_filter=args.company, limit=args.limit)

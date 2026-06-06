"""
TPM Job Scraper — Direct Company Career Pages + YC Jobs
Targets: Google, Amazon, Microsoft, Meta, Apple, NetApp, YC startups
Titles:  Technical Program Manager, Technical Project Manager, TPM, Senior TPM
Runs:    Daily at 8:00 AM — stores results in jobs.db (SQLite)

Install deps:
    pip install requests beautifulsoup4 schedule playwright
    playwright install chromium
"""

import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sqlite3
import time
import json
import re
import threading
import schedule
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright
from urllib.parse import quote_plus

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = "jobs.db"

# ── Load profile (single source of truth) ────────────────────────────────────
try:
    from profile import (
        TARGET_TITLES as SEARCH_TITLES,
        SEARCH_QUERIES,
        PREFERRED_LOCATIONS,
        SALARY_FLOOR,
    )
except ImportError:
    # Fallback if profile.py not present
    SEARCH_TITLES = ["technical program manager","technical project manager","tpm",
                     "senior tpm","product manager","business operations"]
    SEARCH_QUERIES = ["technical program manager","product manager","business operations manager"]
    PREFERRED_LOCATIONS = ["boston","ma","massachusetts","remote","hybrid"]
    SALARY_FLOOR = 130000

# ── Load expanded titles from discovery agent if available ────────────────────
try:
    import json as _json
    _et_path = Path(__file__).parent / "expanded_titles.json"
    if _et_path.exists():
        _et = _json.loads(_et_path.read_text())
        _expanded = _et.get("all", [])
        # Merge into SEARCH_TITLES (dedup, keep order)
        _seen = set(t.lower() for t in SEARCH_TITLES)
        for t in _expanded:
            if t.lower() not in _seen:
                SEARCH_TITLES.append(t)
                _seen.add(t.lower())
        # Also expand SEARCH_QUERIES with adjacent titles for API scrapers
        _qseen = set(q.lower() for q in SEARCH_QUERIES)
        _adjacent_queries = [
            "engineering program manager",
            "infrastructure program manager",
            "release manager",
            "technical operations manager",
            "platform program manager",
        ]
        for q in _adjacent_queries:
            if q.lower() not in _qseen:
                SEARCH_QUERIES.append(q)
                _qseen.add(q.lower())
except Exception:
    pass

# ── MA Proximity Scoring ──────────────────────────────────────────────────────
# Bellingham MA is the home base. Score locations by commute distance.

MA_PROXIMITY = {
    "milford": 5, "franklin": 8, "hopkinton": 10, "bellingham": 0,
    "marlborough": 10, "westborough": 12, "framingham": 15, "natick": 18,
    "needham": 20, "worcester": 25, "waltham": 25, "newton": 25,
    "burlington": 35, "cambridge": 35, "boston": 38, "woburn": 35,
    "chelmsford": 30, "lowell": 35, "andover": 40, "boxborough": 20,
    "wilmington": 35, "lexington": 30, "bedford": 32, "concord": 28,
}

def location_proximity_bonus(location: str) -> int:
    """
    Return a bonus score (0-3) based on proximity to Bellingham MA.
    Used by scout agent to boost nearby jobs.
    """
    if not location:
        return 0
    loc = location.lower()
    if "remote" in loc:
        return 3  # remote is always best
    for city, dist in MA_PROXIMITY.items():
        if city in loc:
            if dist <= 15:
                return 3  # easy commute
            elif dist <= 25:
                return 2  # reasonable commute
            elif dist <= 40:
                return 1  # long but doable
    if any(kw in loc for kw in ["massachusetts", " ma,", ", ma "]):
        return 1  # generic MA
    return 0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html",
}

# ── Location filter ───────────────────────────────────────────────────────────

INTERNATIONAL_SIGNALS = [
    "india", " uk", "london", "canada", "toronto", "australia",
    "sydney", "germany", "berlin", "france", "paris", "singapore",
    "japan", "tokyo", "china", "ireland", "dublin", "netherlands",
    "sweden", "poland", "brazil", "mexico", "bengaluru", "bangalore",
    "hyderabad", "pune", "mumbai", "chennai", "noida", "gurugram",
    "in, ka", "in, mh", "in, tn", "in, dl", "gb,", "au,",
]

def is_preferred_location(location: str) -> bool:
    """
    Return True if location matches Devon's preferences.
    Keeps: MA/Boston, Remote, Hybrid, blank (assume remote).
    Drops: international locations.
    """
    if not location or location.strip() == "":
        return True  # no location = keep (likely remote)
    loc = location.lower()
    # Drop international
    if any(sig in loc for sig in INTERNATIONAL_SIGNALS):
        return False
    # Keep preferred
    if any(pref in loc for pref in PREFERRED_LOCATIONS):
        return True
    # Keep any US location (may be remote or relocation-eligible)
    us_states = ["al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id",
                 "il","in","ia","ks","ky","la","me","md","mi","mn","ms","mo",
                 "mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok","or",
                 "pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi",
                 "wy","dc","us","usa","united states","america"]
    if any(f" {s}" in f" {loc} " or f", {s}" in loc for s in us_states):
        return True
    # Unknown — keep it, let scout agent score it
    return True

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            company        TEXT NOT NULL,
            title          TEXT NOT NULL,
            location       TEXT,
            work_type      TEXT,
            level          TEXT,
            team           TEXT,
            salary_min     INTEGER,
            salary_max     INTEGER,
            salary_raw     TEXT,
            posted_date    TEXT,
            description    TEXT,
            url            TEXT UNIQUE,
            source         TEXT,
            job_id         TEXT,
            found_date     TEXT,
            status         TEXT DEFAULT 'new',
            score          INTEGER,
            notes          TEXT
        )
    """)
    for col, typedef in [
        ("work_type",   "TEXT"),
        ("level",       "TEXT"),
        ("team",        "TEXT"),
        ("salary_min",  "INTEGER"),
        ("salary_max",  "INTEGER"),
        ("salary_raw",  "TEXT"),
        ("posted_date", "TEXT"),
        ("description", "TEXT"),
        ("score",       "INTEGER"),
    ]:
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    con.commit()
    con.close()


def _safe(v):
    """Convert any value to a SQLite-safe string/number/None."""
    if v is None: return None
    if isinstance(v, (int, float)): return v
    if isinstance(v, (dict, list)): return str(v)
    return str(v)


# Fields that should always be updated if we get a better value
UPDATABLE_FIELDS = [
    "location", "work_type", "level", "team",
    "salary_min", "salary_max", "salary_raw",
    "posted_date", "description", "title",
]


def save_jobs(jobs: list[dict]) -> tuple[int, int]:
    """
    Insert new jobs, update existing ones where fields are empty or stale.
    Returns (new_count, updated_count).
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    new_count = 0
    updated_count = 0

    for j in jobs:
        url = _safe(j.get("url", ""))
        if not url:
            continue

        # Drop international jobs — Devon wants MA/Remote only
        if not is_preferred_location(j.get("location", "")):
            continue

        # Check if job already exists
        existing = con.execute(
            "SELECT * FROM jobs WHERE url=?", (url,)
        ).fetchone()

        if existing is None:
            # Brand new job — insert everything
            try:
                con.execute(
                    "INSERT INTO jobs "
                    "(company, title, location, work_type, level, team, salary_min, salary_max, salary_raw, "
                    "posted_date, description, url, source, job_id, found_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _safe(j.get("company", "")),
                        _safe(j.get("title", "")),
                        _safe(j.get("location", "")),
                        _safe(j.get("work_type", "")),
                        _safe(j.get("level", "")),
                        _safe(j.get("team", "")),
                        _safe(j.get("salary_min")),
                        _safe(j.get("salary_max")),
                        _safe(j.get("salary_raw", "")),
                        _safe(j.get("posted_date", "")),
                        _safe(j.get("description", "")),
                        url,
                        _safe(j.get("source", "")),
                        _safe(j.get("job_id", "")),
                        datetime.now().strftime("%Y-%m-%d"),
                    ),
                )
                new_count += 1
            except sqlite3.IntegrityError:
                pass

        else:
            # Job exists — update any fields that are empty OR have new data
            updates = {}
            existing_dict = dict(existing)
            for field in UPDATABLE_FIELDS:
                new_val = _safe(j.get(field))
                old_val = existing_dict.get(field)
                # Update if: old was empty/null AND new has a real value
                if new_val and (old_val is None or old_val == "" or old_val == "None"):
                    updates[field] = new_val

            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                con.execute(
                    f"UPDATE jobs SET {set_clause} WHERE url=?",
                    list(updates.values()) + [url]
                )
                updated_count += 1

    con.commit()
    con.close()
    return new_count, updated_count


def is_tpm_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in SEARCH_TITLES)


def log(company: str, new: int, updated: int = 0):
    parts = [f"{new} new"]
    if updated:
        parts.append(f"{updated} updated")
    print(f"  [{datetime.now():%H:%M:%S}] {company:<12} → {', '.join(parts)} jobs")


def detect_work_type(text: str) -> str:
    """Detect remote/hybrid/onsite from job text or location string."""
    t = text.lower()
    if any(w in t for w in ["fully remote", "100% remote", "remote only", "work from anywhere", "work from home"]):
        return "Remote"
    if any(w in t for w in ["hybrid", "flex", "partially remote", "2 days", "3 days"]):
        return "Hybrid"
    if any(w in t for w in ["on-site", "onsite", "on site", "in office", "in-office"]):
        return "On-site"
    if "remote" in t:
        return "Remote"
    return ""


def parse_salary(text: str) -> tuple:
    """
    Handles all salary formats:
      148,700.00 - 201,200.00 USD   Amazon decimal
      $130k - $180k
      $130,000 - $180,000
      130000 - 175000
    """
    if not text:
        return None, None, ""
    # Strip thousand commas and decimal cents
    t = re.sub(r'(\d),(\d)', r'\1\2', str(text))
    t = re.sub(r'(\d+)\.(\d{2})(?!\d)', r'\1', t)

    patterns = [
        # bare 148700 - 201200  (Amazon after stripping)
        (r'(?<![\d.])(\d{5,6})\s*[-\u2013\u2014]+\s*(\d{5,6})(?![\d.])', 'full'),
        # $130k - $180k
        (r'\$?(\d+)[kK]\s*[-\u2013\u2014to]+\s*\$?(\d+)[kK]', 'k'),
        # $130000 - $180000
        (r'\$(\d{5,6})\s*[-\u2013\u2014to]+\s*\$(\d{5,6})', 'full'),
    ]
    for pat, mode in patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            try:
                lo = int(m.group(1)) * (1000 if mode == 'k' else 1)
                hi = int(m.group(2)) * (1000 if mode == 'k' else 1)
            except ValueError:
                continue
            if 30000 <= lo <= 800000 and 30000 <= hi <= 800000 and lo <= hi:
                return lo, hi, f"${lo//1000}k\u2013${hi//1000}k"

    m = re.search(r'\$(\d+)[kK]', t, re.IGNORECASE)
    if m:
        val = int(m.group(1)) * 1000
        if 30000 <= val <= 800000:
            return val, val, f"~${val//1000}k"
    return None, None, ""


def detect_level(title: str) -> str:
    """Extract seniority level from job title."""
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished", "fellow"]): return "Principal"
    if any(w in t for w in ["staff", "sr staff"]): return "Staff"
    if any(w in t for w in ["senior", "sr.", "sr ", "lead"]): return "Senior"
    if any(w in t for w in ["manager", "director", "head of", "vp"]): return "Manager+"
    if any(w in t for w in ["junior", "jr", "associate", "entry"]): return "Junior"
    return "Mid"


def detect_team(title: str, description: str) -> str:
    """Guess org/team from title and description."""
    combined = (title + " " + description).lower()
    if any(w in combined for w in ["aws", "amazon web services", "cloud infrastructure"]): return "AWS"
    if any(w in combined for w in ["azure", "microsoft cloud"]): return "Azure"
    if any(w in combined for w in ["ads", "advertising", "monetization"]): return "Ads"
    if any(w in combined for w in ["network", "networking", "telecom", "connectivity"]): return "Networking"
    if any(w in combined for w in ["security", "trust", "safety", "privacy"]): return "Security"
    if any(w in combined for w in ["hardware", "silicon", "chip", "device"]): return "Hardware"
    if any(w in combined for w in ["platform", "infrastructure", "devops", "sre"]): return "Platform/Infra"
    if any(w in combined for w in ["data", "analytics", "ml", "ai", "machine learning"]): return "Data/AI"
    if any(w in combined for w in ["product", "consumer", "growth"]): return "Product"
    return ""


def enrich_job(job: dict, description: str = "") -> dict:
    """Run all enrichment on a job dict — call before saving."""
    combined = f"{job.get('title','')} {job.get('location','')} {description}"
    if not job.get("work_type"):
        job["work_type"] = detect_work_type(combined)
    if not job.get("level"):
        job["level"] = detect_level(job.get("title", ""))
    if not job.get("team"):
        job["team"] = detect_team(job.get("title", ""), description)
    if not job.get("salary_min"):
        lo, hi, raw = parse_salary(description)
        job["salary_min"] = lo
        job["salary_max"] = hi
        job["salary_raw"] = raw
    if description and not job.get("description"):
        job["description"] = description[:2000]  # cap at 2k chars
    return job


# ── Google (via careers page — Playwright scrape) ────────────────────────────

def scrape_google() -> list[dict]:
    """
    Google careers scraper.

    Google embeds job data in a script tag as AF_initDataCallback({key:'ds:1', data:[[[job_id, title, url, ...], ...]]})
    Each job array: [job_id, title, apply_url, ?, ?, ?, [locations], [teams], [], ?, ?, ?, level_str]

    Job cards in DOM use:
      <div class="sMn82b">
        <h3 class="QJPWVe">Title</h3>
        <span class="r0wTof">Location</span>
      </div>
    Job links: href="jobs/results/{id}-{slug}?q=..."
    """
    import json as _json
    jobs  = []
    result = [jobs, None]

    def _scrape():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
                page = ctx.new_page()

                seen = set()
                all_captured = []

                google_queries = [
                    "technical program manager",
                    "technical project manager",
                    "product manager",
                    "tpm hardware devices",
                ]

                for query in google_queries:
                    try:
                        nav_url = (
                            "https://www.google.com/about/careers/applications/jobs/results/"
                            f"?q={quote_plus(query)}&sort_by=date"
                        )
                        page.goto(nav_url, timeout=20000, wait_until="networkidle")
                        page.wait_for_timeout(2000)

                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")

                        # PRIMARY: parse AF_initDataCallback script tag
                        # Structure: AF_initDataCallback({key: 'ds:1', data:[[[ job arrays ]]]})
                        for script in soup.find_all("script"):
                            script_text = script.string or ""
                            if "AF_initDataCallback" not in script_text or "ds:1" not in script_text:
                                continue
                            try:
                                # Extract the data array
                                m = re.search(r"AF_initDataCallback\(\{[^}]+data:\s*(\[\[.*)", script_text, re.DOTALL)
                                if not m:
                                    continue
                                raw = m.group(1)
                                # Find matching closing bracket
                                depth, end = 0, 0
                                for i, c in enumerate(raw):
                                    if c == "[": depth += 1
                                    elif c == "]":
                                        depth -= 1
                                        if depth == 0:
                                            end = i + 1
                                            break
                                if not end:
                                    continue
                                data = _json.loads(raw[:end])
                                # data is [[ [job_array], [job_array], ... ]]
                                job_list = data[0] if data else []
                                for job_arr in job_list:
                                    if not isinstance(job_arr, list) or len(job_arr) < 2:
                                        continue
                                    job_id = str(job_arr[0])
                                    title  = str(job_arr[1]) if len(job_arr) > 1 else ""
                                    if not title or not is_tpm_title(title):
                                        continue
                                    if job_id in seen:
                                        continue
                                    seen.add(job_id)
                                    # Locations at index 6, teams at index 7, level at index 12
                                    locs  = job_arr[6] if len(job_arr) > 6 and isinstance(job_arr[6], list) else []
                                    teams = job_arr[7] if len(job_arr) > 7 and isinstance(job_arr[7], list) else []
                                    level_str = str(job_arr[12]) if len(job_arr) > 12 else ""
                                    location = ", ".join(str(l) for l in locs) if locs else ""
                                    team     = str(teams[0]) if teams else ""
                                    slug     = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                                    job_url  = (
                                        f"https://www.google.com/about/careers/applications"
                                        f"/jobs/results/{job_id}-{slug}"
                                    )
                                    all_captured.append({
                                        "company":  "Google",
                                        "title":    title,
                                        "location": location,
                                        "team":     team,
                                        "level":    detect_level(title) or level_str.split("\n")[0].strip(),
                                        "url":      job_url,
                                        "source":   "careers.google.com",
                                        "job_id":   job_id,
                                    })
                            except Exception:
                                pass

                        # FALLBACK: parse job cards from rendered DOM
                        # <div class="sMn82b"> contains <h3 class="QJPWVe">Title</h3>
                        if not all_captured:
                            for card in soup.select("div.sMn82b"):
                                try:
                                    title_el = card.select_one("h3.QJPWVe, h3, h2")
                                    if not title_el:
                                        continue
                                    title = title_el.get_text(strip=True)
                                    if not is_tpm_title(title):
                                        continue
                                    # Get job link from parent
                                    link = card.find_parent("a") or card.select_one("a")
                                    href = link.get("href","") if link else ""
                                    if not href:
                                        # Search nearby
                                        parent = card.parent
                                        for _ in range(3):
                                            if parent is None: break
                                            a = parent.find("a", href=re.compile("jobs/results/"))
                                            if a:
                                                href = a.get("href","")
                                                break
                                            parent = parent.parent
                                    if not href:
                                        continue
                                    # Build full URL
                                    if href.startswith("jobs/results/"):
                                        href = f"https://www.google.com/about/careers/applications/{href}"
                                    elif href.startswith("/"):
                                        href = f"https://www.google.com{href}"
                                    # Extract job_id
                                    id_m = re.search(r"/results/(\d+)", href)
                                    job_id = id_m.group(1) if id_m else ""
                                    if job_id in seen:
                                        continue
                                    seen.add(job_id or href)
                                    loc_el = card.select_one("span.r0wTof")
                                    location = loc_el.get_text(strip=True) if loc_el else ""
                                    all_captured.append({
                                        "company":  "Google",
                                        "title":    title,
                                        "location": location,
                                        "url":      href,
                                        "source":   "careers.google.com",
                                        "job_id":   job_id,
                                        "level":    detect_level(title),
                                    })
                                except Exception:
                                    pass

                    except Exception as e:
                        print(f"  [Google] Error on '{query}': {e}")
                    time.sleep(1)

                browser.close()
                result[0].extend(all_captured)

        except Exception as e:
            result[1] = e

    t = threading.Thread(target=_scrape, daemon=True)
    t.start()
    t.join(timeout=90)
    if t.is_alive():
        print("  [Google] Timed out after 90s")
        return []
    if result[1]:
        print(f"  [Google] Error: {result[1]}")
    return result[0]


# ── Amazon (uses Amazon Jobs JSON API) ────────────────────────────────────────

def fetch_amazon_page_salary(job_url: str) -> tuple:
    """
    Fetch an individual Amazon job page and extract the salary.
    Amazon only puts salary on the job detail page, not in the search API.
    Format on page: $148,700/year - $201,200/year  or  148,700 - 201,200
    """
    try:
        r = requests.get(job_url, headers=HEADERS, timeout=6)
        soup = BeautifulSoup(r.text, "html.parser")

        # Try structured salary elements first
        for selector in [
            "[data-test='comp-and-benefits']",
            ".compensation-info",
            "[class*='salary']",
            "[class*='compensation']",
            "[class*='pay-range']",
        ]:
            el = soup.select_one(selector)
            if el:
                sal = parse_salary(el.get_text(" ", strip=True))
                if sal[2]:
                    return sal

        # Scan full page text for salary patterns
        # Amazon format: "$148,700/year - $201,200/year" or bare numbers
        full_text = soup.get_text(" ", strip=True)
        # Look near keywords
        for kw in ["salary", "pay range", "compensation", "annual", "USD"]:
            idx = full_text.lower().find(kw)
            if idx > 0:
                snippet = full_text[max(0, idx-50):idx+120]
                sal = parse_salary(snippet)
                if sal[2]:
                    return sal

        # Last resort — scan whole page
        return parse_salary(full_text[:5000])
    except Exception:
        return None, None, ""


def scrape_amazon() -> list[dict]:
    jobs = []
    search_url = "https://www.amazon.jobs/en/search.json"
    try:
        raw_jobs = []
        seen_ids = set()
        for query in SEARCH_QUERIES:
            params = {"base_query": query, "sort": "recent", "result_limit": 50, "offset": 0}
            r = requests.get(search_url, params=params, headers=HEADERS, timeout=15)
            for item in r.json().get("jobs", []):
                jid = str(item.get("id_icims",""))
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    raw_jobs.append(item)
            time.sleep(1)

        for item in raw_jobs:
            title = item.get("title", "")
            if not is_tpm_title(title):
                continue

            job_path   = item.get("job_path", "")
            location   = item.get("location", "")
            desc_short = item.get("description_short", "")
            desc_full  = str(item.get("description", "") or desc_short)
            schedule   = item.get("job_schedule_type", "")
            posted     = item.get("posted_date", item.get("updated_time", ""))
            job_url    = f"https://www.amazon.jobs{job_path}"

            # Normalize team field — Amazon returns dict, list, or string
            team_raw = item.get("team", item.get("category", ""))
            if isinstance(team_raw, dict):
                team_hint = team_raw.get("name", team_raw.get("label", ""))
            elif isinstance(team_raw, list):
                team_hint = str(team_raw[0]) if team_raw else ""
            else:
                team_hint = str(team_raw) if team_raw else ""

            # Try salary from API fields first (rarely populated)
            sal_min, sal_max, sal_raw = parse_salary(
                str(item.get("base_pay_range", "")) or
                str(item.get("base_pay", "")) or
                desc_full
            )

            # Note: page-level salary fetch skipped in main scrape loop
            # (too slow for bulk — runs via heal_missing_salary() at startup instead)

            job = {
                "company":     "Amazon",
                "title":       str(title),
                "location":    str(location),
                "posted_date": str(posted)[:10] if posted else "",
                "description": desc_full[:2000],
                "url":         job_url,
                "source":      "amazon.jobs",
                "job_id":      str(item.get("id_icims", "")),
                "team":        team_hint,
                "salary_min":  sal_min,
                "salary_max":  sal_max,
                "salary_raw":  sal_raw,
            }
            enrich_job(job, f"{desc_full} {schedule} {location}")
            jobs.append(job)

    except Exception as e:
        print(f"  [Amazon] Error: {e}")
    return jobs


# ── Microsoft (updated API endpoint) ─────────────────────────────────────────

def scrape_microsoft() -> list[dict]:
    """
    Microsoft careers via Playwright.
    The search page loads jobs via XHR to their internal API.
    Uses v2 careers URL which resolves correctly.
    Intercepts JSON responses and falls back to __NEXT_DATA__ parsing.
    """
    import json as _json
    jobs = []
    seen = set()
    result = [jobs, None]

    def _scrape():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
                page = ctx.new_page()
                captured = []

                def on_response(r):
                    try:
                        if r.status != 200: return
                        url = r.url
                        if "careers.microsoft.com" not in url and "microsoft.com" not in url:
                            return
                        ct = r.headers.get("content-type","")
                        if "json" not in ct: return
                        data = r.json()
                        # Try all known MS API response shapes
                        jobs_list = (
                            data.get("operationResult",{}).get("result",{}).get("jobs") or
                            data.get("jobs") or
                            data.get("value") or
                            []
                        )
                        if jobs_list:
                            captured.extend(jobs_list)
                    except Exception:
                        pass

                page.on("response", on_response)

                for query in SEARCH_QUERIES[:3]:
                    try:
                        url = (
                            "https://jobs.careers.microsoft.com/global/en/search"
                            f"?q={quote_plus(query)}&l=en_us&pg=1&pgSz=20&o=Relevance&flt=true"
                        )
                        page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)

                        # Also try __NEXT_DATA__
                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")
                        nd = soup.find("script", id="__NEXT_DATA__")
                        if nd and nd.string:
                            try:
                                data = _json.loads(nd.string)
                                props = data.get("props",{}).get("pageProps",{})
                                for key in ["jobs","searchResults","initialJobs","results"]:
                                    if key in props:
                                        captured.extend(props[key])
                                        break
                                # Deep search
                                state = props.get("initialState", props.get("dehydratedState",{}))
                                if isinstance(state, dict):
                                    for v in state.values():
                                        if isinstance(v, dict):
                                            jlist = v.get("jobs", v.get("results",[]))
                                            if jlist and isinstance(jlist, list):
                                                captured.extend(jlist)
                            except Exception:
                                pass

                        # HTML card fallback
                        for card in soup.select("[data-job-id], [class*='job-card'], li[class*='jobs']"):
                            try:
                                title_el = card.select_one("h2, h3, [class*='title']")
                                if not title_el: continue
                                title = title_el.get_text(strip=True)
                                if not is_tpm_title(title): continue
                                link = card.select_one("a")
                                href = link.get("href","") if link else ""
                                jid = card.get("data-job-id","") or href.split("/job/")[-1].split("?")[0]
                                if jid in seen: continue
                                seen.add(jid or title)
                                loc_el = card.select_one("[class*='location']")
                                captured.append({
                                    "title": title,
                                    "jobId": jid,
                                    "primaryLocation": loc_el.get_text(strip=True) if loc_el else "",
                                    "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}" if jid else href,
                                })
                            except Exception:
                                pass

                    except Exception as e:
                        print(f"  [Microsoft] {query}: {e}")
                    import time as _t; _t.sleep(1)

                browser.close()

                for item in captured:
                    title = item.get("title","")
                    if not is_tpm_title(title): continue
                    job_id = str(item.get("jobId", item.get("id","")))
                    if job_id in seen: continue
                    seen.add(job_id or title)
                    job_url = item.get("url","") or f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
                    result[0].append({
                        "company":  "Microsoft",
                        "title":    title,
                        "location": item.get("primaryLocation", item.get("location","")),
                        "url":      job_url,
                        "source":   "careers.microsoft.com",
                        "job_id":   job_id,
                        "level":    detect_level(title),
                    })

        except Exception as e:
            result[1] = e

    import threading as _th
    t = _th.Thread(target=_scrape, daemon=True)
    t.start(); t.join(timeout=90)
    if t.is_alive(): print("  [Microsoft] Timed out")
    if result[1]: print(f"  [Microsoft] Error: {result[1]}")
    return result[0]


# ── Meta (uses Meta Careers JSON API) ─────────────────────────────────────────

def scrape_meta() -> list[dict]:
    """
    Meta careers via Playwright.
    Debug confirmed: metacareers.com/graphql fires and
    job links exist as /profile/job_details/{ID}
    """
    import json as _json
    jobs = []
    seen = set()
    result = [jobs, None]

    def _scrape():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
                page = ctx.new_page()
                captured = []

                def on_response(r):
                    try:
                        if "metacareers.com/graphql" not in r.url: return
                        if r.status != 200: return
                        data = r.json()
                        edges = (data.get("data",{})
                                    .get("job_postings",{})
                                    .get("edges",[]))
                        if edges:
                            captured.extend(edges)
                    except Exception:
                        pass

                page.on("response", on_response)

                for query in SEARCH_QUERIES[:2]:
                    try:
                        url = (
                            "https://www.metacareers.com/jobs"
                            f"?q={quote_plus(query)}&sort_by_new=true"
                        )
                        page.goto(url, timeout=25000, wait_until="networkidle")
                        page.wait_for_timeout(4000)

                        # Also parse job links from DOM
                        # Debug confirmed: /profile/job_details/{ID} links
                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")
                        for link in soup.select("a[href*='/profile/job_details/']"):
                            try:
                                href = link.get("href","")
                                txt = link.get_text(" ", strip=True)
                                # Extract title and location from text
                                # Format: "Technical Program Manager, MLSunnyvale, CA +2"
                                parts = txt.split("\n") if "\n" in txt else [txt]
                                title = parts[0].strip()
                                if not is_tpm_title(title): continue
                                job_id = href.split("/job_details/")[-1].split("?")[0].split("/")[0]
                                if job_id in seen: continue
                                seen.add(job_id)
                                captured.append({
                                    "_dom": True,
                                    "id": job_id,
                                    "title": title,
                                    "url": f"https://www.metacareers.com{href}" if href.startswith("/") else href,
                                })
                            except Exception:
                                pass

                    except Exception as e:
                        print(f"  [Meta] {query}: {e}")
                    import time as _t; _t.sleep(1)

                browser.close()

                for item in captured:
                    if item.get("_dom"):
                        title = item.get("title","")
                        job_id = str(item.get("id",""))
                    else:
                        node = item.get("node", item)
                        title = node.get("title","")
                        job_id = str(node.get("id",""))
                    if not is_tpm_title(title): continue
                    if job_id in seen: continue
                    seen.add(job_id or title)
                    locs = item.get("node",item).get("locations",[]) if not item.get("_dom") else []
                    location = ", ".join(str(l) for l in locs) if locs else ""
                    job_url = item.get("url","") or f"https://www.metacareers.com/jobs/{job_id}"
                    result[0].append({
                        "company":  "Meta",
                        "title":    title,
                        "location": location,
                        "url":      job_url,
                        "source":   "metacareers.com",
                        "job_id":   job_id,
                        "level":    detect_level(title),
                    })

        except Exception as e:
            result[1] = e

    import threading as _th
    t = _th.Thread(target=_scrape, daemon=True)
    t.start(); t.join(timeout=60)
    if t.is_alive(): print("  [Meta] Timed out")
    if result[1]: print(f"  [Meta] Error: {result[1]}")
    return result[0]


# ── Apple (updated Jobs API) ──────────────────────────────────────────────────

def scrape_apple() -> list[dict]:
    """
    Apple Jobs via their search API.
    POST to /api/role/search with correct headers and Referer.
    Debug: jobs.apple.com loads but API returned 404 without Referer.
    """
    import json as _json
    jobs = []
    seen = set()

    for query in SEARCH_QUERIES[:3]:
        try:
            session = requests.Session()
            # Establish session/cookies first
            session.get(
                "https://jobs.apple.com/en-us/search",
                headers=HEADERS, timeout=10
            )
            r = session.post(
                "https://jobs.apple.com/api/role/search",
                json={
                    "query":   query,
                    "locale":  "en-us",
                    "page":    1,
                    "filters": {"range": {"standardWeeklyHours": {"start": None, "end": None}}},
                    "sort":    "relevance",
                },
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Referer": "https://jobs.apple.com/en-us/search",
                    "Origin":  "https://jobs.apple.com",
                },
                timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                for item in data.get("searchResults", []):
                    title = item.get("postingTitle","")
                    if not is_tpm_title(title): continue
                    job_id = str(item.get("positionId",""))
                    if job_id in seen: continue
                    seen.add(job_id or title)
                    locs = item.get("locations",[])
                    location = locs[0].get("name","") if locs and isinstance(locs[0],dict) else str(locs[0]) if locs else ""
                    jobs.append({
                        "company":  "Apple",
                        "title":    title,
                        "location": location,
                        "url":      f"https://jobs.apple.com/en-us/details/{job_id}",
                        "source":   "jobs.apple.com",
                        "job_id":   job_id,
                        "level":    detect_level(title),
                    })
        except Exception as e:
            print(f"  [Apple] {query}: {e}")
        time.sleep(1)
    return jobs


# ── NetApp (Workday — updated endpoint) ──────────────────────────────────────

def scrape_netapp() -> list[dict]:
    """
    NetApp careers via Workday API.
    Note: Workday was showing an outage page during last debug.
    If outage continues, returns empty list gracefully.
    """
    import json as _json
    jobs = []

    for query in SEARCH_QUERIES[:2]:
        try:
            # Establish session cookie first (Workday requires this)
            session = requests.Session()
            resp = session.get(
                "https://netapp.wd1.myworkdayjobs.com/NetAppCareers",
                headers=HEADERS, timeout=10
            )
            # Check for outage page
            if "community.workday.com" in resp.url or "outage" in resp.text.lower():
                print("  [NetApp] Workday outage page detected — skipping")
                return []

            r = session.post(
                "https://netapp.wd1.myworkdayjobs.com/wday/cxs/netapp/NetAppCareers/jobs",
                json={"appliedFacets":{},"limit":20,"offset":0,"searchText": query},
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Referer": "https://netapp.wd1.myworkdayjobs.com/NetAppCareers",
                },
                timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                for item in data.get("jobPostings",[]):
                    title = item.get("title","")
                    if not is_tpm_title(title): continue
                    path = item.get("externalPath","")
                    jobs.append({
                        "company":  "NetApp",
                        "title":    title,
                        "location": item.get("locationsText",""),
                        "url":      f"https://netapp.wd1.myworkdayjobs.com/NetAppCareers{path}",
                        "source":   "netapp.wd1.myworkdayjobs.com",
                        "job_id":   path,
                        "level":    detect_level(title),
                    })
        except Exception as e:
            print(f"  [NetApp] {query}: {e}")
        time.sleep(1)
    return jobs


# ── Y Combinator Jobs (via workatastartup.com API) ────────────────────────────

def scrape_yc() -> list[dict]:
    """
    YC / WorkAtAStartup via Playwright.
    Debug confirmed: 93 job links at /jobs/l/... and /jobs/{id}
    Jobs are rendered in the DOM, use direct link parsing.
    """
    import json as _json
    jobs = []
    seen = set()
    result = [jobs, None]

    def _scrape():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
                page = ctx.new_page()

                for query in SEARCH_QUERIES[:2]:
                    try:
                        url = (
                            "https://www.workatastartup.com/jobs"
                            f"?q={quote_plus(query)}&jobType=fulltime&remote=true"
                        )
                        page.goto(url, timeout=25000, wait_until="networkidle")
                        page.wait_for_timeout(3000)

                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")

                        # Debug: 93 links — job links are /jobs/{numeric_id}
                        # Filter for links that are actual job postings (numeric ID)
                        for link in soup.select("a[href^='/jobs/']"):
                            href = link.get("href","")
                            # Skip category links like /jobs/l/software-engineer
                            if "/jobs/l/" in href: continue
                            if not re.search(r"/jobs/\d+", href): continue

                            job_id = re.search(r"/jobs/(\d+)", href)
                            if not job_id: continue
                            jid = job_id.group(1)
                            if jid in seen: continue
                            seen.add(jid)

                            # Title is usually in an h2 inside the card
                            card = link.find_parent("div") or link.find_parent("li")
                            if card:
                                title_el = card.select_one("h2, h3, .job-title, [class*=title]")
                                title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
                            else:
                                title = link.get_text(strip=True)

                            if not title or not is_tpm_title(title): continue

                            # Company name
                            company_el = (card.select_one("[class*=company], [class*=startup], h3") if card else None)
                            company_name = f"YC: {company_el.get_text(strip=True)}" if company_el else "YC Startup"

                            # Location
                            loc_el = (card.select_one("[class*=location], [class*=remote]") if card else None)
                            location = loc_el.get_text(strip=True) if loc_el else "Remote"

                            result[0].append({
                                "company":  company_name,
                                "title":    title,
                                "location": location,
                                "url":      f"https://www.workatastartup.com{href}",
                                "source":   "workatastartup.com",
                                "job_id":   jid,
                                "level":    detect_level(title),
                            })

                    except Exception as e:
                        print(f"  [YC] {query}: {e}")
                    import time as _t; _t.sleep(1)

                browser.close()

        except Exception as e:
            result[1] = e

    import threading as _th
    t = _th.Thread(target=_scrape, daemon=True)
    t.start(); t.join(timeout=60)
    if t.is_alive(): print("  [YC] Timed out")
    if result[1]: print(f"  [YC] Error: {result[1]}")
    return result[0]


# ── Main scrape loop ──────────────────────────────────────────────────────────

def scrape_linkedin() -> list[dict]:
    """
    LinkedIn scraper — wraps linkedin_scraper.py's core functions.
    Uses saved session for auto-login. If session is missing or expired,
    opens a visible browser for manual login + 2FA.
    Returns list of job dicts compatible with save_jobs().
    """
    try:
        from linkedin_scraper import (
            load_creds, scrape_top_picks, scrape_query,
            _make_browser, _parse_card, is_tpm_title as li_is_tpm,
            SESSION_FILE as LI_SESSION, QUERIES as LI_QUERIES,
        )
    except ImportError:
        print("  [LinkedIn] linkedin_scraper.py not found — skipping")
        return []

    try:
        email, pw = load_creds()
    except SystemExit:
        print("  [LinkedIn] No credentials configured — skipping")
        return []

    all_jobs = []
    try:
        with sync_playwright() as p:
            browser, ctx = _make_browser(p)
            page = ctx.new_page()

            # Try saved session first
            logged_in = False
            import json as _json
            if LI_SESSION.exists():
                try:
                    state = _json.loads(LI_SESSION.read_text())
                    ctx.add_cookies(state.get("cookies", []))
                    page.goto("https://www.linkedin.com/feed/",
                              timeout=15000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    if any(x in page.url for x in ["feed","mynetwork","jobs","/in/"]):
                        logged_in = True
                        print("  [LinkedIn] Auto-login with saved session")
                except Exception:
                    pass

            if not logged_in:
                # Auto-submit credentials
                print("  [LinkedIn] Logging in...")
                page.goto("https://www.linkedin.com/login",
                          timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                try:
                    page.fill("#username", email)
                    page.wait_for_timeout(400)
                    page.fill("#password", pw)
                    page.wait_for_timeout(400)
                    page.click('[type="submit"]')
                    page.wait_for_timeout(5000)
                except Exception as e:
                    print(f"  [LinkedIn] Auto-submit failed: {e}")

                cur = page.url
                if any(x in cur for x in ["checkpoint","challenge","security"]):
                    print("  [LinkedIn] 2FA required — complete in browser, then press ENTER")
                    input("  >>> Press ENTER after completing verification: ")
                    page.wait_for_timeout(3000)
                    cur = page.url

                if any(x in cur for x in ["feed","mynetwork","jobs","/in/"]):
                    logged_in = True
                    LI_SESSION.write_text(_json.dumps(ctx.storage_state()))
                    print("  [LinkedIn] Logged in, session saved")
                else:
                    print(f"  [LinkedIn] Login failed (url: {cur[:60]})")

            if not logged_in:
                browser.close()
                return []

            # Scrape top picks (scrolls until no new cards)
            top = scrape_top_picks(page, limit=50)
            print(f"  [LinkedIn] Top picks: {len(top)} jobs")
            all_jobs.extend(top)
            time.sleep(2)

            # Scrape keyword searches (now paginates through multiple pages)
            for q in LI_QUERIES[:4]:
                jobs = scrape_query(page, q, limit=40)
                print(f"  [LinkedIn] '{q}': {len(jobs)} jobs")
                all_jobs.extend(jobs)
                time.sleep(2)

            browser.close()

    except Exception as e:
        print(f"  [LinkedIn] Error: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for j in all_jobs:
        jid = j.get("job_id", "")
        if jid and jid not in seen:
            seen.add(jid)
            unique.append(j)

    return unique


def scrape_themuse() -> list[dict]:
    """
    Wrapper to call The Muse public API scraper.
    Imported from scrape_themuse.py which can also be run standalone.
    """
    try:
        from scrape_themuse import scrape_themuse as _scrape
        return _scrape(max_pages=5)
    except ImportError:
        print("  [Muse] scrape_themuse.py not found in C:\\TPM\\ — skipping")
        return []
    except Exception as e:
        print(f"  [Muse] Error: {e}")
        return []


def run_scrape():
    print(f"\n{'─'*50}")
    print(f"  Scrape started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'─'*50}")

    # Import optional scrapers
    ma_fn = None
    try:
        from ma_scraper import run_ma_scrape
        ma_fn = run_ma_scrape
    except ImportError:
        pass

    custom_fn = None
    try:
        from custom_targets import scrape_all_custom
        custom_fn = scrape_all_custom
    except ImportError:
        pass

    scrapers = [
        ("Google",    scrape_google),     # Playwright — 95s timeout
        ("Amazon",    scrape_amazon),
        ("Microsoft", scrape_microsoft),
        ("Meta",      scrape_meta),
        ("Apple",     scrape_apple),
        ("NetApp",    scrape_netapp),
        ("YC",                scrape_yc),
        ("The Muse",         scrape_themuse),            # Free public API - cross-company
        ("LinkedIn",          scrape_linkedin),   # Playwright + auth — 120s timeout
    ]

    total_new = 0
    total_updated = 0

    def run_with_timeout(fn, name, timeout_sec=45):
        """Run a scraper with a hard timeout so one hang can't block the rest."""
        import threading
        result = [[], None]  # [jobs, error]
        def target():
            try:
                result[0] = fn()
            except Exception as e:
                result[1] = e
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            print(f"  [{name}] Timed out after {timeout_sec}s — skipping")
            return []
        if result[1]:
            print(f"  [{name}] Error: {result[1]}")
            return []
        return result[0]

    for name, fn in scrapers:
        # Playwright scrapers need more time
        if name == "Google":
            timeout = 95
        elif name == "LinkedIn":
            timeout = 240  # auth + scrolling + pagination across multiple queries
        else:
            timeout = 45
        jobs = run_with_timeout(fn, name, timeout_sec=timeout)
        try:
            new, updated = save_jobs(jobs)
            log(name, new, updated)
            total_new     += new
            total_updated += updated
        except Exception as e:
            print(f"  [{name}] Save error: {e}")
        time.sleep(2)

    # Run MA scraper (Indeed, Dice, BuiltInBoston, Google Jobs, Glassdoor)
    if ma_fn:
        try:
            ma_new = ma_fn(limit=20)
            total_new += ma_new
        except Exception as e:
            print(f"  [MA Scraper] Error: {e}")
    else:
        print("  [MA Scraper] ma_scraper.py not found — skipping")

    # Run custom company targets
    if custom_fn:
        try:
            custom_new = custom_fn()
            total_new += custom_new
        except Exception as e:
            print(f"  [Custom Targets] Error: {e}")

    print(f"{'─'*50}")
    print(f"  Done. {total_new} new | {total_updated} updated in {DB_PATH}")
    print(f"{'─'*50}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def heal_missing_salary():
    """
    One-time heal pass: for any Amazon jobs with no salary,
    fetch the job page directly and extract it.
    Runs automatically on startup if missing salaries exist.
    """
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, url, title FROM jobs WHERE company='Amazon' "
        "AND (salary_raw IS NULL OR salary_raw='')"
    ).fetchall()
    con.close()

    if not rows:
        return

    print(f"  Healing salary for {len(rows)} Amazon jobs...")
    healed = 0
    for row_id, url, title in rows:
        try:
            sal_min, sal_max, sal_raw = fetch_amazon_page_salary(url)
        except Exception:
            sal_min, sal_max, sal_raw = None, None, ""
        if sal_raw:
            con = sqlite3.connect(DB_PATH)
            con.execute(
                "UPDATE jobs SET salary_min=?, salary_max=?, salary_raw=? WHERE id=?",
                (sal_min, sal_max, sal_raw, row_id)
            )
            con.commit()
            con.close()
            healed += 1
            print(f"    {title[:50]} → {sal_raw}")
        time.sleep(0.5)
    print(f"  Salary healed for {healed}/{len(rows)} jobs\n")


# ── Liveness Check ───────────────────────────────────────────────────────────

# Sources whose URLs are aggregator-scraped and may go stale quickly
_AGGREGATOR_SOURCES = {
    "linkedin.com", "linkedin.com/top-picks",
    "builtinboston.com", "careers.google.com",
    "amazon.jobs", "glassdoor.com", "indeed.com", "dice.com",
}

# Signals in the final URL that indicate an expired posting
_EXPIRED_URL_SIGNALS = ["error=true", "job-not-found", "expired", "no-longer-available"]

# Signals in page content that indicate an expired posting
_EXPIRED_CONTENT_SIGNALS = [
    "job no longer available", "no longer open", "position has been filled",
    "this job has expired", "job has been filled", "posting has expired",
    "this position is no longer", "job listing is no longer",
]


def _is_url_expired_fast(url: str, session: requests.Session) -> bool | None:
    """
    Quick HEAD/GET check. Returns True=expired, False=live, None=uncertain.
    Avoids Playwright for the bulk of checks.
    """
    try:
        resp = session.head(url, allow_redirects=True, timeout=8,
                            headers={"User-Agent": "Mozilla/5.0"})
        final_url = resp.url.lower()

        if resp.status_code in (404, 410):
            return True
        if any(sig in final_url for sig in _EXPIRED_URL_SIGNALS):
            return True
        if resp.status_code == 200:
            # Greenhouse expired postings redirect to the board root with ?error=true
            if "greenhouse.io" in final_url and "error=true" in final_url:
                return True
            return False
        # 403, 429, 5xx → uncertain, don't mark expired
        return None
    except Exception:
        return None


def _is_url_expired_playwright(url: str) -> bool:
    """Full page render check for ambiguous cases (called sparingly)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            content = page.content().lower()
            final_url = page.url.lower()
            browser.close()
        if any(sig in final_url for sig in _EXPIRED_URL_SIGNALS):
            return True
        if any(sig in content for sig in _EXPIRED_CONTENT_SIGNALS):
            return True
        return False
    except Exception:
        return False  # network error → assume live, don't mark expired


def check_stale_jobs(
    days_old: int = 30,
    limit: int = 200,
    use_playwright_for_uncertain: bool = False,
    progress_fn=None,
) -> dict:
    """
    Check jobs sourced from aggregators that are older than `days_old`.
    Marks confirmed-expired jobs as status='expired' in the DB.

    Returns: {checked, expired, live, uncertain, errors}
    """
    def _emit(msg: str):
        print(msg)
        if progress_fn:
            progress_fn(msg)

    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT id, url, title, company, source, found_date
        FROM jobs
        WHERE status = 'new'
          AND found_date <= date('now', ? || ' days')
          AND (
              source IN ({})
              OR source LIKE '%linkedin%'
              OR source LIKE '%builtin%'
              OR source LIKE '%google%'
              OR source LIKE '%amazon%'
              OR source LIKE '%indeed%'
              OR source LIKE '%dice%'
              OR source LIKE '%glassdoor%'
          )
        ORDER BY found_date ASC
        LIMIT ?
    """.format(",".join("?" * len(_AGGREGATOR_SOURCES))),
        [f"-{days_old}"] + list(_AGGREGATOR_SOURCES) + [limit]
    ).fetchall()
    con.close()

    total = len(rows)
    _emit(f"  Liveness check: {total} aggregator jobs older than {days_old} days")

    stats = {"checked": total, "expired": 0, "live": 0, "uncertain": 0, "errors": 0}
    expired_ids = []

    session = requests.Session()
    for i, (job_id, url, title, company, source, found_date) in enumerate(rows):
        if not url:
            stats["errors"] += 1
            continue

        if i % 20 == 0:
            _emit(f"    [{i}/{total}] checking...")

        result = _is_url_expired_fast(url, session)

        if result is None and use_playwright_for_uncertain:
            result = _is_url_expired_playwright(url)
            if result is None:
                result = False  # still uncertain → leave as live

        if result is True:
            expired_ids.append(job_id)
            stats["expired"] += 1
        elif result is False:
            stats["live"] += 1
        else:
            stats["uncertain"] += 1

        time.sleep(0.15)  # gentle rate limit

    if expired_ids:
        con = sqlite3.connect(DB_PATH)
        con.executemany(
            "UPDATE jobs SET status='expired' WHERE id=?",
            [(jid,) for jid in expired_ids]
        )
        con.commit()
        con.close()

    _emit(
        f"  Done — {stats['expired']} expired, {stats['live']} live, "
        f"{stats['uncertain']} uncertain out of {stats['checked']} checked"
    )
    return stats


if __name__ == "__main__":
    init_db()
    print("TPM Job Scraper starting up...")
    print("Targets: Google, Amazon, Microsoft, Meta, Apple, NetApp, YC, LinkedIn")
    print("Also:    MA Scraper (Indeed, Dice, BuiltInBoston) + Custom Targets")
    print("Titles:  Technical Program Manager / Project Manager / TPM / Senior TPM")
    print(f"Schedule: daily at 08:00  |  DB: {DB_PATH}\n")

    # Heal any existing jobs missing salary
    heal_missing_salary()

    # Run scrape (inserts new + updates empty fields on existing)
    run_scrape()

    # Then schedule daily at 8am
    schedule.every().day.at("08:00").do(run_scrape)
    while True:
        schedule.run_pending()
        time.sleep(60)

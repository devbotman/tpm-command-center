"""
ma_scraper.py — Massachusetts & Remote Job Scraper
===================================================
Scrapes 5 reliable sources for TPM/PM/BizOps jobs in MA + Remote:
  1. Indeed.com          — broad coverage, no auth
  2. Dice.com            — tech-focused, great for TPM
  3. BuiltInBoston.com   — MA-specific tech jobs
  4. Google Jobs RSS     — aggregates many sources
  5. Glassdoor RSS       — broad coverage via RSS

All results are filtered to MA/Boston/Remote and checked against
the target company career pages if the company is in your list.

Usage:
    python ma_scraper.py              # run once
    python ma_scraper.py --limit 50   # max jobs per source
    python ma_scraper.py --dry-run    # print without saving
"""

import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent / "jobs.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SEARCH_QUERIES = [
    "technical program manager",
    "technical project manager",
    "product manager",
    "business operations manager",
    "engineering program manager",
    "TPM",
]

LOCATIONS = ["Massachusetts", "Boston MA", "Remote"]

# MA location keywords for filtering
MA_KEYWORDS = [
    "massachusetts", "boston", " ma,", ", ma ", "ma 0",
    "cambridge", "waltham", "newton", "framingham", "worcester",
    "lowell", "quincy", "somerville", "remote", "hybrid"
]

TARGET_COMPANIES = [
    "google", "amazon", "microsoft", "meta", "apple", "netapp",
    "wayfair", "hubspot", "biogen", "raytheon", "general electric",
    "liberty mutual", "state street", "fidelity", "putnam",
    "akamai", "toast", "rapid7", "brightcove", "drift",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_target_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in [
        "technical program manager", "technical project manager",
        "tpm", "product manager", "business operations",
        "engineering program manager", "program manager",
        "project manager", "it program", "it project",
        "platform program", "infrastructure program",
        "operations manager", "strategy and operations",
        "go-to-market", "gtm operations",
    ])


def is_ma_or_remote(location: str) -> bool:
    if not location:
        return True  # unknown — include and filter later
    loc = location.lower()
    return any(kw in loc for kw in MA_KEYWORDS)


def parse_salary(text: str) -> tuple:
    if not text:
        return None, None, ""
    t = re.sub(r'(\d),(\d)', r'\1\2', str(text))
    t = re.sub(r'(\d+)\.(\d{2})(?!\d)', r'\1', t)
    patterns = [
        (r'(?<![\d.])(\d{5,6})\s*[-\u2013\u2014]+\s*(\d{5,6})(?![\d.])', 'full'),
        (r'\$?(\d+)[kK]\s*[-\u2013\u2014to]+\s*\$?(\d+)[kK]', 'k'),
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


def detect_work_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["fully remote", "100% remote", "remote only", "work from home"]):
        return "Remote"
    if any(w in t for w in ["hybrid", "partially remote"]):
        return "Hybrid"
    if any(w in t for w in ["on-site", "onsite", "in office", "in-office"]):
        return "On-site"
    if "remote" in t:
        return "Remote"
    return ""


def detect_level(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished"]): return "Principal"
    if any(w in t for w in ["staff"]):                      return "Staff"
    if any(w in t for w in ["senior", "sr.", "sr ", "lead"]): return "Senior"
    if any(w in t for w in ["director", "head of", "vp"]): return "Director+"
    return "Mid"


def clean_company(name: str, fallback: str = "") -> str:
    """
    Sanitize company name — remove URLs, domains, and empty strings.
    If the name looks like a URL or is empty, return fallback.
    """
    if not name:
        return fallback
    n = name.strip()
    # Reject anything that looks like a domain/URL
    if any(c in n for c in ["http", "www.", ".com", ".io", ".co", ".org", ".net", "/"]):
        return fallback
    # Reject if it's just whitespace or very short
    if len(n) < 2:
        return fallback
    return n


def extract_company_from_url(url: str) -> str:
    """Extract a readable company name from a job URL as last resort."""
    import re
    if not url:
        return ""
    patterns = [
        r'/company/([^/?&#]+)',           # BuiltInBoston: /company/cai
        r'jobs\.lever\.co/([^/?]+)',    # Lever: jobs.lever.co/acmecorp
        r'([^.]+)\.greenhouse\.io',     # Greenhouse: acmecorp.greenhouse.io
        r'([^.]+)\.lever\.co',          # Lever subdomain
        r'([^.]+)\.workday\.com',       # Workday
        r'([^.]+)\.wd\d+\.myworkday',  # Workday alt
        r'careers\.([^.]+)\.com',       # careers.acme.com
    ]
    for pat in patterns:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            name = m.group(1).replace("-", " ").replace("_", " ").strip()
            # Skip generic words
            if name.lower() in ["jobs", "careers", "hiring", "work", "apply", "en"]:
                continue
            if len(name) > 1:
                return name.title()
    return ""


def save_job(job: dict, dry_run: bool = False) -> bool:
    """Save a single job. Returns True if new, False if already exists."""
    if dry_run:
        print(f"    [DRY RUN] {job['company']} — {job['title']} ({job.get('location','')})")
        return True
    try:
        con = sqlite3.connect(str(DB_PATH))
        # Check if URL exists
        existing = con.execute(
            "SELECT id, salary_raw, work_type, description FROM jobs WHERE url=?",
            (job.get("url",""),)
        ).fetchone()

        if existing is None:
            con.execute(
                "INSERT INTO jobs (company, title, location, work_type, level, salary_min, "
                "salary_max, salary_raw, posted_date, description, url, source, job_id, found_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.get("company",""), job.get("title",""),
                    job.get("location",""), job.get("work_type",""),
                    job.get("level",""), job.get("salary_min"),
                    job.get("salary_max"), job.get("salary_raw",""),
                    job.get("posted_date",""), job.get("description","")[:2000],
                    job.get("url",""), job.get("source",""),
                    job.get("job_id",""), datetime.now().strftime("%Y-%m-%d"),
                )
            )
            con.commit()
            con.close()
            return True
        else:
            # Update empty fields
            updates = {}
            row_id, old_sal, old_wt, old_desc = existing
            if not old_sal and job.get("salary_raw"):
                updates["salary_raw"]  = job["salary_raw"]
                updates["salary_min"]  = job.get("salary_min")
                updates["salary_max"]  = job.get("salary_max")
            if not old_wt and job.get("work_type"):
                updates["work_type"] = job["work_type"]
            if not old_desc and job.get("description"):
                updates["description"] = job["description"][:2000]
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                con.execute(f"UPDATE jobs SET {set_clause} WHERE id=?",
                            list(updates.values()) + [row_id])
                con.commit()
            con.close()
            return False
    except Exception as e:
        print(f"    DB error: {e}")
        return False


# ── Source 1: Indeed ──────────────────────────────────────────────────────────

def scrape_indeed(query: str, limit: int = 20) -> list[dict]:
    """Indeed HTML search — RSS is malformed, HTML is more reliable."""
    jobs = []
    try:
        url = (f"https://www.indeed.com/jobs?q={quote_plus(query)}"
               f"&l=Massachusetts&sort=date&radius=50&fromage=7")
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Indeed embeds job data in JSON inside script tags
        import json as _json
        for script in soup.find_all("script"):
            text = script.string or ""
            if "jobKeysWithInfo" in text or "mosaic-provider-jobcards" in text:
                m = re.search(r'"jobKeysWithInfo"\s*:\s*(\{[^}]+\})', text)
                if not m:
                    continue
                try:
                    data = _json.loads(m.group(1))
                    for jk, info in list(data.items())[:limit]:
                        title = info.get("title","")
                        if not is_target_title(title): continue
                        company = info.get("company","")
                        location = info.get("formattedLocation", info.get("location",""))
                        if not is_ma_or_remote(location): continue
                        sal = info.get("extractedSalary",{}) or {}
                        sal_min = sal.get("min")
                        sal_max = sal.get("max")
                        sal_raw = f"${int(sal_min)//1000}k–${int(sal_max)//1000}k" if sal_min and sal_max else ""
                        jobs.append({
                            "company": company, "title": title, "location": location,
                            "work_type": detect_work_type(f"{title} {location}"),
                            "level": detect_level(title),
                            "salary_min": sal_min, "salary_max": sal_max, "salary_raw": sal_raw,
                            "url": f"https://www.indeed.com/viewjob?jk={jk}",
                            "source": "indeed.com", "job_id": jk,
                        })
                except Exception:
                    pass

        # Fallback: parse job cards from HTML
        if not jobs:
            for card in soup.select(".job_seen_beacon, .tapItem, [data-jk]")[:limit]:
                try:
                    title_el = card.select_one(".jobTitle, h2.title, [data-testid='job-title']")
                    if not title_el: continue
                    title = title_el.get_text(strip=True)
                    if not is_target_title(title): continue
                    co_el = (card.select_one(".companyName") or
                             card.select_one("[data-testid='company-name']") or
                             card.select_one(".company"))
                    raw_company = co_el.get_text(strip=True) if co_el else ""
                    company = clean_company(raw_company) or "Unknown"
                    loc_el = card.select_one(".companyLocation, .location")
                    location = loc_el.get_text(strip=True) if loc_el else "Massachusetts"
                    if not is_ma_or_remote(location): continue
                    jk = card.get("data-jk","")
                    sal_el = card.select_one(".salary-snippet, .estimated-salary")
                    sal_min, sal_max, sal_raw = parse_salary(sal_el.get_text() if sal_el else "")
                    jobs.append({
                        "company": company, "title": title, "location": location,
                        "work_type": detect_work_type(f"{title} {location}"),
                        "level": detect_level(title),
                        "salary_min": sal_min, "salary_max": sal_max, "salary_raw": sal_raw,
                        "url": f"https://www.indeed.com/viewjob?jk={jk}" if jk else "",
                        "source": "indeed.com", "job_id": jk,
                    })
                except Exception:
                    pass
    except Exception as e:
        print(f"    [Indeed] {query}: {e}")
    return jobs


# ── Source 2: Dice ────────────────────────────────────────────────────────────

def scrape_dice(query: str, limit: int = 20) -> list[dict]:
    """Dice.com — scrape search results page directly."""
    jobs = []
    try:
        url = (f"https://www.dice.com/jobs?q={quote_plus(query)}"
               f"&location=Massachusetts%2C+United+States&radius=50"
               f"&radiusUnit=mi&page=1&pageSize={limit}&filters.postedDate=ONE_WEEK"
               f"&filters.employmentType=FULLTIME&language=en")
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Dice puts job data in a Next.js __NEXT_DATA__ script tag
        import json as _json
        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            try:
                data = _json.loads(script.string or "")
                results = (data.get("props",{}).get("pageProps",{})
                              .get("initialState",{}).get("jobs",{})
                              .get("searchResults",{}).get("hits",[]))
                for item in results[:limit]:
                    title = item.get("title","")
                    if not is_target_title(title): continue
                    location = item.get("location","")
                    if not is_ma_or_remote(location): continue
                    company = item.get("hiringCompany",{}).get("name","") if isinstance(item.get("hiringCompany"),dict) else ""
                    desc = item.get("descriptionFragment","")
                    sal = item.get("salary","") or ""
                    sal_min, sal_max, sal_raw = parse_salary(sal or desc)
                    job_id = item.get("id","")
                    jobs.append({
                        "company": company, "title": title, "location": location,
                        "work_type": "Remote" if item.get("isRemote") else detect_work_type(f"{title} {location}"),
                        "level": detect_level(title),
                        "salary_min": sal_min, "salary_max": sal_max, "salary_raw": sal_raw,
                        "posted_date": str(item.get("postedDate",""))[:10],
                        "description": desc[:2000],
                        "url": f"https://www.dice.com/job-detail/{job_id}",
                        "source": "dice.com", "job_id": job_id,
                    })
            except Exception:
                pass

        # Fallback: parse job cards
        if not jobs:
            for card in soup.select("dhi-search-card, .card-title-link, [data-cy='card']")[:limit]:
                try:
                    title_el = card.select_one("a.card-title-link, h5, .title")
                    if not title_el: continue
                    title = title_el.get_text(strip=True)
                    if not is_target_title(title): continue
                    loc_el = card.select_one(".location, [data-cy='location']")
                    location = loc_el.get_text(strip=True) if loc_el else ""
                    if not is_ma_or_remote(location): continue
                    co_el = (card.select_one("[data-cy='company']") or
                             card.select_one(".company-name") or
                             card.select_one("[class*='employer']"))
                    raw_company = co_el.get_text(strip=True) if co_el else ""
                    company = clean_company(raw_company) or extract_company_from_url(url_str) or "Unknown"
                    href = title_el.get("href","") if title_el.name == "a" else ""
                    url_str = f"https://www.dice.com{href}" if href.startswith("/") else href
                    jobs.append({
                        "company": company, "title": title, "location": location,
                        "work_type": detect_work_type(f"{title} {location}"),
                        "level": detect_level(title),
                        "url": url_str, "source": "dice.com", "job_id": href,
                    })
                except Exception:
                    pass
    except Exception as e:
        print(f"    [Dice] {query}: {e}")
    return jobs


# ── Source 3: BuiltInBoston ───────────────────────────────────────────────────

def parse_bib_salary(text: str) -> tuple:
    """
    Parse BuiltInBoston salary format: '164K-215K Annually' or '120K Annually'
    No $ sign, K suffix, space before Annually/Year.
    """
    if not text:
        return None, None, ""
    t = text.strip()
    # Match: 164K-215K  or  164k - 215k  or  $164K-$215K
    m = re.search(r"\$?(\d+)[kK]\s*[-–]+\s*\$?(\d+)[kK]", t)
    if m:
        lo = int(m.group(1)) * 1000
        hi = int(m.group(2)) * 1000
        if 30000 <= lo <= 800000 and lo <= hi:
            return lo, hi, f"${lo//1000}k–${hi//1000}k"
    # Single value: 164K
    m = re.search(r"\$?(\d+)[kK]", t)
    if m:
        val = int(m.group(1)) * 1000
        if 30000 <= val <= 800000:
            return val, val, f"~${val//1000}k"
    return None, None, ""


def scrape_builtinboston(query: str, limit: int = 20) -> list[dict]:
    """
    BuiltInBoston.com — updated selectors from live page inspection (2026-06).

    Title:   <a data-id="job-card-title" href="/job/...">Title</a>
    Company: walk up from title link until "Annually" text found in container,
             then find [data-id='company-title'] span
    Salary:  text node matching 'NNK-NNK Annually' anywhere in the card container
    """
    jobs = []
    try:
        url = f"https://www.builtinboston.com/jobs?search={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        seen_urls = set()

        # Use data-id='job-card-title' — the reliable title anchor
        job_links = soup.select("a[data-id='job-card-title']")

        for link in job_links[:limit * 3]:
            try:
                href = link.get("href", "")
                if not href or href in seen_urls:
                    continue
                job_url = f"https://www.builtinboston.com{href}" if href.startswith("/") else href

                title = link.get_text(strip=True)
                if not title or not is_target_title(title):
                    continue

                seen_urls.add(href)

                # Walk up until we find the card container that holds salary text
                card = link.parent
                for _ in range(10):
                    if card is None:
                        break
                    if "Annually" in card.get_text():
                        break
                    card = card.parent

                # Extract company
                company = "Unknown"
                if card:
                    co_el = card.select_one("[data-id='company-title'] span")
                    if co_el:
                        company = clean_company(co_el.get_text(strip=True)) or "Unknown"
                    if company == "Unknown":
                        co_link = card.select_one("a[href*='/company/']")
                        if co_link:
                            company = extract_company_from_url(co_link.get("href", "")) or "Unknown"

                # Extract salary — find text node matching NNK-NNK Annually
                salary_raw = ""
                sal_min = sal_max = None
                if card:
                    for text_node in card.find_all(string=lambda t: t and "Annually" in t):
                        t = text_node.strip()
                        if re.search(r"\d+[kK]", t):
                            sal_min, sal_max, salary_raw = parse_bib_salary(t)
                            if salary_raw:
                                break

                # Extract location
                location = "Boston, MA"
                if card:
                    card_text = card.get_text(" ", strip=True)
                    for kw in ["Remote", "Hybrid", "Boston", "Cambridge", "Waltham", "Burlington"]:
                        if kw in card_text:
                            location = kw + ", MA" if kw not in ("Remote", "Hybrid") else kw
                            break

                work_text = f"{title} {location} " + (card.get_text(" ", strip=True)[:300] if card else "")

                jobs.append({
                    "company":    company,
                    "title":      title,
                    "location":   location,
                    "work_type":  detect_work_type(work_text),
                    "level":      detect_level(title),
                    "salary_min": sal_min,
                    "salary_max": sal_max,
                    "salary_raw": salary_raw,
                    "url":        job_url,
                    "source":     "builtinboston.com",
                    "job_id":     href,
                })

                if len(jobs) >= limit:
                    break

            except Exception:
                continue

    except Exception as e:
        print(f"    [BuiltInBoston] {query}: {e}")
    return jobs


# ── Source 4: Google Jobs RSS ─────────────────────────────────────────────────

def scrape_google_jobs_rss(query: str, limit: int = 20) -> list[dict]:
    """Google Jobs via programmatic search — aggregates many sources."""
    jobs = []
    try:
        # Use Google's job search with structured location
        search_q = quote_plus(f"{query} jobs Massachusetts OR Remote site:jobs.lever.co OR site:greenhouse.io OR site:workday.com")
        url = f"https://www.google.com/search?q={search_q}&num=20"
        r = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        import json as _json
        # Try JSON-LD first
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") != "JobPosting": continue
                    title = item.get("title","")
                    if not is_target_title(title): continue
                    loc = item.get("jobLocation",{})
                    if isinstance(loc, list): loc = loc[0] if loc else {}
                    addr = loc.get("address",{})
                    location = f"{addr.get('addressLocality','')}, {addr.get('addressRegion','')}".strip(", ")
                    if not is_ma_or_remote(location): continue
                    desc = item.get("description","")
                    sal_min, sal_max, sal_raw = parse_salary(desc)
                    raw_co = item.get("hiringOrganization",{}).get("name","")
                    company = clean_company(raw_co) or extract_company_from_url(item.get("url","")) or "Unknown"
                    jobs.append({
                        "company": company, "title": title, "location": location,
                        "work_type": detect_work_type(f"{title} {desc} {location}"),
                        "level": detect_level(title),
                        "salary_min": sal_min, "salary_max": sal_max, "salary_raw": sal_raw,
                        "posted_date": item.get("datePosted","")[:10],
                        "description": BeautifulSoup(desc,"html.parser").get_text(" ",strip=True)[:2000],
                        "url": item.get("url", item.get("sameAs","")),
                        "source": "google jobs", "job_id": item.get("identifier",{}).get("value",""),
                    })
                    if len(jobs) >= limit: break
            except Exception:
                pass
    except Exception as e:
        print(f"    [Google Jobs] {query}: {e}")
    return jobs


# ── Source 5: Glassdoor RSS ───────────────────────────────────────────────────

def scrape_glassdoor(query: str, limit: int = 20) -> list[dict]:
    """Glassdoor via their public job search — no auth needed for basic results."""
    jobs = []
    try:
        url = (f"https://www.glassdoor.com/Job/jobs.htm"
               f"?suggestCount=0&suggestChosen=false&clickSource=searchBtn"
               f"&typedKeyword={quote_plus(query)}"
               f"&locT=S&locId=14&jobType=&fromAge=7&minSalary=0&includeNoSalaryJobs=true"
               f"&radius=50&cityId=-1&minRating=0.0&industryId=0&sgocId=0"
               f"&seniorityType=all&sc.keyword={quote_plus(query)}"
               f"&orderBy=date_desc")
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Glassdoor embeds job data in React props / JSON script tags
        import json, re as _re
        for script in soup.find_all("script"):
            text = script.string or ""
            if '"jobListings"' in text or '"JobListingOutput"' in text:
                m = _re.search(r'"jobListings"\s*:\s*(\[.*?\])', text, _re.DOTALL)
                if m:
                    try:
                        listings = json.loads(m.group(1))
                        for item in listings[:limit]:
                            title = item.get("jobTitleText","")
                            if not is_target_title(title): continue
                            location = item.get("locationName","")
                            if not is_ma_or_remote(location): continue
                            company = item.get("employerName","")
                            job_id  = str(item.get("jobListingId",""))
                            job_url = f"https://www.glassdoor.com/job-listing/j{job_id}.htm"
                            sal_min = item.get("payPeriodicityAnnualizedPayLow")
                            sal_max = item.get("payPeriodicityAnnualizedPayHigh")
                            sal_raw = f"${int(sal_min)//1000}k–${int(sal_max)//1000}k" if sal_min and sal_max else ""
                            jobs.append({
                                "company": company, "title": title, "location": location,
                                "work_type": detect_work_type(f"{title} {location}"),
                                "level": detect_level(title),
                                "salary_min": sal_min, "salary_max": sal_max, "salary_raw": sal_raw,
                                "posted_date": str(item.get("listingDateText",""))[:10],
                                "url": job_url, "source": "glassdoor.com", "job_id": job_id,
                            })
                    except Exception:
                        pass

        # Fallback: parse job cards
        if not jobs:
            for card in soup.select("li.react-job-listing, [data-test='jobListing']")[:limit]:
                try:
                    title_el = card.select_one("[data-test='job-title'], .job-title")
                    if not title_el: continue
                    title = title_el.get_text(strip=True)
                    if not is_target_title(title): continue
                    loc_el = card.select_one("[data-test='emp-location'], .location")
                    location = loc_el.get_text(strip=True) if loc_el else ""
                    if not is_ma_or_remote(location): continue
                    co_el = card.select_one("[data-test='employer-name'], .employer-name")
                    company = co_el.get_text(strip=True) if co_el else ""
                    link_el = card.select_one("a[href*='/job-listing/']")
                    href = link_el["href"] if link_el else ""
                    url = f"https://www.glassdoor.com{href}" if href.startswith("/") else href
                    jobs.append({
                        "company": company, "title": title, "location": location,
                        "work_type": detect_work_type(f"{title} {location}"),
                        "level": detect_level(title), "url": url,
                        "source": "glassdoor.com", "job_id": href,
                    })
                except Exception:
                    pass
    except Exception as e:
        print(f"    [Glassdoor] {query}: {e}")
    return jobs


# ── Cross-verify against company career page ──────────────────────────────────

def cross_verify_with_company(job: dict) -> dict:
    """
    If the job is at a target company, try to find and link to the
    official posting on their career page for the most accurate data.
    Returns job with updated url/salary if found.
    """
    company = job.get("company","").lower()
    if not any(tc in company for tc in TARGET_COMPANIES):
        return job  # not a target company, skip

    title = job.get("title","")
    # Map company name to their career page search URL
    career_search = {
        "amazon":    f"https://www.amazon.jobs/en/search.json?base_query={quote_plus(title)}&result_limit=5",
        "microsoft": f"https://jobs.careers.microsoft.com/global/en/search?q={quote_plus(title)}&pgSz=5",
        "google":    f"https://careers.google.com/api/v3/search/?q={quote_plus(title)}&page_size=5",
        "apple":     None,  # POST endpoint, skip
        "meta":      None,  # JS-rendered, skip
        "netapp":    None,  # Workday, skip
    }

    for key, search_url in career_search.items():
        if key not in company or not search_url:
            continue
        try:
            r = requests.get(search_url, headers=HEADERS, timeout=8)
            data = r.json()
            results = (data.get("jobs") or
                       data.get("operationResult",{}).get("result",{}).get("jobs",[]))
            for result in results[:3]:
                result_title = result.get("title","").lower()
                if title.lower()[:20] in result_title or result_title[:20] in title.lower():
                    # Found matching posting on company site
                    if key == "amazon":
                        path = result.get("job_path","")
                        job["url"] = f"https://www.amazon.jobs{path}"
                        job["source"] = f"{job['source']} → verified:amazon.jobs"
                    elif key == "microsoft":
                        jid = result.get("jobId","")
                        job["url"] = f"https://jobs.careers.microsoft.com/global/en/job/{jid}"
                        job["source"] = f"{job['source']} → verified:careers.microsoft.com"
                    elif key == "google":
                        jid = result.get("id","")
                        job["url"] = f"https://careers.google.com/jobs/results/{jid}"
                        job["source"] = f"{job['source']} → verified:careers.google.com"
                    break
        except Exception:
            pass
    return job


# ── Main ──────────────────────────────────────────────────────────────────────

def run_ma_scrape(limit: int = 20, dry_run: bool = False) -> int:
    """Run all MA scrapers and return total new jobs saved."""
    print(f"\n  MA Scraper starting — {datetime.now():%H:%M:%S}")
    print(f"  Sources: Indeed, Dice, BuiltInBoston, Google Jobs, Glassdoor")
    print(f"  Location: MA/Boston/Remote only\n")

    sources = [
        ("Indeed",         scrape_indeed),
        ("Dice",           scrape_dice),
        ("BuiltInBoston",  scrape_builtinboston),
        ("Google Jobs",    scrape_google_jobs_rss),
        ("Glassdoor",      scrape_glassdoor),
    ]

    total_new = 0
    for source_name, fn in sources:
        source_new = 0
        for query in SEARCH_QUERIES:
            try:
                jobs = fn(query, limit=limit)
                for job in jobs:
                    # Cross-verify target company jobs
                    job = cross_verify_with_company(job)
                    if save_job(job, dry_run=dry_run):
                        source_new += 1
                        total_new  += 1
                time.sleep(1)
            except Exception as e:
                print(f"    [{source_name}] Error on '{query}': {e}")

        print(f"  [{source_name:<14}] → {source_new} new jobs")

    print(f"\n  MA Scraper done — {total_new} new jobs added\n")
    return total_new





def fix_existing_company_names():
    """
    Fix jobs with empty/bad company names.
    For BuiltInBoston: scrapes the actual job page to get company + salary.
    For others: extracts company from URL patterns (Lever, Greenhouse etc).
    """
    import sqlite3 as _sq
    con = _sq.connect(str(DB_PATH))
    con.row_factory = _sq.Row
    rows = con.execute(
        "SELECT id, company, url, source, salary_raw FROM jobs WHERE "
        "company LIKE '%.com%' OR company LIKE '%.io%' OR "
        "company LIKE '%.org%' OR company LIKE 'http%' OR "
        "company = 'Unknown' OR company = '' OR company IS NULL"
    ).fetchall()

    if not rows:
        print("  No jobs need fixing — all company names look good!")
        return

    print(f"  Found {len(rows)} jobs needing company/salary fix...")
    fixed = 0

    for row in rows:
        row_id   = row["id"]
        url      = row["url"] or ""
        source   = row["source"] or ""
        sal_raw  = row["salary_raw"] or ""
        updates  = {}

        if "builtinboston" in source and url:
            # Scrape the job page directly
            try:
                rp = requests.get(url, headers=HEADERS, timeout=10)
                sp = BeautifulSoup(rp.text, "html.parser")

                # Company: <h2 data-id="company-title"><span>Name</span></h2>
                co_el = sp.select_one("[data-id='company-title'] span")
                if co_el:
                    name = clean_company(co_el.get_text(strip=True))
                    if name and name not in ("Unknown", ""):
                        updates["company"] = name
                        print(f"    id={row_id}: company = {name}")

                # Fallback: company link /company/slug
                if "company" not in updates:
                    co_link = sp.select_one("a[href*='/company/']")
                    if co_link:
                        href = co_link.get("href", "")
                        slug = href.split("/company/")[-1].strip("/").split("/")[0]
                        name = slug.replace("-", " ").replace("_", " ").title()
                        if name and len(name) > 1:
                            updates["company"] = name
                            print(f"    id={row_id}: company (slug) = {name}")

                # Salary: sibling of fa-sack-dollar icon
                if not sal_raw:
                    icon = sp.select_one(".fa-sack-dollar, [class*='sack-dollar']")
                    if icon:
                        parent = icon.find_parent()
                        if parent:
                            sal_el = parent.find_next_sibling()
                            if sal_el:
                                sal_min, sal_max, s_raw = parse_bib_salary(
                                    sal_el.get_text(strip=True)
                                )
                                if s_raw:
                                    updates["salary_raw"] = s_raw
                                    updates["salary_min"] = sal_min
                                    updates["salary_max"] = sal_max
                                    print(f"    id={row_id}: salary = {s_raw}")
                    # Also try direct span
                    if "salary_raw" not in updates:
                        for span in sp.select("span.font-barlow"):
                            t = span.get_text(strip=True)
                            import re as _re
                            if _re.search(r"\d+[kK]", t):
                                sal_min, sal_max, s_raw = parse_bib_salary(t)
                                if s_raw:
                                    updates["salary_raw"] = s_raw
                                    updates["salary_min"] = sal_min
                                    updates["salary_max"] = sal_max
                                    print(f"    id={row_id}: salary (span) = {s_raw}")
                                    break
                time.sleep(0.4)

            except Exception as e:
                print(f"    id={row_id}: page fetch failed — {e}")

        else:
            # Non-BuiltInBoston: extract from URL
            name = extract_company_from_url(url)
            if name and name not in ("Unknown", ""):
                updates["company"] = name

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            con.execute(
                f"UPDATE jobs SET {set_clause} WHERE id=?",
                list(updates.values()) + [row_id]
            )
            fixed += 1

    con.commit()
    con.close()
    print(f"  Done — fixed {fixed}/{len(rows)} jobs")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",    type=int,  default=20)
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--fix-names", action="store_true", help="Fix existing bad company names in DB")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("[!] jobs.db not found — run server.py or scraper.py first")
        import sys; sys.exit(1)

    if args.fix_names:
        fix_existing_company_names()
    else:
        # Auto-fix names on every run
        fix_existing_company_names()
        run_ma_scrape(limit=args.limit, dry_run=args.dry_run)

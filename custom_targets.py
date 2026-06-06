"""
custom_targets.py — Custom Company Targeting (v2)
===================================================
Lets Devon add any company to the scrape list.
Auto-detects ATS platform (Greenhouse, Lever, Workday, Oracle/Taleo, SmartRecruiters)
and uses the correct API/scraping strategy.

Usage:
    python custom_targets.py --add "Wayfair, HubSpot, Akamai, Nvidia, Oracle"
    python custom_targets.py --list
    python custom_targets.py --remove "HubSpot"
    python custom_targets.py --scrape
    python custom_targets.py --scrape --company "Nvidia"
    python custom_targets.py --detect "Nvidia"   # detect ATS platform only

Companies are stored in custom_targets.json and auto-scraped on each scraper run.
"""

import json
import re
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

try:
    import requests
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"[!] Missing: {e}")
    sys.exit(1)

TARGETS_FILE = Path(__file__).parent / "custom_targets.json"
DB_PATH      = Path(__file__).parent / "jobs.db"
OLLAMA_URL   = "http://localhost:11434/api/chat"
MODEL        = "deepseek-coder-v2:16b"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}

# Regex patterns for Workday CSRF token extraction (used by both scrape_workday and scrape_workday_playwright)
CSRF_PATTERNS = [
    r'name=["\']?calypso[_-]?csrf[_-]?token["\']?\s+content=["\']([^"\']+)["\']',
    r'wd-csrf-token["\s:=]+["\']([a-f0-9\-]{20,})["\']',
    r'"csrfToken"\s*:\s*"([^"]+)"',
    r'CALYPSO_CSRF_TOKEN["\s:=]+["\']([^"\']+)["\']',
]

# Title keywords — Staff TPM level and above
TPM_KEYWORDS = [
    # Core TPM
    "technical program manager", "technical project manager",
    "tpm", "sr tpm", "senior tpm", "staff tpm", "principal tpm",
    "distinguished tpm",
    # Staff+ PM variants
    "staff program manager", "principal program manager",
    "distinguished program manager", "senior program manager",
    "staff technical program", "principal technical program",
    "senior technical program",
    # Engineering program management
    "engineering program manager", "senior engineering program",
    "staff engineering program", "principal engineering program",
    # Delivery / platform / release
    "technical delivery manager", "delivery manager",
    "platform program manager", "release program manager",
    "senior release manager",
    # Senior operations / portfolio
    "senior operations manager", "technical operations manager",
    "engineering operations manager",
    "program operations", "portfolio manager",
    # Director-tier leadership
    "director of program", "director of technical program",
    "associate director of program", "associate director of technical",
    "associate director, technical", "associate director technical",
    "associate director",
    "head of program",
    # Product management (senior)
    "senior product manager", "staff product manager",
    "principal product manager", "group product manager",
    "technical product manager", "product lead",
]

# Signals that indicate a role is too junior for Staff TPM
# Note: "associate" is intentionally excluded here — "Associate Director" is senior.
# Junior associate roles are caught by "associate program manager", "associate pm", etc.
_JUNIOR_SIGNALS = [
    "junior", "jr.", "jr ", "entry level", "entry-level",
    "intern", "internship", "co-op", "coordinator",
    "early career", "new grad", "graduate program",
    "associate program manager", "associate pm", "associate project manager",
]

# Companies that use non-standard TPM title nomenclature.
# Keys are lowercase substrings matched against company_name.lower().
# Entries here BYPASS the standard TPM_KEYWORDS list for that company.
# Keep these Senior+ only — junior filter still applies.
TITLE_OVERRIDE_MAP = {
    "fidelity": [
        "vp technical program manager", "principal program manager",
        "senior program manager", "senior technical program",
        "senior product manager", "program director", "portfolio manager",
        "senior it program manager",
    ],
    "boston scientific": [
        "sr. program manager", "senior program manager", "principal program manager",
        "senior project manager", "program director", "r&d program manager",
        "senior product manager",
    ],
    "thermo fisher": [
        "it program manager", "digital program manager", "global program manager",
        "senior program manager", "principal program manager",
        "technology program manager",
    ],
    "state street": [
        "senior program manager", "vp program manager", "principal program manager",
        "senior product manager", "senior project manager",
    ],
    "waters": [
        "program manager", "senior program manager", "principal program manager",
        "strategic operations", "enterprise program",
    ],
    "liberty mutual": [
        "senior technical lead", "senior it program manager",
        "senior program manager", "principal program manager",
    ],
    "john hancock": [
        "senior program manager", "it program manager",
        "technology program manager", "principal program manager",
    ],
    "wellington": [
        "technology program manager", "senior program manager",
        "principal program manager",
    ],
    "travelers": [
        "senior technology project manager", "it program manager",
        "senior program manager", "technology program manager",
    ],
    "manulife": [
        "senior program manager", "it program manager",
        "technology program manager",
    ],
}

# ── ATS Platform Detection ────────────────────────────────────────────────────

# Known company → career URL mappings (saves discovery time)
KNOWN_CAREER_URLS = {
    "nvidia":    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "oracle":    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
    "wayfair":   "https://www.wayfair.com/careers/jobs",
    "hubspot":   "https://boards.greenhouse.io/hubspot",
    "akamai":    "https://jobs.akamai.com/en/sites/CX_1/jobs",
    "cloudflare":"https://boards.greenhouse.io/cloudflare",
    "stripe":    "https://stripe.com/jobs/search",
    "datadog":   "https://boards.greenhouse.io/datadoghq",
    "confluent": "https://careers.confluent.io/search/jobs",
    "mongodb":   "https://www.mongodb.com/careers",
    "elastic":   "https://jobs.elastic.co/jobs",
    "hashicorp": "https://www.hashicorp.com/careers",
    "pagerduty": "https://boards.greenhouse.io/pagerduty",
    "twilio":    "https://boards.greenhouse.io/twilio",
    "databricks":"https://www.databricks.com/company/careers/open-positions",
    "snowflake": "https://careers.snowflake.com/us/en/search-results",
    # MA targets — confirmed working URLs
    "waterscorporation":   "https://uscareers-waters.icims.com/jobs/intro",
    "waters":              "https://uscareers-waters.icims.com/jobs/intro",
    "moderna":             "https://modernatx.wd1.myworkdayjobs.com/M_tx",
    "massmutual":          "https://massmutual.wd5.myworkdayjobs.com/en-US/MassMutual_Careers",
    "fidelityinvestments": "https://jobs.fidelity.com/en/jobs/?keyword=technical+program+manager",
    "fidelity":            "https://jobs.fidelity.com/en/jobs/?keyword=technical+program+manager",
    "athenahealth":        "https://athenahealth.wd1.myworkdayjobs.com/External",
    "mercurysystems":      "https://mercurysystems.wd5.myworkdayjobs.com/External",
    "mercury":             "https://mercurysystems.wd5.myworkdayjobs.com/External",
    "cognex":              "https://cognex.wd1.myworkdayjobs.com/en-US/Cognex_Careers",
    "draftkings":          "https://boards.greenhouse.io/draftkings",
    "klaviyo":             "https://boards.greenhouse.io/klaviyo",
    "rapid7":              "https://boards.greenhouse.io/rapid7",
    "microsoft":           "https://careers.microsoft.com/v2/global/en/search.html",
    "rocketsoftware":      "https://jobs.lever.co/rocketsoftware",
    "racketsoftware":      "https://jobs.lever.co/rocketsoftware",
}

def detect_platform(url: str, html: str = "") -> dict:
    """
    Detect which ATS platform a career page uses.
    Returns {platform, api_url, api_method, api_body, selectors}.
    """
    url_lower = url.lower()

    # ── iCIMS ──
    if "icims.com" in url_lower:
        m = re.search(r'(https://[^/]+\.icims\.com)', url, re.IGNORECASE)
        base = m.group(1) if m else url.split("/jobs")[0]
        return {
            "platform": "icims",
            "intro_url": f"{base}/jobs/intro",
            "base_url": base,
            "needs_playwright": False,
        }

    # ── Ashby ──
    if "ashbyhq.com" in url_lower:
        m = re.search(r'ashbyhq\.com/([^/?]+)', url_lower)
        slug = m.group(1) if m else ""
        return {
            "platform": "ashby",
            "api_url": "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            "ashby_slug": slug,
            "url_template": f"https://jobs.ashbyhq.com/{slug}/{{id}}",
        }

    # ── Greenhouse ──
    if "greenhouse.io" in url_lower:
        m = re.search(r'greenhouse\.io/(\w+)', url_lower)
        board_id = m.group(1) if m else ""
        return {
            "platform": "greenhouse",
            "api_url": f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true",
            "api_method": "GET",
            "api_body": None,
            "job_path": "jobs[]",
            "title_key": "title",
            "id_key": "id",
            "location_key": "location.name",
            "url_template": f"https://boards.greenhouse.io/{board_id}/jobs/{{id}}",
            "board_id": board_id,
        }

    # ── Lever ──
    if "lever.co" in url_lower:
        m = re.search(r'lever\.co/(\w+)', url_lower)
        company_id = m.group(1) if m else ""
        return {
            "platform": "lever",
            "api_url": f"https://api.lever.co/v0/postings/{company_id}?mode=json",
            "api_method": "GET",
            "api_body": None,
            "job_path": "root[]",
            "title_key": "text",
            "id_key": "id",
            "location_key": "categories.location",
            "url_template": f"https://jobs.lever.co/{company_id}/{{id}}",
        }

    # ── Workday ──
    if "myworkdayjobs.com" in url_lower or "workday.com" in url_lower:
        # Extract org and site from URL pattern:
        # {org}.wd{n}.myworkdayjobs.com/{SiteName}
        # or {org}.wd{n}.myworkdayjobs.com/en-US/{SiteName}
        m = re.search(r'([\w-]+)\.wd(\d+)\.myworkdayjobs\.com', url)
        if m:
            org = m.group(1)
            base = re.search(r'(https://[\w.-]+\.myworkdayjobs\.com)', url).group(1)
            # Extract site name — skip locale segments like en-US, en, fr
            path_after = url.split('.myworkdayjobs.com/')[-1].strip('/')
            parts = [p for p in path_after.split('/') if p and not re.match(r'^[a-z]{2}(-[A-Z]{2})?$', p)]
            site = parts[0] if parts else "External"
            return {
                "platform": "workday",
                "api_url": f"{base}/wday/cxs/{org}/{site}/jobs",
                "api_method": "POST",
                "api_body": {"appliedFacets": {}, "limit": 20, "offset": 0,
                             "searchText": "technical program manager"},
                "session_url": f"{base}/{site}",
                "job_path": "jobPostings[]",
                "title_key": "title",
                "id_key": "externalPath",
                "location_key": "locationsText",
                "url_template": f"{base}/{site}{{id}}",
            }

    # ── Oracle HCM / Taleo ──
    if "oraclecloud.com" in url_lower or "taleo" in url_lower:
        # Oracle uses a REST API: /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        base = re.search(r'(https://[^/]+)', url).group(1)
        # Extract site ID from URL if present
        site_m = re.search(r'/sites/(\w+)', url)
        site_id = site_m.group(1) if site_m else "CX_1"
        return {
            "platform": "oracle",
            "api_url": f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            "api_method": "GET",
            "api_params": {
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations,flexFieldsFacet.values",
                "finder": f"findReqs;siteNumber={site_id};keyword=technical program manager;lastSelectedFacet=LOCATIONS",
                "limit": 25, "offset": 0,
            },
            "api_body": None,
            "job_path": "items[]",
            "title_key": "Title",
            "id_key": "Id",
            "location_key": "PrimaryLocation",
            "url_template": url.split("/requisitions")[0] + "/requisitions/{id}",
        }

    # ── SmartRecruiters ──
    if "smartrecruiters.com" in url_lower:
        m = re.search(r'smartrecruiters\.com/(\w+)', url_lower)
        company_id = m.group(1) if m else ""
        return {
            "platform": "smartrecruiters",
            "api_url": f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings",
            "api_method": "GET",
            "api_body": None,
            "api_params": {"q": "technical program manager", "limit": 20},
            "job_path": "content[]",
            "title_key": "name",
            "id_key": "id",
            "location_key": "location.city",
            "url_template": f"https://jobs.smartrecruiters.com/{company_id}/{{id}}",
        }

    # ── Check HTML for platform hints ──
    if html:
        if "greenhouse" in html.lower():
            return {"platform": "greenhouse_embedded", "needs_playwright": True}
        if "lever" in html.lower():
            return {"platform": "lever_embedded", "needs_playwright": True}

    # ── Generic / Unknown ──
    return {"platform": "generic", "needs_playwright": True}


# ── Target management ────────────────────────────────────────────────────────

def load_targets() -> dict:
    if TARGETS_FILE.exists():
        return json.loads(TARGETS_FILE.read_text())
    return {}


def save_targets(targets: dict):
    TARGETS_FILE.write_text(json.dumps(targets, indent=2))


def add_companies(names: list[str]):
    targets = load_targets()
    added = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        key = name.lower().replace(" ", "_")
        if key not in targets:
            targets[key] = {
                "name":         name,
                "added":        datetime.now().strftime("%Y-%m-%d"),
                "career_url":   None,
                "platform":     None,
                "platform_cfg": None,
                "last_scraped": None,
                "job_count":    0,
            }
            added.append(name)
    save_targets(targets)
    if added:
        print(f"  Added: {', '.join(added)}")
    else:
        print("  No new companies added (may already exist)")


def remove_company(name: str):
    targets = load_targets()
    key = name.lower().replace(" ", "_")
    if key in targets:
        del targets[key]
        save_targets(targets)
        print(f"  Removed: {name}")
    else:
        print(f"  Not found: {name}")


def list_targets():
    targets = load_targets()
    if not targets:
        print("  No custom targets yet. Add some with --add")
        return
    print(f"\n  Custom company targets ({len(targets)}):")
    print(f"  {'Company':<20} {'Platform':<18} {'Last scraped':<14} {'Jobs':<6} URL")
    print(f"  {'-' * 80}")
    for key, t in targets.items():
        platform = t.get("platform", "?") or "?"
        url_short = (t.get("career_url") or "not found")[:40]
        print(f"  {t['name']:<20} {platform:<18} {t.get('last_scraped', 'never'):<14} {t.get('job_count', 0):<6} {url_short}")


# ── Career URL discovery ─────────────────────────────────────────────────────

def discover_career_url(company_name: str) -> tuple[str, dict]:
    """
    Find a company's career page URL and detect the ATS platform.
    Returns (url, platform_config).
    """
    name_lower = company_name.lower().replace(" ", "")

    # Check known URLs first
    if name_lower in KNOWN_CAREER_URLS:
        url = KNOWN_CAREER_URLS[name_lower]
        print(f"    Known URL: {url}")
        platform_cfg = detect_platform(url)
        return url, platform_cfg

    # Domain guesses for ATS platforms
    domain_guesses = [
        company_name.lower().replace(" ", ""),
        company_name.lower().replace(" ", "-"),
        company_name.lower().replace(" ", "_"),
    ]

    # Try Greenhouse first (most common for tech companies)
    for domain in domain_guesses:
        for pattern in [
            f"https://boards.greenhouse.io/{domain}",
            f"https://boards-api.greenhouse.io/v1/boards/{domain}/jobs",
        ]:
            try:
                r = requests.get(pattern, headers=HEADERS, timeout=8, allow_redirects=True)
                if r.status_code == 200 and len(r.text) > 500:
                    url = f"https://boards.greenhouse.io/{domain}"
                    print(f"    Found Greenhouse: {url}")
                    return url, detect_platform(url)
            except Exception:
                pass

    # Try Lever
    for domain in domain_guesses:
        url = f"https://jobs.lever.co/{domain}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code == 200 and "lever" in r.text.lower():
                print(f"    Found Lever: {url}")
                return url, detect_platform(url)
        except Exception:
            pass

    # Try company website /careers, /jobs
    career_paths = ["careers", "jobs", "careers/jobs", "about/careers", "work-with-us"]
    for domain in domain_guesses:
        for path in career_paths:
            url = f"https://www.{domain}.com/{path}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
                if r.status_code == 200 and len(r.text) > 1000:
                    # Check if it's a real careers page
                    if any(kw in r.text.lower() for kw in ["job", "position", "role", "opening", "career"]):
                        print(f"    Found via website: {r.url}")
                        platform_cfg = detect_platform(r.url, r.text)
                        return r.url, platform_cfg
            except Exception:
                pass

    # Ask LLM as last resort
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [{
                    "role": "user",
                    "content": f"""What is the careers/jobs page URL for {company_name}?
Return ONLY the URL, nothing else. Common patterns:
- boards.greenhouse.io/company
- jobs.lever.co/company
- company.wd5.myworkdayjobs.com/CompanyCareers
- careers.company.com"""
                }],
                "stream": False,
                "options": {"num_predict": 80},
            },
            timeout=30,
        )
        url_text = r.json().get("message", {}).get("content", "").strip()
        # Strip markdown link syntax: [text](url) → url, or url](url) → url
        url_text = re.sub(r'\]\(https?://[^\s\)]*\)', '', url_text)
        url_text = re.sub(r'\[([^\]]*)\]', r'\1', url_text)
        url_match = re.search(r'https?://[^\s\'"<>\]\)]+', url_text)
        if url_match:
            url = url_match.group(0).rstrip(".,;)]}>")
            print(f"    LLM suggested: {url}")
            platform_cfg = detect_platform(url)
            return url, platform_cfg
    except Exception:
        pass

    return "", {"platform": "unknown"}


# ── Platform-specific scrapers ────────────────────────────────────────────────

def is_tpm_title(title: str, company_name: str = "") -> bool:
    """
    Returns True if title is a TPM/PM role at Senior level or above.
    Devon is Staff TPM — filters out junior/associate/coordinator roles.
    Supports per-company overrides for non-standard TPM titles.
    """
    t = title.lower()

    # Hard exclude junior signals regardless of company
    if any(sig in t for sig in _JUNIOR_SIGNALS):
        return False

    # Company-specific overrides
    company_lower = company_name.lower()
    for key, override in TITLE_OVERRIDE_MAP.items():
        if key in company_lower:
            return any(kw in t for kw in override)

    return any(kw in t for kw in TPM_KEYWORDS)


def _detect_level(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["principal", "distinguished"]): return "Principal"
    if "staff" in t: return "Staff"
    if any(w in t for w in ["senior", "sr.", "lead"]): return "Senior"
    if any(w in t for w in ["director", "head of", "vp"]): return "Director+"
    return "Mid"


def _detect_work_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["remote", "work from home", "work from anywhere"]): return "Remote"
    if any(w in t for w in ["hybrid", "flex"]): return "Hybrid"
    if any(w in t for w in ["on-site", "onsite"]): return "On-site"
    return ""


def _safe_get(d: dict, key: str, default=""):
    """Safely get nested keys like 'location.name'."""
    parts = key.split(".")
    val = d
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part, default)
        elif isinstance(val, list) and val:
            val = val[0] if part == "0" else default
        else:
            return default
    return str(val) if val else default


def scrape_greenhouse(company_name: str, cfg: dict) -> list[dict]:
    """Scrape via Greenhouse public JSON API with token fallback."""
    jobs = []
    try:
        board_id = cfg.get("board_id", "")
        tokens_to_try = [board_id]
        if board_id:
            tokens_to_try += [board_id + "2", board_id.replace("-", "")]

        working_data = None
        for token in tokens_to_try:
            token_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            r = requests.get(token_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                working_data = r.json()
                if token != board_id:
                    print(f"    Greenhouse fallback token worked: {token}")
                break
            elif r.status_code == 404:
                continue

        if working_data is None:
            print(f"    Greenhouse API returned 404 for all tokens — board may have migrated")
            return []

        job_list = working_data.get("jobs", working_data) if isinstance(working_data, dict) else working_data

        for item in job_list:
            if not isinstance(item, dict):
                continue
            title = item.get(cfg.get("title_key", "title"), "")
            if not is_tpm_title(title, company_name):
                continue
            job_id = str(item.get(cfg.get("id_key", "id"), ""))
            location = _safe_get(item, cfg.get("location_key", "location.name"))
            content = item.get("content", "")
            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location,
                "work_type":  _detect_work_type(f"{title} {location} {content}"),
                "url":        cfg.get("url_template", "").format(id=job_id),
                "source":     "custom:greenhouse",
                "job_id":     job_id,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
                "description": BeautifulSoup(content, "html.parser").get_text(" ", strip=True)[:2000] if content else "",
            })

    except Exception as e:
        print(f"    Greenhouse error: {e}")
    return jobs


def scrape_lever(company_name: str, cfg: dict) -> list[dict]:
    """Scrape via Lever public JSON API."""
    jobs = []
    try:
        r = requests.get(cfg["api_url"], headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"    Lever API returned {r.status_code}")
            return []

        data = r.json()
        for item in data:
            if not isinstance(item, dict):
                continue
            title = item.get(cfg.get("title_key", "text"), "")
            if not is_tpm_title(title, company_name):
                continue

            job_id = str(item.get(cfg.get("id_key", "id"), ""))
            location = _safe_get(item, cfg.get("location_key", "categories.location"))
            desc = item.get("descriptionPlain", "")

            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location,
                "work_type":  _detect_work_type(f"{title} {location} {desc}"),
                "url":        item.get("hostedUrl", cfg.get("url_template", "").format(id=job_id)),
                "source":     f"custom:lever",
                "job_id":     job_id,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
                "description": desc[:2000],
            })

    except Exception as e:
        print(f"    Lever error: {e}")
    return jobs


def scrape_workday(company_name: str, cfg: dict) -> list[dict]:
    """
    Scrape via Workday REST API (POST with session cookie + CSRF token).

    Root cause of 422 on newer tenants: Workday requires an X-Calypso-CSRF-Token
    header obtained from the session page. Older tenants tolerate its absence;
    newer ones (Waters, Moderna, Bose, BostonScientific, etc.) hard-require it.
    Adding it is safe across all tenants — it's ignored where not needed.

    Also: never include empty 'locations': [] in the body. Causes 422 on strict tenants.
    """
    jobs = []
    try:
        session = requests.Session()
        session_url = cfg.get("session_url", "")
        csrf_token = ""

        # Establish session cookies AND extract CSRF token
        if session_url:
            try:
                resp = session.get(session_url, headers=HEADERS, timeout=12)
                if "community.workday.com" in resp.url or "outage" in resp.text.lower():
                    print(f"    Workday outage detected — skipping")
                    return []

                # 1. Check cookies for CSRF token
                for name in session.cookies.keys():
                    if "csrf" in name.lower():
                        csrf_token = session.cookies[name]
                        break

                # 2. Check HTML for CSRF token in meta or script tags
                if not csrf_token:
                    for pattern in CSRF_PATTERNS:
                        m = re.search(pattern, resp.text, re.IGNORECASE)
                        if m:
                            csrf_token = m.group(1)
                            break

            except Exception as e:
                print(f"    Workday session error: {e}")

        search_queries = ["technical program manager", "program manager", "product manager"]
        seen_ids = set()
        api_url = cfg["api_url"]

        post_headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": session_url or api_url,
        }
        if csrf_token:
            post_headers["X-Calypso-CSRF-Token"] = csrf_token

        for query in search_queries:
            # NOTE: do NOT include empty "locations": [] — causes 422 on strict tenants
            bodies_to_try = [
                {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query},
                {"searchText": query, "limit": 20, "offset": 0},
                {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query,
                 "locationProximity": None},
            ]

            success = False
            for body in bodies_to_try:
                try:
                    r = session.post(api_url, json=body, headers=post_headers, timeout=15)
                except Exception as e:
                    print(f"    Workday request error: {e}")
                    break

                if r.status_code == 200:
                    success = True
                    data = r.json()
                    for item in data.get("jobPostings", []):
                        title = item.get(cfg.get("title_key", "title"), "")
                        if not is_tpm_title(title, company_name):
                            continue
                        path = item.get(cfg.get("id_key", "externalPath"), "")
                        if path in seen_ids:
                            continue
                        seen_ids.add(path)
                        location = item.get(cfg.get("location_key", "locationsText"), "")
                        job_url = cfg.get("url_template", "").format(id=path)
                        jobs.append({
                            "company":    company_name,
                            "title":      title,
                            "location":   location,
                            "work_type":  _detect_work_type(f"{title} {location}"),
                            "url":        job_url,
                            "source":     "custom:workday",
                            "job_id":     path,
                            "found_date": datetime.now().strftime("%Y-%m-%d"),
                            "level":      _detect_level(title),
                        })
                    break  # working body found
                elif r.status_code == 422:
                    continue  # try next body format
                else:
                    break  # unexpected error — don't retry

            if not success:
                print(f"    Workday API returned 422 for '{query}' (all body formats failed)")
            time.sleep(1)

    except Exception as e:
        print(f"    Workday error: {e}")
    return jobs


def scrape_workday_playwright(company_name: str, cfg: dict) -> list[dict]:
    """
    Playwright-based Workday scraper for wd5 tenants that don't surface the
    CSRF token in static HTML. Renders the career page with a real browser so
    JS executes and the CSRF cookie is set, then reuses the same POST API logic.
    """
    jobs = []
    session_url = cfg.get("session_url", "")
    api_url = cfg.get("api_url", "")
    csrf_token = ""
    cookie_header = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS.get("User-Agent", ""))
            page = context.new_page()
            try:
                page.goto(session_url, wait_until="networkidle", timeout=30000)
            except Exception:
                page.goto(session_url, wait_until="domcontentloaded", timeout=20000)

            # 1. Check cookies — most reliable source for wd5 CSRF token
            cookies = context.cookies()
            for c in cookies:
                if "csrf" in c["name"].lower():
                    csrf_token = c["value"]
                    break

            # 2. Fallback: check rendered HTML
            if not csrf_token:
                content = page.content()
                for pattern in CSRF_PATTERNS:
                    m = re.search(pattern, content, re.IGNORECASE)
                    if m:
                        csrf_token = m.group(1)
                        break

            # Build Cookie header string from browser session
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            browser.close()
    except Exception as e:
        print(f"    Workday Playwright session error: {e}")
        return jobs

    if not csrf_token:
        print(f"    Workday Playwright: no CSRF token found for {company_name} — request will likely 422")

    search_queries = ["technical program manager", "program manager", "product manager"]
    seen_ids = set()

    post_headers = {
        **HEADERS,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": session_url or api_url,
    }
    if csrf_token:
        post_headers["X-Calypso-CSRF-Token"] = csrf_token
    if cookie_header:
        post_headers["Cookie"] = cookie_header

    import requests as _requests
    session = _requests.Session()

    for query in search_queries:
        bodies_to_try = [
            {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query},
            {"searchText": query, "limit": 20, "offset": 0},
            {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query, "locationProximity": None},
        ]
        success = False
        for body in bodies_to_try:
            try:
                r = session.post(api_url, json=body, headers=post_headers, timeout=15)
            except Exception as e:
                print(f"    Workday Playwright request error: {e}")
                break

            if r.status_code == 200:
                success = True
                data = r.json()
                for item in data.get("jobPostings", []):
                    title = item.get(cfg.get("title_key", "title"), "")
                    if not is_tpm_title(title, company_name):
                        continue
                    path = item.get(cfg.get("id_key", "externalPath"), "")
                    if path in seen_ids:
                        continue
                    seen_ids.add(path)
                    location = item.get(cfg.get("location_key", "locationsText"), "")
                    job_url = cfg.get("url_template", "").format(id=path)
                    jobs.append({
                        "company":    company_name,
                        "title":      title,
                        "location":   location,
                        "work_type":  _detect_work_type(f"{title} {location}"),
                        "url":        job_url,
                        "source":     "custom:workday",
                        "job_id":     path,
                        "found_date": datetime.now().strftime("%Y-%m-%d"),
                        "level":      _detect_level(title),
                    })
                break
            elif r.status_code == 422:
                continue
            else:
                break

        if not success:
            print(f"    Workday Playwright API 422 for '{query}' on {company_name} (all body formats failed)")
        time.sleep(1)

    return jobs


def scrape_oracle(company_name: str, cfg: dict) -> list[dict]:
    """Scrape via Oracle HCM REST API."""
    jobs = []
    try:
        r = requests.get(
            cfg["api_url"],
            params=cfg.get("api_params", {}),
            headers={**HEADERS, "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    Oracle API returned {r.status_code}")
            # Fall back to Playwright
            return scrape_generic_playwright(company_name, cfg.get("url_template", "").split("{")[0])

        data = r.json()
        raw_items = data.get("items", [])

        # Oracle HCM Fusion (e.g. jobs.akamai.com) nests jobs in items[0].requisitionList;
        # older Oracle tenants put job objects directly in items[].
        if raw_items and "requisitionList" in raw_items[0]:
            job_list = raw_items[0].get("requisitionList", [])
        else:
            job_list = raw_items

        for item in job_list:
            title = item.get(cfg.get("title_key", "Title"), "")
            if not is_tpm_title(title, company_name):
                continue

            job_id = str(item.get(cfg.get("id_key", "Id"), ""))
            location = item.get(cfg.get("location_key", "PrimaryLocation"), "")

            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location,
                "work_type":  _detect_work_type(f"{title} {location}"),
                "url":        cfg.get("url_template", "").format(id=job_id),
                "source":     "custom:oracle",
                "job_id":     job_id,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
            })

    except Exception as e:
        print(f"    Oracle error: {e}")
    return jobs


def scrape_smartrecruiters(company_name: str, cfg: dict) -> list[dict]:
    """Scrape via SmartRecruiters public API."""
    jobs = []
    try:
        r = requests.get(
            cfg["api_url"],
            params=cfg.get("api_params", {}),
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return []

        data = r.json()
        for item in data.get("content", []):
            title = item.get(cfg.get("title_key", "name"), "")
            if not is_tpm_title(title, company_name):
                continue

            job_id = str(item.get(cfg.get("id_key", "id"), ""))
            loc = item.get("location", {})
            location = f"{loc.get('city', '')}, {loc.get('region', '')}".strip(", ")

            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location,
                "work_type":  _detect_work_type(f"{title} {location}"),
                "url":        cfg.get("url_template", "").format(id=job_id),
                "source":     f"custom:smartrecruiters",
                "job_id":     job_id,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
            })

    except Exception as e:
        print(f"    SmartRecruiters error: {e}")
    return jobs


def scrape_generic_playwright(company_name: str, career_url: str) -> list[dict]:
    """
    Generic Playwright scraper for unknown ATS platforms.
    Tries multiple selector strategies and also intercepts JSON API responses.
    """
    jobs = []
    seen = set()

    def _scrape():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA)
            page = ctx.new_page()

            # Intercept JSON responses that might contain job data
            api_jobs = []

            def on_response(resp):
                try:
                    if resp.status != 200:
                        return
                    ct = resp.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    data = resp.json()
                    data_str = str(data).lower()
                    if any(kw in data_str for kw in ["program manager", "tpm", "project manager"]):
                        api_jobs.append({"url": resp.url, "data": data})
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(career_url, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                html = page.content()
            except Exception as e:
                print(f"    Navigation error: {e}")
                browser.close()
                return

            browser.close()

            # If we captured JSON API responses with job data, try parsing them
            if api_jobs:
                print(f"    Captured {len(api_jobs)} JSON responses with job data")
                for api_resp in api_jobs:
                    _parse_api_jobs(api_resp["data"], company_name, career_url, jobs, seen)

            # Also parse DOM
            soup = BeautifulSoup(html, "html.parser")
            domain = career_url.split("/")[2] if "/" in career_url else ""

            # Multi-strategy link finding
            selectors = [
                "a.posting-title",  # Greenhouse embedded
                "a[href*='/jobs/']",
                "a[href*='/job/']",
                "a[href*='/careers/']",
                "a[href*='/positions/']",
                "a[href*='/openings/']",
                "a[href*='/role/']",
                "a[href*='/requisitions/']",
                ".opening a", ".job-post a",
                "[class*='job-title'] a",
                "[class*='position-title'] a",
                "[class*='job'] a[href]",
                "h2 a", "h3 a",
            ]

            found_links = []
            for sel in selectors:
                links = soup.select(sel)
                if links:
                    found_links.extend(links)

            # Also find links whose text looks like a job title
            for a in soup.find_all("a", href=True):
                t = a.get_text(strip=True).lower()
                if any(kw in t for kw in TPM_KEYWORDS):
                    found_links.append(a)

            print(f"    Found {len(found_links)} candidate links in DOM")

            for link in found_links[:80]:
                try:
                    href = link.get("href", "")
                    if not href or href in seen or href == "#":
                        continue
                    seen.add(href)

                    title = link.get_text(strip=True)
                    if not title:
                        for el in link.select("h2,h3,h4,span,div"):
                            t = el.get_text(strip=True)
                            if t and len(t) > 5:
                                title = t
                                break

                    if not title or len(title) < 4 or not is_tpm_title(title, company_name):
                        continue

                    # Build full URL
                    if href.startswith("/"):
                        base = "/".join(career_url.split("/")[:3])
                        full_url = base + href
                    elif href.startswith("http"):
                        full_url = href
                    else:
                        full_url = career_url.rstrip("/") + "/" + href

                    # Location from parent card
                    card = (link.find_parent("li") or
                            link.find_parent("[class*='posting']") or
                            link.find_parent("div") or link.parent)
                    location = ""
                    if card:
                        loc_el = card.select_one(
                            "[class*='location'],[class*='office'],"
                            "[class*='remote'],span.location,.posting-location"
                        )
                        if loc_el:
                            location = loc_el.get_text(strip=True)

                    jobs.append({
                        "company":    company_name,
                        "title":      title,
                        "location":   location,
                        "work_type":  _detect_work_type(f"{title} {location}"),
                        "url":        full_url,
                        "source":     f"custom:{domain}",
                        "job_id":     href[:100],
                        "found_date": datetime.now().strftime("%Y-%m-%d"),
                        "level":      _detect_level(title),
                    })
                except Exception:
                    continue

    t = threading.Thread(target=_scrape, daemon=True)
    t.start()
    t.join(timeout=60)
    if t.is_alive():
        print(f"    Playwright scraper timed out")

    return jobs


def _parse_api_jobs(data, company_name: str, career_url: str, jobs: list, seen: set):
    """Try to extract jobs from a captured JSON API response."""
    # Recursively find lists of dicts that look like jobs
    def _find_job_lists(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, list):
            # Check if this looks like a job list
            if len(obj) > 0 and isinstance(obj[0], dict):
                keys = set(obj[0].keys())
                if keys & {"title", "name", "postingTitle", "text", "jobTitle", "Title"}:
                    for item in obj:
                        if not isinstance(item, dict):
                            continue
                        title = (item.get("title") or item.get("name") or
                                 item.get("postingTitle") or item.get("text") or
                                 item.get("jobTitle") or item.get("Title") or "")
                        if not title or not is_tpm_title(title, company_name):
                            continue
                        job_id = str(item.get("id", item.get("Id", item.get("requisitionId", ""))))
                        if job_id in seen:
                            continue
                        seen.add(job_id or title)

                        location = (item.get("location", "") or item.get("PrimaryLocation", "") or
                                    item.get("locationsText", ""))
                        if isinstance(location, dict):
                            location = location.get("name", location.get("city", ""))
                        elif isinstance(location, list):
                            location = str(location[0]) if location else ""

                        jobs.append({
                            "company":    company_name,
                            "title":      str(title),
                            "location":   str(location),
                            "work_type":  _detect_work_type(f"{title} {location}"),
                            "url":        str(item.get("url", item.get("hostedUrl", career_url))),
                            "source":     f"custom:api",
                            "job_id":     job_id,
                            "found_date": datetime.now().strftime("%Y-%m-%d"),
                            "level":      _detect_level(str(title)),
                        })
        if isinstance(obj, dict):
            for v in obj.values():
                _find_job_lists(v, depth + 1)

    _find_job_lists(data)


def scrape_icims(company_name: str, cfg: dict) -> list[dict]:
    """
    iCIMS scraper — pure requests, no Playwright.
    Uses in_iframe=1&pr=0 parameter which returns full job listing HTML directly.
    Confirmed working: returns 10+ job links for Waters Corporation.
    """
    jobs = []

    # Get base URL from cfg, fall back to deriving from intro_url
    base = cfg.get("base_url", "")
    if not base:
        intro_url = cfg.get("intro_url", "")
        base = intro_url.rsplit("/jobs", 1)[0] if "/jobs" in intro_url else intro_url
    if not base:
        print("    iCIMS: no base_url in cfg")
        return []

    # This is the confirmed working URL pattern for iCIMS
    search_url = f"{base}/jobs/search?ss=1&searchKeyword=program+manager&in_iframe=1&pr=0"

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = requests.get(search_url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"    iCIMS returned {r.status_code} for {search_url}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()

        for a in soup.find_all("a", href=True):
            href = str(a.get("href", ""))
            if not re.search(r'/jobs/\d+/.+/job', href):
                continue
            if href in seen:
                continue
            seen.add(href)

            title = a.get_text(strip=True)
            if not title:
                parent = a.find_parent("div") or a.find_parent("li") or a.find_parent("tr")
                if parent:
                    h = parent.select_one("h2,h3,h4,[class*=title],[class*=job-title]")
                    title = h.get_text(strip=True) if h else ""

            if not title or not is_tpm_title(title, company_name):
                continue

            full_url = href if href.startswith("http") else f"{base}{href}"

            card = a.find_parent("li") or a.find_parent("tr") or a.find_parent("div")
            location = ""
            if card:
                loc = card.select_one(
                    "[class*=location],[class*=city],[class*=Location],"
                    ".iCIMS_JobsTable_Location,td.location"
                )
                if loc:
                    location = loc.get_text(strip=True)

            job_id_m = re.search(r'/jobs/(\d+)/', href)
            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location or "Milford, MA",
                "work_type":  _detect_work_type(f"{title} {location}"),
                "url":        full_url,
                "source":     "custom:icims",
                "job_id":     job_id_m.group(1) if job_id_m else href[:50],
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
            })

    except Exception as e:
        print(f"    iCIMS scraper error: {e}")

    return jobs


_ASHBY_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings {
      id
      title
      locationName
      employmentType
    }
  }
}
"""


def scrape_ashby(company_name: str, cfg: dict) -> list[dict]:
    """Scrape via Ashby public GraphQL API — no auth required."""
    jobs = []
    slug = cfg.get("ashby_slug", "")
    if not slug:
        print(f"    Ashby: no slug configured for {company_name}")
        return jobs

    try:
        r = requests.post(
            cfg["api_url"],
            json={
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": slug},
                "query": _ASHBY_QUERY,
            },
            headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    Ashby API returned {r.status_code} for {company_name}")
            return jobs

        postings = r.json().get("data", {}).get("jobBoard", {})
        if postings is None:
            print(f"    Ashby: no job board found for slug '{slug}' — check company slug")
            return jobs

        for item in postings.get("jobPostings", []):
            title = item.get("title", "")
            if not is_tpm_title(title, company_name):
                continue
            job_id = item.get("id", "")
            location = item.get("locationName", "")
            jobs.append({
                "company":    company_name,
                "title":      title,
                "location":   location,
                "work_type":  _detect_work_type(f"{title} {location}"),
                "url":        cfg.get("url_template", "").format(id=job_id),
                "source":     "custom:ashby",
                "job_id":     job_id,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "level":      _detect_level(title),
            })

    except Exception as e:
        print(f"    Ashby error for {company_name}: {e}")

    return jobs


# ── Main scraper dispatcher ──────────────────────────────────────────────────

PLATFORM_SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever":      scrape_lever,
    "workday":    scrape_workday,
    "oracle":     scrape_oracle,
    "smartrecruiters": scrape_smartrecruiters,
    "icims":      scrape_icims,
    "ashby":      scrape_ashby,
}


def scrape_company_jobs(company_name: str, career_url: str, platform_cfg: dict = None) -> list[dict]:
    """
    Dispatch to the correct platform scraper, or fall back to generic Playwright.
    """
    if platform_cfg is None:
        platform_cfg = detect_platform(career_url)

    platform = platform_cfg.get("platform", "generic")
    scraper_fn = PLATFORM_SCRAPERS.get(platform)

    if scraper_fn:
        # wd5 Workday tenants require a Playwright-rendered session to obtain the CSRF token
        if platform == "workday" and "wd5.myworkdayjobs.com" in career_url:
            print(f"    Using workday-playwright scraper (wd5 tenant)")
            jobs = scrape_workday_playwright(company_name, platform_cfg)
        else:
            print(f"    Using {platform} scraper")
            jobs = scraper_fn(company_name, platform_cfg)
        if jobs:
            return jobs
        print(f"    {platform} scraper returned 0 — falling back to Playwright")

    # Generic Playwright fallback
    print(f"    Using generic Playwright scraper")
    return scrape_generic_playwright(company_name, career_url)


# ── DB operations ─────────────────────────────────────────────────────────────

def save_jobs(jobs: list[dict]) -> int:
    """Save to DB, return new count."""
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    new = 0
    for j in jobs:
        try:
            con.execute(
                "INSERT INTO jobs (company,title,location,work_type,level,url,source,job_id,found_date,description) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (j.get("company", ""), j.get("title", ""), j.get("location", ""),
                 j.get("work_type", ""), j.get("level", ""), j.get("url", ""),
                 j.get("source", ""), j.get("job_id", ""), j.get("found_date", ""),
                 j.get("description", ""))
            )
            new += 1
        except sqlite3.IntegrityError:
            pass  # duplicate URL
        except Exception as e:
            print(f"    DB error: {e}")
    con.commit()
    con.close()
    return new


# ── Main scrape loop ─────────────────────────────────────────────────────────

def _scrape_one_target(args_tuple):
    """Worker for parallel scraping. Returns (key, name, jobs, skip_reason)."""
    key, t, company_filter = args_tuple
    name = t.get("name", key)

    if company_filter and company_filter.lower() not in name.lower():
        return key, name, [], "filtered"

    # Skip if scraped in the last 23 hours
    last = t.get("last_scraped", "")
    if last:
        try:
            from datetime import timedelta as _td
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            if (datetime.now() - last_dt).total_seconds() < 82800:  # 23h
                return key, name, [], "recent"
        except Exception:
            pass

    if not t.get("career_url"):
        return key, name, [], "no_url"

    career_url   = t["career_url"]
    platform_cfg = t.get("platform_cfg") or detect_platform(career_url)

    try:
        jobs = scrape_company_jobs(name, career_url, platform_cfg)
        return key, name, jobs, None
    except Exception as e:
        return key, name, [], f"error:{e}"


def scrape_all_custom(company_filter: str = "", parallel: int = 8) -> int:
    """
    Scrape all custom targets in parallel (default 8 workers).
    Returns total new jobs added.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = load_targets()
    if not targets:
        print("  No custom targets.")
        return 0

    # Phase 1 (sequential): cache-bust URLs + discover missing ones
    needs_url = []
    for key, t in list(targets.items()):
        name = t.get("name", key)
        if company_filter and company_filter.lower() not in name.lower():
            continue

        name_key = name.lower().replace(" ", "").replace("/", "").replace("-", "")
        known_url = KNOWN_CAREER_URLS.get(name_key) or KNOWN_CAREER_URLS.get(
            name.lower().replace(" ", "_")
        )
        if known_url and t.get("career_url") and t["career_url"] != known_url:
            print(f"  {name}: updating stale cached URL")
            t["career_url"]   = known_url
            t["platform_cfg"] = None
            t["platform"]     = None
            cfg = detect_platform(known_url)
            t["platform_cfg"] = cfg
            t["platform"]     = cfg.get("platform", "unknown")

        if not t.get("career_url"):
            needs_url.append(key)

    if needs_url:
        print(f"  Discovering URLs for {len(needs_url)} new companies...")
        for key in needs_url:
            t    = targets[key]
            name = t.get("name", key)
            url, cfg = discover_career_url(name)
            if url:
                t["career_url"]   = url
                t["platform"]     = cfg.get("platform", "unknown")
                t["platform_cfg"] = cfg
                print(f"    {name}: {url}")
            else:
                print(f"    {name}: not found -- skipping")

    for key, t in targets.items():
        if t.get("career_url") and not t.get("platform_cfg"):
            t["platform_cfg"] = detect_platform(t["career_url"])
            t["platform"]     = t["platform_cfg"].get("platform", "unknown")

    save_targets(targets)

    # Phase 2 (parallel): scrape all targets that have a URL
    items = [
        (key, t, company_filter)
        for key, t in targets.items()
        if t.get("career_url")
    ]

    workers   = 1 if company_filter else parallel
    total_new = 0
    skipped   = 0

    print(f"  Scraping {len(items)} companies ({workers} parallel workers)...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scrape_one_target, item): item for item in items}
        for future in as_completed(futures):
            try:
                key, name, jobs, skip = future.result(timeout=90)
            except Exception as e:
                print(f"  [!] Worker error: {e}")
                continue

            if skip == "filtered":
                continue
            if skip in ("recent", "no_url"):
                skipped += 1
                continue
            if skip and skip.startswith("error"):
                print(f"  {name}: {skip}")
                continue

            print(f"  {name}: {len(jobs)} matching jobs")
            if jobs:
                new = save_jobs(jobs)
                targets[key]["last_scraped"] = datetime.now().strftime("%Y-%m-%d")
                targets[key]["job_count"]    = targets[key].get("job_count", 0) + new
                total_new += new
                if new:
                    print(f"    -> {new} new saved")

    save_targets(targets)

    if skipped:
        print(f"  Skipped {skipped} companies (scraped recently or no URL)")

    return total_new
def detect_and_report(company_name: str):
    """Run detection only and print results."""
    print(f"\n  Detecting ATS platform for {company_name}...")
    url, cfg = discover_career_url(company_name)
    if url:
        print(f"\n  Career URL: {url}")
        print(f"  Platform:   {cfg.get('platform', 'unknown')}")
        if cfg.get("api_url"):
            print(f"  API URL:    {cfg['api_url']}")
            print(f"  Method:     {cfg.get('api_method', 'GET')}")
        if cfg.get("job_path"):
            print(f"  Job path:   {cfg['job_path']}")
        print(f"\n  Full config:")
        print(json.dumps(cfg, indent=2, default=str))
    else:
        print(f"  Could not find career page for {company_name}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--add",     type=str, help="Comma-separated company names to add")
    parser.add_argument("--remove",  type=str, help="Company name to remove")
    parser.add_argument("--list",    action="store_true")
    parser.add_argument("--scrape",  action="store_true", help="Scrape all custom targets now")
    parser.add_argument("--company", type=str, help="Filter to specific company when scraping")
    parser.add_argument("--detect",  type=str, help="Detect ATS platform for a company")
    args = parser.parse_args()

    if args.add:
        names = [n.strip() for n in args.add.split(",")]
        add_companies(names)
        print("\n  Run 'python custom_targets.py --scrape' to scrape them now")

    elif args.remove:
        remove_company(args.remove)

    elif args.list:
        list_targets()

    elif args.detect:
        detect_and_report(args.detect)

    elif args.scrape:
        total = scrape_all_custom(company_filter=args.company or "")
        print(f"\n  Done — {total} new jobs added")

    else:
        list_targets()
        print("\n  Usage:")
        print("  python custom_targets.py --add 'Wayfair, HubSpot, Akamai, Nvidia'")
        print("  python custom_targets.py --list")
        print("  python custom_targets.py --scrape")
        print("  python custom_targets.py --detect 'Nvidia'")

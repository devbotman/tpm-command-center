"""
describe.py — Job description fetcher
======================================
Fetches and stores job descriptions for jobs that have a URL but no description.
Uses requests for static pages, Playwright for JS-rendered pages.

Usage:
    python describe.py              # fetch descriptions for top 30 unscored new jobs
    python describe.py --limit 100  # fetch more
    python describe.py --scored     # fetch for already-scored jobs (to improve re-scoring)

Called by scorer.py before scoring to give the LLM richer signal.
"""

import re
import sqlite3
import time
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "jobs.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Sources that are always JS-rendered — skip requests, go straight to Playwright
PLAYWRIGHT_SOURCES = {
    "amazon.jobs", "careers.google.com", "linkedin.com",
    "linkedin.com/top-picks", "careers.tiktok.com",
}

# Sources where requests works fine
REQUESTS_OK_SOURCES = {
    "custom:greenhouse", "custom:boards.greenhouse.io", "greenhouse_api",
    "custom:ashby", "custom:lever",
}

# Tags whose content we want to extract (in priority order)
_DESC_SELECTORS = [
    "div.job-description",
    "div[class*='description']",
    "div[class*='job-detail']",
    "div[class*='content']",
    "section[class*='description']",
    "article",
    "main",
]

# How many chars to store (truncate to save DB space)
MAX_CHARS = 4000


def get_db():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def _fetch_requests(url: str) -> str | None:
    """Try fetching with requests. Returns extracted text or None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200 and len(r.text) > 500:
            return _extract_text(r.text)
    except Exception:
        pass
    return None


def _fetch_playwright(url: str) -> str | None:
    """Render with Playwright and extract text. Returns None on failure."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            content = page.content()
            browser.close()
        return _extract_text(content)
    except Exception:
        return None


def fetch_description(url: str, source: str = "") -> str | None:
    """
    Fetch job description for a given URL.
    Tries requests first (fast), falls back to Playwright for JS sources.
    Returns plain text or None if fetch fails.
    """
    if not url or not url.startswith("http"):
        return None

    source_lower = source.lower()

    # Always use Playwright for known JS-only sources
    if any(s in source_lower or s in url.lower() for s in PLAYWRIGHT_SOURCES):
        return _fetch_playwright(url)

    # Try requests first
    text = _fetch_requests(url)
    if text and len(text) > 200:
        return text

    # Fall back to Playwright if requests got too little content
    return _fetch_playwright(url)


def fetch_descriptions_for_jobs(limit: int = 30, scored_only: bool = False,
                                 verbose: bool = True) -> int:
    """
    Fetch descriptions for jobs that have a URL but no description stored.

    Args:
        limit:       Max jobs to process
        scored_only: Only fetch for already-scored jobs (for re-scoring improvement)
        verbose:     Print progress

    Returns:
        Number of descriptions successfully fetched
    """
    con = get_db()

    if scored_only:
        query = """SELECT id, company, title, url, source FROM jobs
                   WHERE url != '' AND (description IS NULL OR description = '')
                   AND score IS NOT NULL
                   ORDER BY score DESC LIMIT ?"""
    else:
        query = """SELECT id, company, title, url, source FROM jobs
                   WHERE url != '' AND (description IS NULL OR description = '')
                   AND status = 'new'
                   ORDER BY found_date DESC LIMIT ?"""

    jobs = [dict(r) for r in con.execute(query, (limit,)).fetchall()]
    con.close()

    if not jobs:
        if verbose:
            print("  Describe: no jobs need descriptions.")
        return 0

    if verbose:
        print(f"  Describe: fetching descriptions for {len(jobs)} jobs...")

    fetched = 0
    for i, job in enumerate(jobs, 1):
        url = job.get("url", "")
        source = job.get("source", "")
        if verbose and i % 5 == 0:
            print(f"    {i}/{len(jobs)} — {job['company']}: {job['title'][:40]}")

        desc = fetch_description(url, source)
        if desc and len(desc) > 100:
            con = get_db()
            con.execute("UPDATE jobs SET description=? WHERE id=?", (desc, job["id"]))
            con.commit()
            con.close()
            fetched += 1
        else:
            if verbose:
                print(f"    ✗ {job['company']}: {job['title'][:40]} — no description fetched")

        time.sleep(0.3)  # polite rate limiting

    if verbose:
        print(f"  Describe: {fetched}/{len(jobs)} descriptions fetched successfully")

    return fetched


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch job descriptions into DB")
    parser.add_argument("--limit",  type=int, default=30)
    parser.add_argument("--scored", action="store_true",
                        help="Fetch for already-scored jobs (re-scoring prep)")
    args = parser.parse_args()
    fetch_descriptions_for_jobs(limit=args.limit, scored_only=args.scored)

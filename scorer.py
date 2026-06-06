"""
scorer.py — Standalone job scoring module
==========================================
Scores unscored jobs in jobs.db against Devon's profile using a local
Ollama LLM. Called by:
  - run_full_pipeline.py  (--score flag)
  - server.py             (_run_scout SSE handler)
  - CLI: python scorer.py [--limit N] [--all]

Returns a list of scored job dicts sorted by score desc.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH    = Path(__file__).parent / "jobs.db"
DIGEST_DIR = Path(__file__).parent / "digests"
MODEL      = "llama3.1:8b"

SCORE_PROMPT_TEMPLATE = """You are a TPM career coach. Score these job postings for Devon O'Rourke.

Devon's profile:
- Distinguished Technical Program Manager, 10 years experience
- E2E architecture, 5G/telecom, GCP/AWS cloud infrastructure, hardware/software delivery
- Lab testing, UAT, staging, pilot programs
- Executive reporting, stakeholder management, cross-functional leadership
- Target: Staff/Principal TPM or Associate Director, remote or Boston MA area
- Salary target: $150k-$200k

Jobs to score:
{job_list}

For each job, respond in this EXACT JSON format (array):
[
  {{"id": JOB_ID, "score": 1-10, "reason": "one sentence why", "flag": "any red flag or empty string"}},
  ...
]

Score 8-10: Strong match — TPM/Staff TPM/Principal TPM/Assoc Director, MA or Remote, telecom/cloud/infra
Score 5-7: Partial match — adjacent role, right domain wrong level, or great role but far location
Score 1-4: Poor fit — wrong domain, too junior, international only

Respond with ONLY the JSON array, no other text:"""


def get_db():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _rows_to_list(rows):
    return [dict(r) for r in rows]


def score_new_jobs(limit: int = 50, score_all: bool = False,
                   fetch_descriptions: bool = True, verbose: bool = True) -> list[dict]:
    """
    Score unscored new jobs. Returns sorted digest list.

    Args:
        limit:              Max jobs to score per run (default 50)
        score_all:          If True, re-score already-scored jobs too
        fetch_descriptions: Pre-fetch job descriptions to improve scoring accuracy
        verbose:            Print progress to stdout
    """
    import ollama

    con = get_db()
    if score_all:
        query = "SELECT * FROM jobs WHERE status='new' ORDER BY found_date DESC LIMIT ?"
    else:
        query = "SELECT * FROM jobs WHERE status='new' AND score IS NULL ORDER BY found_date DESC LIMIT ?"

    jobs = _rows_to_list(con.execute(query, (limit,)).fetchall())
    con.close()

    if not jobs:
        if verbose:
            print("  Scorer: no unscored new jobs found.")
        return []

    # Pre-fetch descriptions for jobs that don't have one (improves scoring quality)
    if fetch_descriptions:
        needs_desc = [j for j in jobs if not j.get("description")]
        if needs_desc and verbose:
            print(f"  Scorer: fetching descriptions for {len(needs_desc)} jobs first...")
        if needs_desc:
            try:
                from describe import fetch_descriptions_for_jobs
                fetch_descriptions_for_jobs(limit=len(needs_desc), verbose=verbose)
                # Reload jobs from DB to pick up newly fetched descriptions
                con = get_db()
                ids = tuple(j["id"] for j in jobs)
                placeholders = ",".join("?" * len(ids))
                jobs = _rows_to_list(con.execute(
                    f"SELECT * FROM jobs WHERE id IN ({placeholders})", ids
                ).fetchall())
                con.close()
            except Exception as e:
                if verbose:
                    print(f"  Scorer: description fetch skipped — {e}")

    if verbose:
        print(f"  Scorer: scoring {len(jobs)} jobs...")

    # Build job list — include description snippet if available
    def _job_line(j):
        base = f"[{j['id']}] {j['company']} | {j['title']} | {j.get('location') or 'Remote'}"
        desc = j.get("description") or ""
        if desc:
            # Include first 300 chars of description as context
            snippet = desc[:300].replace("\n", " ").strip()
            return f"{base}\n    Description: {snippet}"
        return base

    job_list = "\n".join(_job_line(j) for j in jobs)

    prompt = SCORE_PROMPT_TEMPLATE.format(job_list=job_list)

    try:
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        raw = response["message"]["content"].strip()
    except Exception as e:
        if verbose:
            print(f"  Scorer: Ollama error — {e}")
        return []

    # Extract JSON array from response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    scores = []
    if match:
        try:
            scores = json.loads(match.group())
        except Exception:
            scores = []

    score_map = {s["id"]: s for s in scores}

    digest = []
    for j in jobs:
        s = score_map.get(j["id"], {"score": 5, "reason": "Score unavailable", "flag": ""})
        digest.append({
            "id":       j["id"],
            "company":  j["company"],
            "title":    j["title"],
            "location": j.get("location") or "",
            "url":      j.get("url") or "",
            "score":    s.get("score", 5),
            "reason":   s.get("reason", ""),
            "flag":     s.get("flag", ""),
        })

    digest.sort(key=lambda x: x["score"], reverse=True)

    # Persist scores to DB
    con = get_db()
    for item in digest:
        con.execute("UPDATE jobs SET score=? WHERE id=?", (item["score"], item["id"]))
    con.commit()
    con.close()

    # Save digest JSON
    DIGEST_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    digest_path = DIGEST_DIR / f"digest_{date_str}.json"
    digest_path.write_text(json.dumps(digest, indent=2))

    strong = [d for d in digest if d["score"] >= 8]
    if verbose:
        print(f"  Scorer: {len(digest)} scored — {len(strong)} strong matches (score ≥ 8)")
        if strong:
            print("  Top picks:")
            for d in strong[:5]:
                print(f"    [{d['score']}] {d['company']} — {d['title']} ({d['location'] or 'Remote'})")

    return digest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Score unscored jobs in jobs.db")
    parser.add_argument("--limit", type=int, default=50, help="Max jobs to score")
    parser.add_argument("--all",   action="store_true", help="Re-score already-scored jobs too")
    args = parser.parse_args()
    score_new_jobs(limit=args.limit, score_all=args.all)

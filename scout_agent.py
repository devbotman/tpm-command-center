"""
TPM Job Scout Agent — Daily Job Scoring & Digest
=================================================
Reads new jobs from jobs.db, scores each one against Devon's profile,
and prints a ranked morning digest of the top matches.

Usage:
    python scout_agent.py              # score all unscored new jobs
    python scout_agent.py --top 10     # show top 10 matches
    python scout_agent.py --digest     # save digest to daily_digest.txt

Install:
    pip install crewai ollama
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from crewai import Agent, Crew, Process, Task
    import ollama as ollama_client
except ImportError:
    print("\n[!] Run: pip install crewai ollama\n")
    sys.exit(1)

OLLAMA_MODEL = "llama3.1:70b-instruct-q4_K_M"
DB_PATH      = "jobs.db"

CANDIDATE_PROFILE = """
Devon O'Rourke — Technical Program Manager
Location: Bellingham, MA (commutes to I-495 corridor, Route 128, and Boston metro)

CORE BACKGROUND:
- E2E architecture design and delivery
- Infrastructure planning and implementation  
- Telecom systems and carrier integrations
- Product roadmap development and cross-functional delivery
- Lab testing, UAT, staging, pilot product rollouts
- Executive reporting and board-level presentations
- Professional debugging across hardware and software stacks

PREFERENCES:
- Remote, hybrid, or on-site within ~40mi of Bellingham MA
- Nearest tech hubs: Westborough (12mi), Framingham (15mi), Waltham (25mi), Boston (38mi)
- Salary target: $130k-$180k+
- Team size: 5-50 engineers, technical depth required, strategic visibility

TARGET ROLES (in priority order):
1. Technical Program Manager / Senior TPM — core fit
2. Engineering Program Manager / Infrastructure PM — strong fit
3. Release Manager / Launch Manager — Devon has UAT, staging, pilot experience
4. Technical Operations Manager — fits infra + debugging background
5. Platform/Cloud Program Manager — fits architecture experience
6. Business Operations / Chief of Staff (Engineering) — fits exec reporting + cross-functional
7. Technical Product Manager — fits roadmap + delivery experience

TARGET COMPANIES:
- Big Tech: Google, Amazon, Microsoft, Meta, Apple
- MA Companies: Bose, MathWorks, Raytheon, HubSpot, Akamai, Wayfair, Toast, PTC, Dell/EMC
- Well-funded startups (Series B+) in MA or remote
- Any company with 200+ engineers that needs program management

RED FLAGS TO WATCH FOR:
- "Manager" in title but actually an individual contributor role
- Requires 10+ years in a very specific niche Devon doesn't have (e.g. ML/AI-native)
- Junior/Associate level roles
- International-only roles with no US/remote option
"""


def get_new_jobs(limit: int = 50) -> list[dict]:
    """Pull recent unscored jobs from the DB."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM jobs WHERE status = 'new' ORDER BY found_date DESC LIMIT ?",
            (limit,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"DB error: {e}")
        return []


def build_scout_crew(jobs: list[dict]) -> Crew:

    llm_config = {
        "model": f"ollama/{OLLAMA_MODEL}",
        "base_url": "http://localhost:11434",
    }

    job_list_text = "\n".join([
        f"[{i+1}] ID:{j['id']} | {j['company']} | {j['title']} | {j['location']} | {j['url']}"
        for i, j in enumerate(jobs)
    ])

    scout = Agent(
        role="TPM Career Coach & Job Match Specialist",
        goal=(
            "Analyze a list of TPM job postings and score each one against "
            "the candidate's profile. Identify the best matches and explain why."
        ),
        backstory=(
            "You are a specialized TPM career coach who has placed hundreds of "
            "Technical Program Managers at top tech companies. You have a sharp eye "
            "for which roles are genuine matches vs. title-only fits. You consider "
            "company stage, role scope, technical depth, and growth potential.\n\n"
            f"{CANDIDATE_PROFILE}"
        ),
        llm=llm_config,
        verbose=False,
        allow_delegation=False,
    )

    task_score = Task(
        description=(
            "Here are today's new job postings:\n\n"
            f"{job_list_text}\n\n"
            "For each job:\n"
            "1. Score it 1-10 based on fit with Devon's FULL profile (including adjacent roles)\n"
            "2. Give a one-line reason for the score\n"
            "3. Flag any red flags (wrong level, location mismatch, etc.)\n\n"
            "SCORING GUIDANCE:\n"
            "- Score 9-10: Core TPM role at a target company, remote or near Bellingham MA\n"
            "- Score 7-8: Adjacent role (release mgr, eng ops, platform PM) with strong skill overlap,\n"
            "  OR core TPM at a less-known company in MA\n"
            "- Score 5-6: Partial match — right skills but wrong level, or great role but far location\n"
            "- Score 3-4: Stretch role — Devon could do it but would need to learn significantly\n"
            "- Score 1-2: Poor fit — wrong domain, wrong level, or international only\n\n"
            "LOCATION BONUS: Jobs in Westborough, Framingham, Marlborough, Hopkinton, Milford\n"
            "  are VERY close to Devon (10-15mi). Waltham, Needham, Burlington are ~25-35mi.\n"
            "  Boston/Cambridge are ~38mi. Give a +1 location bonus for close commutes.\n\n"
            "BE ADVENTUROUS: Don't dismiss roles just because the title isn't 'TPM'.\n"
            "  Release Manager, Engineering Operations, Platform PM, Technical Operations,\n"
            "  and Chief of Staff (Engineering) are all strong fits for Devon's background.\n\n"
            "Then provide:\n"
            "- TOP 5 recommended jobs to apply to first (with scores and reasons)\n"
            "- HIDDEN GEMS: 2-3 jobs that aren't obvious TPM titles but match Devon's skills\n"
            "- SKIP list: jobs that are poor matches and why\n"
            "- One key tip for today's applications based on the job market signals you see\n\n"
            "Be direct and opinionated — Devon needs to prioritize efficiently."
        ),
        expected_output=(
            "A ranked digest with: top 5 picks (ID, company, title, score, reason), "
            "hidden gems with reasons, skip list with reasons, and one actionable tip."
        ),
        agent=scout,
    )

    return Crew(
        agents=[scout],
        tasks=[task_score],
        process=Process.sequential,
        verbose=False,
    )


def main():
    parser = argparse.ArgumentParser(description="TPM Job Scout Agent")
    parser.add_argument("--top",    type=int, default=5,  help="Number of top jobs to highlight")
    parser.add_argument("--limit",  type=int, default=30, help="Max jobs to score in one run")
    parser.add_argument("--digest", action="store_true",  help="Save digest to daily_digest.txt")
    args = parser.parse_args()

    print("\n" + "═"*55)
    print("  TPM Job Scout Agent — Morning Digest")
    print(f"  {datetime.now():%A, %B %d %Y}")
    print("═"*55 + "\n")

    # Check Ollama
    try:
        ollama_client.list()
    except Exception:
        print("[!] Ollama is not running. Open the Ollama desktop app first.\n")
        sys.exit(1)

    jobs = get_new_jobs(args.limit)
    if not jobs:
        print("No new jobs in tracker. Run scraper.py first.\n")
        sys.exit(0)

    print(f"Scoring {len(jobs)} new jobs...\n")

    crew   = build_scout_crew(jobs)
    result = crew.kickoff()

    output = str(result)
    print("\n" + "═"*55)
    print(output)
    print("═"*55 + "\n")

    if args.digest:
        digest_path = f"daily_digest_{datetime.now():%Y%m%d}.txt"
        Path(digest_path).write_text(
            f"TPM Job Digest — {datetime.now():%Y-%m-%d}\n{'='*55}\n\n{output}\n"
        )
        print(f"Digest saved to: {digest_path}\n")


if __name__ == "__main__":
    main()

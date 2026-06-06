"""
profile.py — Devon O'Rourke's Candidate Profile
=================================================
Loads candidate data from profile.yml (or a client-specific profile).
All agents import from this module — the constants below are the stable API.

To switch clients: set PROFILE_PATH env var before running, or pass --client
to run_full_pipeline.py which sets it automatically.

    PROFILE_PATH=clients/sarah_wk/profile.yml python run_full_pipeline.py
"""

import os
import yaml
from pathlib import Path

# ── Profile loader ─────────────────────────────────────────────────────────────

def load_profile(path: str | None = None) -> dict:
    """Load profile YAML. Falls back to profile.yml in the project root."""
    if path is None:
        path = os.environ.get("PROFILE_PATH", str(Path(__file__).parent / "profile.yml"))
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

_p = load_profile()

# ── Identity ──────────────────────────────────────────────────────────────────

_identity = _p.get("identity", {})
NAME     = _identity.get("name", "")
TITLE    = _identity.get("title", "")
LOCATION = _identity.get("location", "")
EMAIL    = _identity.get("email", "")
PHONE    = _identity.get("phone", "")

# ── Experience summary ────────────────────────────────────────────────────────

_exp = _p.get("experience", {})
YEARS_EXPERIENCE = _exp.get("years", 0)
CURRENT_ROLE     = _exp.get("current_role", "")
EXPERIENCE       = _exp.get("summary", "")
EDUCATION        = _p.get("education", "")

# ── Skills ────────────────────────────────────────────────────────────────────

_skills        = _p.get("skills", {})
TECHNICAL_SKILLS = _skills.get("technical", [])
SOFT_SKILLS      = _skills.get("soft", [])

# ── Job search preferences ────────────────────────────────────────────────────

_js = _p.get("job_search", {})
PREFERRED_LOCATIONS = _js.get("preferred_locations", [])
SALARY_FLOOR        = _js.get("salary_floor", 130000)
SALARY_TARGET_MIN   = _js.get("salary_target_min", 150000)
SALARY_TARGET_MAX   = _js.get("salary_target_max", 200000)
TARGET_COMPANIES    = _js.get("target_companies", [])
SEARCH_QUERIES      = _js.get("search_queries", [])
TARGET_TITLES       = _js.get("target_titles", [])

# ── Scoring weights ───────────────────────────────────────────────────────────

SCORING_PROFILE = f"""
Candidate: {NAME}
Title: {TITLE}
Location: {LOCATION}
Experience: {YEARS_EXPERIENCE} years

STRONG MATCHES (score 8-10):
- Any TPM, Technical PM, Staff TPM, Principal TPM, or Program Manager role
- Staff or Principal-level program/product management
- 5G, telecom, network, carrier, connectivity programs
- GCP or AWS cloud infrastructure programs
- Hardware/software integrated systems delivery
- Boston MA area, hybrid or remote roles
- Salary ${SALARY_TARGET_MIN//1000}k-${SALARY_TARGET_MAX//1000}k+
- Companies: {', '.join(TARGET_COMPANIES[:8])}

GOOD MATCHES (score 6-7):
- Associate Director or Director of Program/Technical Management
- Engineering Operations Manager, Delivery Manager, Release Program Manager
- Product Manager with technical depth
- Business/Strategy Operations with engineering org exposure
- Remote roles anywhere in USA
- Roles requiring executive communication and stakeholder management

WEAK MATCHES (score 3-5):
- Pure software engineering management (not program management)
- Sales, marketing, or non-technical operations
- Roles outside USA or requiring relocation outside MA/Remote
- Entry-level or IC contributor roles ({NAME.split()[0]} is senior/staff level)
- VP or C-suite (overleveled for current search)

RED FLAGS (score 1-2):
- International locations (outside USA)
- Salary below ${SALARY_FLOOR//1000}k
- Requires 15+ years experience (overqualified risk)
- Pure hardware without software/program component
"""

# ── System prompt for LLM chat ────────────────────────────────────────────────

CHAT_SYSTEM_PROMPT = f"""You are a career assistant and technical advisor for {NAME}, a {TITLE} based in {LOCATION}.

{NAME.split()[0]}'s background:
{EXPERIENCE}

Education: {EDUCATION}

Key strengths: {', '.join(TECHNICAL_SKILLS[:12])}

{NAME.split()[0]}'s job search preferences:
- Target locations: Boston MA area, Remote, Hybrid
- Filters OUT: International jobs, non-USA locations
- Target salary: ${SALARY_TARGET_MIN//1000}k-${SALARY_TARGET_MAX//1000}k
- Strong interest in: 5G/telecom, cloud infrastructure (GCP/AWS), E2E program delivery

When helping {NAME.split()[0]}:
- Be direct and specific — {NAME.split()[0]} is senior level and doesn't need hand-holding
- Use {NAME.split()[0]}'s actual experience and metrics when suggesting resume bullets
- Reference specific numbers ($8.38M program, 90,000 hours saved, 40% reduction) when relevant
- For job-specific questions, tailor advice to the exact role and company
- For code questions, default to Python and be pragmatic about solutions
"""

# ── Code editing permissions ──────────────────────────────────────────────────

EDITABLE_FILES = [
    "scraper.py",
    "server.py",
    "resume_agent.py",
    "scout_agent.py",
    "scraper_agent.py",
    "profile.py",
    "profile.yml",
]

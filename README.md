# 🎯 TPM Command Center

A self-hosted job search automation platform for senior Technical Program Managers. Scrapes 250+ company ATS boards, scores jobs with a local LLM, and surfaces the best matches in a real-time dashboard — all running locally with no subscriptions or API keys required.

> Built by a Distinguished TPM with 10 years of experience in 5G infrastructure, cloud platform delivery, and E2E program management.

---

## ✨ Features

| Feature | Details |
|---------|---------|
| **Multi-ATS scraping** | Greenhouse, Workday (wd1 + wd5 via Playwright), Lever, Ashby, Oracle HCM Fusion, iCIMS, SmartRecruiters, LinkedIn, Google Jobs, Amazon Jobs |
| **250+ target companies** | Pre-configured with correct ATS URLs for tech, telecom, AI, Boston-area, gaming, streaming |
| **LLM job scoring** | Local Ollama (llama3.1:8b) scores each job 1–10 against your profile and reasons why |
| **Real-time dashboard** | Flask + SSE streaming, live scrape progress, job preview pane, status tracking |
| **Location-aware sorting** | MA/Boston jobs surfaced first, then Remote/Hybrid, then other US |
| **Deduplication** | Auto-dedupes jobs stored from multiple sources (same posting on LinkedIn + ATS) |
| **Description scraping** | Fetches full job descriptions to give the LLM richer scoring context |
| **Resume tailoring** | LLM-assisted resume bullet generation keyed to specific job postings |
| **Liveness checks** | HEAD-request scan flags expired postings before you waste time applying |
| **Multi-client support** | `--client` flag swaps the entire profile/DB/targets for a different candidate |

---

## 🏗️ Architecture

```
run_full_pipeline.py          # Orchestrator: discovery → scrape → dedup → score
│
├── scraper.py                # LinkedIn, Google Jobs, Amazon, BuiltInBoston
├── custom_targets.py         # 250+ ATS scrapers (Greenhouse, Workday, Ashby, ...)
├── discovery_agent.py        # CrewAI agent: finds new company ATS URLs
├── scout_agent.py            # CrewAI agent: scores + ranks jobs
├── scorer.py                 # Standalone LLM scoring (used by pipeline + server)
├── describe.py               # Job description fetcher (requests → Playwright fallback)
├── dedup.py                  # Duplicate detection and removal
├── resume_agent.py           # Resume tailoring agent
├── market_agent.py           # Market trends and salary intel agent
├── profile.py                # Loads profile.yml — the stable API for all agents
│
├── server.py                 # Flask API + SSE streaming
├── dashboard.html            # Single-page dashboard UI
│
├── profile.yml               # YOUR profile (gitignored — copy from profile.yml.example)
├── profile.yml.example       # Template — fill this in and rename
├── custom_targets.json       # Company → ATS URL mapping (auto-maintained)
└── jobs.db                   # SQLite job database (gitignored)
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- [Ollama](https://ollama.ai) running locally with `llama3.1:8b` pulled
- Playwright browsers: `playwright install chromium`

### Setup

```bash
git clone https://github.com/devbotman/tpm-command-center.git
cd tpm-command-center
pip install -r requirements.txt
playwright install chromium

# Configure your profile
cp profile.yml.example profile.yml
# Edit profile.yml with your name, experience, target companies, salary range
```

### Run

```bash
# Full pipeline (discovery + scrape + dedup + score)
python run_full_pipeline.py --fast --score

# Start dashboard
python server.py
# → http://localhost:5050
```

### Usage flags

```bash
python run_full_pipeline.py --fast           # Skip discovery (use cached targets)
python run_full_pipeline.py --fast --score   # Also score new jobs after scraping
python run_full_pipeline.py --client clients/other_person/  # Multi-client mode

python scorer.py --limit 100                 # Score up to 100 unscored jobs
python describe.py --limit 50               # Fetch job descriptions for 50 jobs
python dedup.py --apply                     # Remove duplicate job entries
```

---

## 📊 Dashboard

The dashboard runs at `http://localhost:5050` and provides:

- **Applications tab** — full job table with location-priority sort (MA/Boston first), salary filter, work-type filter, inline status updates
- **Digest tab** — LLM-scored ranked list with reasoning, "strong match" highlighting
- **Resume tab** — paste a job description, get tailored resume bullets
- **Scout tab** — trigger job scoring, fetch descriptions, view top picks
- **Admin tab** — edit source files, run pipeline steps, manage targets

---

## 🎯 Supported ATS Platforms

| Platform | Method | Notes |
|----------|--------|-------|
| **Greenhouse** | JSON API | `boards.greenhouse.io/[org]/jobs` |
| **Workday wd1** | REST API | Standard CSRF extraction |
| **Workday wd5** | Playwright + REST | JS-rendered CSRF token via browser |
| **Lever** | JSON API | `jobs.lever.co/[org]` |
| **Ashby** | GraphQL API | `jobs.ashbyhq.com/api/non-user-graphql` |
| **Oracle HCM Fusion** | REST API | Nested `requisitionList` response |
| **iCIMS** | REST API | `/search/jobs` endpoint |
| **SmartRecruiters** | REST API | `/jobs/search` endpoint |
| **LinkedIn** | Playwright | Rate-limited, polite delays |
| **Google Jobs** | Playwright | `careers.google.com` |
| **Amazon Jobs** | REST API | `amazon.jobs/en/search` |
| **Generic** | Playwright | Fallback for any HTML career page |

---

## 🏢 Pre-configured Companies (250+)

**AI / ML:** Anthropic, OpenAI, Databricks, Scale AI, Cohere, Hugging Face, Perplexity, Cerebras, xAI, Together AI, Groq, Modal, Harvey, Weights & Biases, ElevenLabs, Anduril, Figure AI

**5G / Telecom:** Ericsson, Nokia, Mavenir, T-Mobile, AT&T, Verizon, CommScope, Juniper, Cisco, Qualcomm

**Cloud / Infra:** AWS, Google Cloud, Microsoft Azure, Cloudflare, Fastly, HashiCorp, MongoDB, Elastic, Confluent, Snowflake, Databricks, Palantir, CrowdStrike

**Boston / MA:** MathWorks, HubSpot, Klaviyo, DraftKings, Rapid7, Toast, Wayfair, Liberty Mutual, State Street, MassMutual, John Hancock, Fidelity, Biogen, Moderna, Vertex, Boston Dynamics, Boston Scientific, iRobot

**Gaming / Streaming:** Netflix, Spotify, Roku, Twitch, Discord, Reddit, Pinterest, Sony Interactive, Epic Games, Riot Games, Roblox, Unity

---

## ⚙️ Configuration

Edit `profile.yml` to customize:

```yaml
identity:
  name: Your Name
  title: Staff Technical Program Manager
  location: Boston, MA

job_search:
  salary_floor: 130000          # flag jobs below this
  salary_target_min: 150000     # scoring sweet spot
  salary_target_max: 200000

  target_titles:
    - technical program manager
    - staff tpm
    - associate director of program management
    # ... add more

  search_queries:
    - technical program manager
    - engineering program manager
```

The scoring LLM reads `SCORING_PROFILE` from `profile.py` and rates each job 1–10 with a one-line reason.

---

## 🔒 Privacy

- `profile.yml` is `.gitignore`d — your personal info never leaves your machine
- `jobs.db` is `.gitignore`d — your application history stays local
- All LLM inference runs locally via Ollama — no data sent to external APIs
- Scraping uses polite delays and standard browser user-agent strings

---

## 🛠️ Tech Stack

| Layer | Tech |
|-------|------|
| Language | Python 3.12 |
| Web scraping | `requests`, `playwright` (sync API) |
| AI agents | CrewAI + Ollama (`llama3.1:8b`) |
| Backend | Flask + Server-Sent Events |
| Frontend | Vanilla JS, single HTML file |
| Database | SQLite |
| Profile config | YAML (`profile.yml`) |

---

## 📁 Multi-Client Mode

Run the same codebase for multiple job seekers:

```bash
mkdir -p clients/sarah/
cp profile.yml.example clients/sarah/profile.yml
# Edit clients/sarah/profile.yml with Sarah's details

python run_full_pipeline.py --client clients/sarah --fast --score
python server.py --client clients/sarah
```

Each client gets their own `profile.yml`, `jobs.db`, and optionally `custom_targets.json`.

---

## 📄 License

MIT — use freely, attribution appreciated.

---

*Built with Python, Playwright, CrewAI, Ollama, and too much coffee.*

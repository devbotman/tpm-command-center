"""
run_full_pipeline.py — Run discovery + scrape in the correct order
===================================================================
1. Runs discovery_agent to populate custom_targets.json + expanded_titles.json
2. Then runs the main scraper (which now picks up the expanded targets + titles)

Usage:
    python run_full_pipeline.py              # full run
    python run_full_pipeline.py --skip-discovery   # scrape only (if discovery already ran)
    python run_full_pipeline.py --discovery-only   # discovery only, no scrape
"""

import argparse
import sys
import time
from pathlib import Path
from datetime import datetime

# Force UTF-8 on Windows (cp1252 default chokes on Unicode job titles/company names)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description="Full pipeline: discovery + scrape")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--fast",           action="store_true",
                        help="Skip discovery, parallel custom targets (~3-4 min)")
    parser.add_argument("--score",          action="store_true",
                        help="Auto-score new jobs with Ollama after scraping")
    parser.add_argument("--score-limit",    type=int, default=50,
                        help="Max jobs to score per run (default 50, used with --score)")
    parser.add_argument("--client",         default=None,
                        help="Client profile directory (e.g. clients/sarah_wk). "
                             "Sets PROFILE_PATH, DB_PATH, and TARGETS_FILE for that client.")
    args = parser.parse_args()

    t_start = time.time()

    base = Path(__file__).parent

    # Apply client overrides before any imports that read profile/DB paths
    if args.client:
        client_dir = base / args.client
        profile_path = client_dir / "profile.yml"
        if not profile_path.exists():
            print(f"ERROR: client profile not found at {profile_path}")
            return
        import os
        os.environ["PROFILE_PATH"] = str(profile_path)
        os.environ["DB_PATH"]      = str(client_dir / "jobs.db")
        os.environ["TARGETS_FILE"] = str(client_dir / "custom_targets.json")
        print(f"\n  CLIENT MODE: {args.client}")

    print(f"\n{'='*55}")
    print(f"  TPM Command Center -- Full Pipeline")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    if args.fast:
        print(f"  MODE: FAST (parallel scraping, no discovery)")
    print(f"{'='*55}\n")

    # Step 0: Fix known bad entries
    fix_custom_targets(base)

    # Step 1: Discovery (skip if --fast, --skip-discovery, or ran <6 days ago)
    skip_disc = args.skip_discovery or args.fast
    if not skip_disc:
        stamp = base / ".last_discovery"
        if stamp.exists():
            age = (time.time() - stamp.stat().st_mtime) / 86400
            if age < 6:
                print(f"  Step 1: Skipping discovery (ran {age:.1f}d ago, weekly cadence)\n")
                skip_disc = True

    if not skip_disc:
        print("  Step 1: Running Discovery Agent...\n")
        try:
            sys.path.insert(0, str(base))
            from discovery_agent import run_discovery
            results = run_discovery(dry_run=args.dry_run)
            print(f"\n  Discovery complete: {results.get('ma_companies_added',0)} new companies")
            print(f"  Expanded titles: {len(results.get('expanded_titles',[]))}")
            (base / ".last_discovery").write_text(datetime.now().isoformat())
        except Exception as e:
            print(f"  Discovery error: {e}")

        if args.discovery_only:
            print("\n  Done (discovery only).\n")
            return
        time.sleep(1)

    # Step 2: Main scraper (Google, Amazon, LinkedIn, etc.)
    print("\n  Step 2: Running Scraper...\n")
    try:
        sys.path.insert(0, str(base))
        from scraper import init_db, run_scrape
        init_db()
        run_scrape()
    except Exception as e:
        print(f"  Scraper error: {e}")

    # Step 3: Healer
    try:
        heal_failing_targets(base)
    except Exception as e:
        print(f"  Healer error: {e}")

    # Step 4: Deduplication (always runs — fast, no external deps)
    print("\n  Step 4: Deduplicating jobs...\n")
    try:
        from dedup import run_dedup
        run_dedup(apply=True, verbose=True)
    except Exception as e:
        print(f"  Dedup error: {e}")

    # Step 5: Auto-score (optional, requires Ollama running)
    if args.score:
        print("\n  Step 4: Scoring new jobs...\n")
        try:
            from scorer import score_new_jobs
            score_new_jobs(limit=args.score_limit, verbose=True)
        except Exception as e:
            print(f"  Scorer error: {e}")

    elapsed = time.time() - t_start
    print(f"\n{'='*55}")
    print(f"  Pipeline complete -- {datetime.now():%H:%M:%S}  ({elapsed/60:.1f} min)")
    print(f"{'='*55}\n")


def fix_custom_targets(base: Path):
    """
    Fix known bad career URLs in custom_targets.json.
    These get set wrong during initial discovery if the company site
    redirects to a generic page instead of their actual ATS.
    """
    import json

    targets_file = base / "custom_targets.json"
    if not targets_file.exists():
        return

    # Correct ATS URLs for companies whose generic career pages don't work
    FIXES = {
        "nvidia": {
            "career_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
            "platform": "workday",
        },
        "oracle": {
            "career_url": "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
            "platform": "oracle",
        },
        "datadog": {
            "career_url": "https://boards.greenhouse.io/datadoghq",
            "platform": "greenhouse",
        },
        "cloudflare": {
            "career_url": "https://boards.greenhouse.io/cloudflare",
            "platform": "greenhouse",
        },
        "stripe": {
            "career_url": "https://stripe.com/jobs/search",
            "platform": "generic",
        },
        "pagerduty": {
            "career_url": "https://boards.greenhouse.io/pagerduty",
            "platform": "greenhouse",
        },
        "twilio": {
            "career_url": "https://boards.greenhouse.io/twilio",
            "platform": "greenhouse",
        },
        "hubspot": {
            "career_url": "https://boards.greenhouse.io/hubspot",
            "platform": "greenhouse",
        },
        "akamai": {
            "career_url": "https://akamaicareers.inflightcloud.com/search",
            "platform": "generic",
        },
        "wayfair": {
            "career_url": "https://www.wayfair.com/careers/jobs",
            "platform": "generic",
        },
        "toast": {
            "career_url": "https://boards.greenhouse.io/toast",
            "platform": "greenhouse",
        },
        "bose": {
            "career_url": "https://boseallabout.wd1.myworkdayjobs.com/Bose_Careers",
            "platform": "workday",
        },
        "mathworks": {
            "career_url": "https://www.mathworks.com/company/jobs.html",
            "platform": "generic",
        },
        "rapid7": {
            "career_url": "https://boards.greenhouse.io/rapid7",
            "platform": "greenhouse",
        },
        "raytheon": {
            "career_url": "https://rtx.wd1.myworkdayjobs.com/RTX",
            "platform": "workday",
        },
        "dell": {
            "career_url": "https://dell.wd1.myworkdayjobs.com/External",
            "platform": "workday",
        },
        "dell/emc": {
            "career_url": "https://dell.wd1.myworkdayjobs.com/External",
            "platform": "workday",
        },
        "boston_scientific": {
            "career_url": "https://bostonscientific.wd1.myworkdayjobs.com/US",
            "platform": "workday",
        },
        "ptc": {
            "career_url": "https://ptc.wd3.myworkdayjobs.com/PTCCareers",
            "platform": "workday",
        },
        "draftkings": {
            "career_url": "https://boards.greenhouse.io/draftkings",
            "platform": "greenhouse",
        },
        "klaviyo": {
            "career_url": "https://boards.greenhouse.io/klaviyo",
            "platform": "greenhouse",
        },
        "sharkninja": {
            "career_url": "https://boards.greenhouse.io/sharkninja",
            "platform": "greenhouse",
        },
        # Learned from 2026-03-15 run
        "fidelity_investments": {
            "career_url": "https://jobs.fidelity.com/search-jobs",
            "platform": "generic",
        },
        "thermo_fisher_scientific": {
            "career_url": "https://jobs.thermofisher.com/global/en/search-results",
            "platform": "generic",
        },
        "athenahealth": {
            "career_url": "https://athenahealth.wd1.myworkdayjobs.com/External",
            "platform": "workday",
        },
        "biogen": {
            "career_url": "https://biogen.wd1.myworkdayjobs.com/Biogen_Careers",
            "platform": "workday",
        },
        "moderna": {
            "career_url": "https://modernatx.wd1.myworkdayjobs.com/Moderna_Careers",
            "platform": "workday",
        },
        "vertex_pharmaceuticals": {
            "career_url": "https://vrtx.wd5.myworkdayjobs.com/VertexCareers",
            "platform": "workday",
        },
        "analog_devices": {
            "career_url": "https://analogdevices.wd1.myworkdayjobs.com/ADI_Careers",
            "platform": "workday",
        },
        "cisco": {
            "career_url": "https://jobs.cisco.com/jobs/SearchJobs",
            "platform": "generic",
        },
        "cognex": {
            "career_url": "https://boards.greenhouse.io/cognex",
            "platform": "greenhouse",
        },
        "irobot": {
            "career_url": "https://irobot.wd5.myworkdayjobs.com/iRobot",
            "platform": "workday",
        },
        "state_street_corporation": {
            "career_url": "https://statestreet.wd1.myworkdayjobs.com/Global",
            "platform": "workday",
        },
        "mercury_systems": {
            "career_url": "https://mrcy.wd5.myworkdayjobs.com/Mercury_Careers",
            "platform": "workday",
        },
        "national_grid": {
            "career_url": "https://nationalgrid.wd3.myworkdayjobs.com/NGCareers",
            "platform": "workday",
        },
        "imprivata": {
            "career_url": "https://boards.greenhouse.io/imprivata",
            "platform": "greenhouse",
        },
        "pegasystems": {
            "career_url": "https://www.pega.com/about/careers/search-jobs",
            "platform": "generic",
        },
        "constant_contact": {
            "career_url": "https://boards.greenhouse.io/constantcontact",
            "platform": "greenhouse",
        },
        "recorded_future": {
            "career_url": "https://boards.greenhouse.io/recordedfuture",
            "platform": "greenhouse",
        },
        # Fixes from 2026-03-15 second run — bad cached URLs
        "rocket_software": {
            "career_url": "https://boards.greenhouse.io/rocketsoftware",
            "platform": "greenhouse",
        },
        "virtusa": {
            "career_url": "https://www.virtusa.com/careers",
            "platform": "generic",
        },
        "waters_corporation": {
            "career_url": "https://waters.wd5.myworkdayjobs.com/Waters_Careers",
            "platform": "workday",
        },
        "avid_technology": {
            "career_url": "https://boards.greenhouse.io/avid",
            "platform": "greenhouse",
        },
        "procter_&_gamble": {
            "career_url": "https://www.pgcareers.com/global/en/search-results",
            "platform": "generic",
        },
        "tjx_companies": {
            "career_url": "https://tjx.wd1.myworkdayjobs.com/TJX",
            "platform": "workday",
        },
        "carbon_black/vmware": {
            "career_url": "https://broadcom.wd1.myworkdayjobs.com/External_Career",
            "platform": "workday",
        },
        "logmein": {
            "career_url": "https://boards.greenhouse.io/goto",
            "platform": "greenhouse",
        },
        "progress_software": {
            "career_url": "https://progress.wd1.myworkdayjobs.com/Progress_Careers",
            "platform": "workday",
        },
        "cengage_learning": {
            "career_url": "https://cengage.wd1.myworkdayjobs.com/CengageCareers",
            "platform": "workday",
        },
        "snyk": {
            "career_url": "https://boards.greenhouse.io/snyk",
            "platform": "greenhouse",
        },
        "datarobot": {
            "career_url": "https://boards.greenhouse.io/datarobot",
            "platform": "greenhouse",
        },
        "cyberark": {
            "career_url": "https://cyberark.wd1.myworkdayjobs.com/CyberArk",
            "platform": "workday",
        },
        # Workday instances where the API path was wrong (422 errors) —
        # switch to their real Workday career site paths
        "bose": {
            "career_url": "https://boseallabout.wd1.myworkdayjobs.com/en-US/Bose_Careers",
            "platform": "workday",
        },
        "boston_scientific": {
            "career_url": "https://bostonscientific.wd1.myworkdayjobs.com/en-US/US",
            "platform": "workday",
        },
        "raytheon": {
            "career_url": "https://careers.rtx.com/global/en/search-results",
            "platform": "generic",
        },
        "biogen": {
            "career_url": "https://biogen.wd1.myworkdayjobs.com/en-US/Biogen_Careers",
            "platform": "workday",
        },
        "moderna": {
            "career_url": "https://modernatx.wd1.myworkdayjobs.com/en-US/M_Careers",
            "platform": "workday",
        },
        "vertex_pharmaceuticals": {
            "career_url": "https://vrtx.wd5.myworkdayjobs.com/en-US/VertexCareers",
            "platform": "workday",
        },
        "analog_devices": {
            "career_url": "https://analogdevices.wd1.myworkdayjobs.com/en-US/External",
            "platform": "workday",
        },
        "mercury_systems": {
            "career_url": "https://mrcy.wd5.myworkdayjobs.com/en-US/External",
            "platform": "workday",
        },
        "irobot": {
            "career_url": "https://irobot.wd5.myworkdayjobs.com/en-US/iRobot",
            "platform": "workday",
        },
        "national_grid": {
            "career_url": "https://nationalgrid.wd3.myworkdayjobs.com/en-US/External",
            "platform": "workday",
        },
        # ── Gaming companies ──
        "sony_interactive_entertainment": {
            "career_url": "https://boards.greenhouse.io/sonyinteractiveentertainmentglobal",
            "platform": "greenhouse",
        },
        "nintendo": {
            "career_url": "https://careers.nintendo.com/job-openings/",
            "platform": "generic",
        },
        "activision_blizzard": {
            "career_url": "https://careers.activisionblizzard.com/search-results",
            "platform": "generic",
        },
        "electronic_arts": {
            "career_url": "https://ea.gr8people.com/jobs",
            "platform": "generic",
        },
        "take-two_interactive": {
            "career_url": "https://boards.greenhouse.io/taketwointeractivesoftware",
            "platform": "greenhouse",
        },
        "riot_games": {
            "career_url": "https://www.riotgames.com/en/work-with-us#702702",
            "platform": "generic",
        },
        "epic_games": {
            "career_url": "https://boards.greenhouse.io/epicgames",
            "platform": "greenhouse",
        },
        "roblox": {
            "career_url": "https://careers.roblox.com/jobs",
            "platform": "generic",
        },
        "unity_technologies": {
            "career_url": "https://careers.unity.com/find-position",
            "platform": "generic",
        },
        "bungie": {
            "career_url": "https://boards.greenhouse.io/bungie",
            "platform": "greenhouse",
        },
        "ubisoft": {
            "career_url": "https://www.ubisoft.com/en-us/company/careers",
            "platform": "generic",
        },
        "niantic": {
            "career_url": "https://nianticlabs.com/careers",
            "platform": "generic",
        },
        "zynga": {
            "career_url": "https://boards.greenhouse.io/zynga",
            "platform": "greenhouse",
        },
        "valve": {
            "career_url": "https://www.valvesoftware.com/en/jobs",
            "platform": "generic",
        },
        # ── Telecom / 5G (Devon's core domain) ──
        "ericsson": {
            "career_url": "https://jobs.ericsson.com/search",
            "platform": "generic",
        },
        "nokia": {
            "career_url": "https://nokia.wd3.myworkdayjobs.com/Nokia_External_Career_Site",
            "platform": "workday",
        },
        "mavenir": {
            "career_url": "https://boards.greenhouse.io/mavenir",
            "platform": "greenhouse",
        },
        "t-mobile": {
            "career_url": "https://tmobile.wd5.myworkdayjobs.com/T-Mobile",
            "platform": "workday",
        },
        "att": {
            "career_url": "https://www.att.jobs/search-jobs",
            "platform": "generic",
        },
        "verizon": {
            "career_url": "https://mycareer.verizon.com/jobs/search",
            "platform": "generic",
        },
        "commscope": {
            "career_url": "https://commscope.wd5.myworkdayjobs.com/CommScope_Careers",
            "platform": "workday",
        },
        # ── Network / Edge / CDN ──
        "fastly": {
            "career_url": "https://boards.greenhouse.io/fastly",
            "platform": "greenhouse",
        },
        # ── Autonomous / Robotics / Defense ──
        "spacex": {
            "career_url": "https://boards.greenhouse.io/spacex",
            "platform": "greenhouse",
        },
        "waymo": {
            "career_url": "https://waymo.com/joinus/",
            "platform": "generic",
        },
        # ── AI labs (additions) ──
        "xai": {
            "career_url": "https://boards.greenhouse.io/xai",
            "platform": "greenhouse",
        },
        "together_ai": {
            "career_url": "https://boards.greenhouse.io/togetherai",
            "platform": "greenhouse",
        },
        "modal": {
            "career_url": "https://modal.com/careers",
            "platform": "generic",
        },
        "groq": {
            "career_url": "https://groq.com/careers/",
            "platform": "generic",
        },
        # ── AI companies ──
        "anthropic": {
            "career_url": "https://boards.greenhouse.io/anthropic",
            "platform": "greenhouse",
        },
        "openai": {
            "career_url": "https://boards.greenhouse.io/openai",
            "platform": "greenhouse",
        },
        "cohere": {
            "career_url": "https://boards.greenhouse.io/cohere",
            "platform": "greenhouse",
        },
        "hugging_face": {
            "career_url": "https://boards.greenhouse.io/huggingface",
            "platform": "greenhouse",
        },
        "databricks": {
            "career_url": "https://boards.greenhouse.io/databricks",
            "platform": "greenhouse",
        },
        "scale_ai": {
            "career_url": "https://boards.greenhouse.io/scaleai",
            "platform": "greenhouse",
        },
        "weights_&_biases": {
            "career_url": "https://boards.greenhouse.io/wandb",
            "platform": "greenhouse",
        },
        "runway": {
            "career_url": "https://boards.greenhouse.io/runwayml",
            "platform": "greenhouse",
        },
        "perplexity_ai": {
            "career_url": "https://boards.greenhouse.io/perplexityai",
            "platform": "greenhouse",
        },
        "cerebras_systems": {
            "career_url": "https://boards.greenhouse.io/cerebrassystems",
            "platform": "greenhouse",
        },
        "jasper_ai": {
            "career_url": "https://boards.greenhouse.io/jasper",
            "platform": "greenhouse",
        },
        "replit": {
            "career_url": "https://boards.greenhouse.io/replit",
            "platform": "greenhouse",
        },
        "character_ai": {
            "career_url": "https://boards.greenhouse.io/characterai",
            "platform": "greenhouse",
        },
        "anduril": {
            "career_url": "https://boards.greenhouse.io/andurilindustries",
            "platform": "greenhouse",
        },
        "shield_ai": {
            "career_url": "https://boards.greenhouse.io/shieldai",
            "platform": "greenhouse",
        },
        "figure_ai": {
            "career_url": "https://boards.greenhouse.io/figureai",
            "platform": "greenhouse",
        },
        "inflection_ai": {
            "career_url": "https://boards.greenhouse.io/inflectionai",
            "platform": "greenhouse",
        },
        "stability_ai": {
            "career_url": "https://boards.greenhouse.io/stabilityai",
            "platform": "greenhouse",
        },
        "mistral_ai": {
            "career_url": "https://mistral.ai/careers",
            "platform": "generic",
        },
        # ── Other fixes from recent runs ──
        "sap": {
            "career_url": "https://jobs.sap.com/search",
            "platform": "generic",
        },
        "f5_networks": {
            "career_url": "https://f5.recsolu.com/jobs",
            "platform": "generic",
        },
        "sailpoint": {
            "career_url": "https://sailpoint.wd1.myworkdayjobs.com/SailPoint",
            "platform": "workday",
        },
        "opentext": {
            "career_url": "https://opentext.wd3.myworkdayjobs.com/Opentext_Careers",
            "platform": "workday",
        },
        "parexel": {
            "career_url": "https://jobs.parexel.com/search-jobs",
            "platform": "generic",
        },
        # ── Waltham / Route 128 corridor companies ──
        "dynatrace": {
            "career_url": "https://careers.dynatrace.com/jobs/",
            "platform": "generic",
        },
        "thermo_fisher_scientific": {
            "career_url": "https://jobs.thermofisher.com/global/en/search-results",
            "platform": "generic",
        },
        "dassault_systemes": {
            "career_url": "https://www.3ds.com/careers/jobs",
            "platform": "generic",
        },
        "care.com": {
            "career_url": "https://boards.greenhouse.io/carecom",
            "platform": "greenhouse",
        },
        "perkinelmer": {
            "career_url": "https://perkinelmer.wd5.myworkdayjobs.com/PKI_Careers",
            "platform": "workday",
        },
        "zoominfo": {
            "career_url": "https://boards.greenhouse.io/zoominfo",
            "platform": "greenhouse",
        },
        "vistaprint": {
            "career_url": "https://careers.vista.com/search-jobs",
            "platform": "generic",
        },
        "netcracker": {
            "career_url": "https://www.netcracker.com/careers",
            "platform": "generic",
        },
        "boston_dynamics": {
            "career_url": "https://bostondynamics.wd1.myworkdayjobs.com/Boston_Dynamics",
            "platform": "workday",
        },
        "devoted_health": {
            "career_url": "https://boards.greenhouse.io/devotedhealth",
            "platform": "greenhouse",
        },
        "tripadvisor": {
            "career_url": "https://boards.greenhouse.io/tripadvisor",
            "platform": "greenhouse",
        },
        "tripAdvisor": {
            "career_url": "https://boards.greenhouse.io/tripadvisor",
            "platform": "greenhouse",
        },
        "intuit": {
            "career_url": "https://jobs.intuit.com/search-jobs",
            "platform": "generic",
        },
        "mitre": {
            "career_url": "https://careers.mitre.org/us/en/search-results",
            "platform": "generic",
        },
        "bmc_software": {
            "career_url": "https://bmc.wd1.myworkdayjobs.com/BMC_Careers",
            "platform": "workday",
        },
        "elsevier": {
            "career_url": "https://relx.wd3.myworkdayjobs.com/ElsevierJobs",
            "platform": "workday",
        },
        "alkermes": {
            "career_url": "https://alkermes.wd1.myworkdayjobs.com/Alkermes_Careers",
            "platform": "workday",
        },
        "infosys": {
            "career_url": "https://career.infosys.com/joblist",
            "platform": "generic",
        },
        "acquia": {
            "career_url": "https://www.acquia.com/careers/open-positions",
            "platform": "generic",
        },
        # ── Red Hat / IBM Open Source ──
        "red_hat": {
            "career_url": "https://redhat.wd5.myworkdayjobs.com/Jobs",
            "platform": "workday",
        },
        # ── National TPM employers (remote-friendly) ──
        "salesforce": {
            "career_url": "https://salesforce.wd12.myworkdayjobs.com/Careers",
            "platform": "workday",
        },
        "servicenow": {
            "career_url": "https://jobs.smartrecruiters.com/ServiceNow",
            "platform": "smartrecruiters",
        },
        "workday": {
            "career_url": "https://workday.wd5.myworkdayjobs.com/Workday",
            "platform": "workday",
        },
        "palantir": {
            "career_url": "https://boards.greenhouse.io/palantir",
            "platform": "greenhouse",
        },
        "crowdstrike": {
            "career_url": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers",
            "platform": "workday",
        },
        "okta": {
            "career_url": "https://okta.wd1.myworkdayjobs.com/OktaCareers",
            "platform": "workday",
        },
        "palo_alto_networks": {
            "career_url": "https://jobs.paloaltonetworks.com/en-US/search",
            "platform": "generic",
        },
        "splunk": {
            "career_url": "https://careers.cisco.com/global/en/splunk",
            "platform": "generic",
        },
        "hashicorp": {
            "career_url": "https://boards.greenhouse.io/hashicorp",
            "platform": "greenhouse",
        },
        "mongodb": {
            "career_url": "https://boards.greenhouse.io/mongodb",
            "platform": "greenhouse",
        },
        "elastic": {
            "career_url": "https://jobs.elastic.co",
            "platform": "generic",
        },
        "confluent": {
            "career_url": "https://boards.greenhouse.io/confluent",
            "platform": "greenhouse",
        },
        "snowflake": {
            "career_url": "https://snowflake.wd5.myworkdayjobs.com/en-US/Snowflake_Careers",
            "platform": "workday",
        },
        "twilio": {
            "career_url": "https://boards.greenhouse.io/twilio",
            "platform": "greenhouse",
        },
        "zendesk": {
            "career_url": "https://jobs.zendesk.com/us/en/search-results",
            "platform": "generic",
        },
        "docusign": {
            "career_url": "https://boards.greenhouse.io/docusign",
            "platform": "greenhouse",
        },
        "veeva_systems": {
            "career_url": "https://boards.greenhouse.io/veevasystems",
            "platform": "greenhouse",
        },
        "hubspot": {
            "career_url": "https://boards.greenhouse.io/hubspot",
            "platform": "greenhouse",
        },
        "qualtrics": {
            "career_url": "https://boards.greenhouse.io/qualtrics",
            "platform": "greenhouse",
        },
        "coupa_software": {
            "career_url": "https://boards.greenhouse.io/coupacommerce",
            "platform": "greenhouse",
        },
        "medallia": {
            "career_url": "https://boards.greenhouse.io/medallia",
            "platform": "greenhouse",
        },
        "sprinklr": {
            "career_url": "https://boards.greenhouse.io/sprinklr",
            "platform": "greenhouse",
        },
        "cloudera": {
            "career_url": "https://cloudera.wd5.myworkdayjobs.com/External_Career",
            "platform": "workday",
        },
        "nutanix": {
            "career_url": "https://nutanix.eightfold.ai/careers",
            "platform": "generic",
        },
        "pure_storage": {
            "career_url": "https://boards.greenhouse.io/purestorage",
            "platform": "greenhouse",
        },
        "rubrik": {
            "career_url": "https://boards.greenhouse.io/rubrik",
            "platform": "greenhouse",
        },
        "cohesity": {
            "career_url": "https://boards.greenhouse.io/cohesity",
            "platform": "greenhouse",
        },
        "veeam": {
            "career_url": "https://careers.veeam.com/vacancies",
            "platform": "generic",
        },
        "commscope": {
            "career_url": "https://commscope.wd5.myworkdayjobs.com/CommScope_Careers",
            "platform": "workday",
        },
        "juniper_networks": {
            "career_url": "https://juniper.wd1.myworkdayjobs.com/Juniper",
            "platform": "workday",
        },
        "fortinet": {
            "career_url": "https://fortinet.wd3.myworkdayjobs.com/fortinet",
            "platform": "workday",
        },
        "qualcomm": {
            "career_url": "https://qualcomm.wd5.myworkdayjobs.com/External",
            "platform": "workday",
        },
        "broadcom": {
            "career_url": "https://broadcom.wd1.myworkdayjobs.com/External_Career",
            "platform": "workday",
        },
        "marvell_technology": {
            "career_url": "https://marvell.wd1.myworkdayjobs.com/MarvellCareers",
            "platform": "workday",
        },
        "vmware": {
            "career_url": "https://broadcom.wd1.myworkdayjobs.com/External_Career",
            "platform": "workday",
        },
        "twitch": {
            "career_url": "https://boards.greenhouse.io/twitch",
            "platform": "greenhouse",
        },
        # ── Streaming platforms ──
        "netflix": {
            "career_url": "https://jobs.netflix.com/search",
            "platform": "generic",
        },
        "spotify": {
            "career_url": "https://jobs.lever.co/spotify",
            "platform": "lever",
        },
        "hulu": {
            "career_url": "https://boards.greenhouse.io/hulu",
            "platform": "greenhouse",
        },
        "roku": {
            "career_url": "https://boards.greenhouse.io/roku",
            "platform": "greenhouse",
        },
        "peacock": {
            "career_url": "https://nbcunicareers.com/search",
            "platform": "generic",
        },
        "disney_streaming": {
            "career_url": "https://jobs.disneycareers.com/search-jobs",
            "platform": "generic",
        },
        "paramount": {
            "career_url": "https://careers.paramount.com/search",
            "platform": "generic",
        },
        "discovery_plus": {
            "career_url": "https://careers.wbd.com/global/en/search-results",
            "platform": "generic",
        },
        "reddit": {
            "career_url": "https://boards.greenhouse.io/reddit",
            "platform": "greenhouse",
        },
        "discord": {
            "career_url": "https://boards.greenhouse.io/discord",
            "platform": "greenhouse",
        },
        "pinterest": {
            "career_url": "https://boards.greenhouse.io/pinterest",
            "platform": "greenhouse",
        },
        "snap": {
            "career_url": "https://snap.wd1.myworkdayjobs.com/snap",
            "platform": "workday",
        },
        "tiktok": {
            "career_url": "https://careers.tiktok.com/position",
            "platform": "generic",
        },
        # ── Gaming studios ──
        "bethesda_softworks": {
            "career_url": "https://bethesda.net/en/article/careers",
            "platform": "generic",
        },
        "zenimax_media": {
            "career_url": "https://jobs.zenimax.com",
            "platform": "generic",
        },
        "2k_games": {
            "career_url": "https://2k.com/en-US/careers/",
            "platform": "generic",
        },
        "naughty_dog": {
            "career_url": "https://boards.greenhouse.io/naughtydog",
            "platform": "greenhouse",
        },
        "insomniac_games": {
            "career_url": "https://boards.greenhouse.io/insomniacgames",
            "platform": "greenhouse",
        },
        "respawn_entertainment": {
            "career_url": "https://www.respawn.com/careers",
            "platform": "generic",
        },
        "bioware": {
            "career_url": "https://ea.gr8people.com/jobs",
            "platform": "generic",
        },
        "rockstar_games": {
            "career_url": "https://www.rockstargames.com/careers",
            "platform": "generic",
        },
        "343_industries": {
            "career_url": "https://careers.microsoft.com/v2/global/en/search.html",
            "platform": "generic",
        },
        "obsidian_entertainment": {
            "career_url": "https://www.obsidian.net/careers",
            "platform": "generic",
        },
        "double_fine_productions": {
            "career_url": "https://boards.greenhouse.io/doublefine",
            "platform": "greenhouse",
        },
        "cd_projekt_red": {
            "career_url": "https://jobs.cdprojektred.com",
            "platform": "generic",
        },
        "square_enix": {
            "career_url": "https://careers.square-enix-games.com/en",
            "platform": "generic",
        },
        "bandai_namco": {
            "career_url": "https://www.bandainamcous.com/careers",
            "platform": "generic",
        },
        "wbgames": {
            "career_url": "https://careers.wbd.com/global/en/search-results",
            "platform": "generic",
        },
        "blizzard_entertainment": {
            "career_url": "https://careers.blizzard.com/en-us/openings",
            "platform": "generic",
        },
        "supercell": {
            "career_url": "https://supercell.com/en/careers/",
            "platform": "generic",
        },
        "king": {
            "career_url": "https://boards.greenhouse.io/king",
            "platform": "greenhouse",
        },
        # ── Boston-area additions ──
        "getronics": {
            "career_url": "https://www.getronics.com/careers/",
            "platform": "generic",
        },
        "carbonite": {
            "career_url": "https://www.carbonite.com/about/careers",
            "platform": "generic",
        },
        "rapid7": {
            "career_url": "https://www.rapid7.com/company/careers/",
            "platform": "generic",
        },
        "toast_inc": {
            "career_url": "https://boards.greenhouse.io/toastinc",
            "platform": "greenhouse",
        },
        "chewy": {
            "career_url": "https://boards.greenhouse.io/chewy",
            "platform": "greenhouse",
        },
        "localiq": {
            "career_url": "https://usa.gannett.com/careers/",
            "platform": "generic",
        },
        "brightcove": {
            "career_url": "https://boards.greenhouse.io/brightcove",
            "platform": "greenhouse",
        },
        "turbine": {
            "career_url": "https://www.turbine.com/careers",
            "platform": "generic",
        },
        "globalink": {
            "career_url": "https://boards.greenhouse.io/globalink",
            "platform": "greenhouse",
        },
        "brightcove": {
            "career_url": "https://boards.greenhouse.io/brightcove",
            "platform": "greenhouse",
        },
        "drizly": {
            "career_url": "https://boards.greenhouse.io/drizly",
            "platform": "greenhouse",
        },
        "teikametrics": {
            "career_url": "https://boards.greenhouse.io/teikametrics",
            "platform": "greenhouse",
        },
        "localytics": {
            "career_url": "https://boards.greenhouse.io/localytics",
            "platform": "greenhouse",
        },
        "ziprecruiter": {
            "career_url": "https://boards.greenhouse.io/ziprecruiter",
            "platform": "greenhouse",
        },
        "draftkings_boston": {
            "career_url": "https://boards.greenhouse.io/draftkings",
            "platform": "greenhouse",
        },
        "wellframe": {
            "career_url": "https://boards.greenhouse.io/wellframe",
            "platform": "greenhouse",
        },
        "flywire": {
            "career_url": "https://boards.greenhouse.io/flywire",
            "platform": "greenhouse",
        },
        "kyruus": {
            "career_url": "https://boards.greenhouse.io/kyruushealth",
            "platform": "greenhouse",
        },
        "turbonomic": {
            "career_url": "https://turbonomic.com/company/careers/",
            "platform": "generic",
        },
        "optum": {
            "career_url": "https://careers.unitedhealthgroup.com/search-jobs",
            "platform": "generic",
        },
        "john_hancock": {
            "career_url": "https://johnhancock.wd1.myworkdayjobs.com/JHCareers",
            "platform": "workday",
        },
        "liberty_mutual": {
            "career_url": "https://libertymutual.wd5.myworkdayjobs.com/LMI_Careers",
            "platform": "workday",
        },
        "travelers": {
            "career_url": "https://travelers.wd5.myworkdayjobs.com/External",
            "platform": "workday",
        },
        "putnam_investments": {
            "career_url": "https://boards.greenhouse.io/putnam",
            "platform": "greenhouse",
        },
        "wellington_management": {
            "career_url": "https://wellingtoncareers.wd1.myworkdayjobs.com/Wellington_Careers",
            "platform": "workday",
        },
        "gmr_marketing": {
            "career_url": "https://boards.greenhouse.io/gmrmarketing",
            "platform": "greenhouse",
        },
    }

    # Entries to remove — dead targets that consistently return 0 and waste time
    REMOVE_KEYS = [
        # Acquired/defunct companies
        "isight_partners_(now_part_of_fireye)",
        "isight_partners",
        "endeca_(now_part_of_oracle,_but_still_has_significant_presence_in_ma)",
        "endeca_technologies",
        "rapidminer_(now_part_of_altair)",
        "nuance_communications_(now_part_of_microsoft,_but_still_has_significant_presence_in_ma)",
        "nuance_communications",
        "analog_devices_(also_has_offices_in_wilmington_and_waltham)",
        "akumin_(formerly_known_as_medical_imaging_solutions)",
        "openview_venture_partners_(investment_firm_with_offices_in_boston)",
        "cognex_corporation",
        "nuance/microsoft",
        "carbon_black",
        "carbon_black_(now_part_of_vmware)",
        "alight_solutions",
        "mellanox_technologies",      # acquired by Nvidia
        "ptc_(parametric_technology_corporation)",  # dupe of ptc

        # Fake / non-existent companies from LLM hallucinations
        "akamai_(cambridge)",
        "akamai_technologies",
        "akouba",
        "akrios",
        "iflyte",
        "iflytek",
        "digital_reasoning",
        "kaspersky_lab",

        # Greenhouse 404s — migrated off platform, consistently fail
                        "sharkninja",
                        "imprivata",
                                
        # Workday 422s — API rejects all body formats, Playwright finds nothing
                                                                                                        
        # Career page not found / consistently 0 results
        "carbon_black/vmware",
        "nuance/microsoft",
        "cogent_communications",
        "harvard_pilgrim_health_care",
        "siemens_plm_software",
        "thoughtworks",
        "virtusa",
        "akumin",
        "zerto",
    ]

    try:
        targets = json.loads(targets_file.read_text())
        fixed = 0
        removed = 0

        # Fix bad career URLs
        for key, fix in FIXES.items():
            if key in targets:
                current_url = targets[key].get("career_url", "")
                correct_url = fix["career_url"]
                if current_url != correct_url:
                    targets[key]["career_url"] = correct_url
                    targets[key]["platform"] = fix["platform"]
                    targets[key]["platform_cfg"] = None  # force re-detection
                    fixed += 1

        # Remove known-bad entries (acquired companies, fake companies, spam)
        for key in REMOVE_KEYS:
            if key in targets:
                del targets[key]
                removed += 1

        # Also remove any entries whose career_url is a known spam/parked domain
        spam_domains = ["silverwhitebirds.co", "hugedomains.com", "parking", "sedo.com"]
        for key in list(targets.keys()):
            url = targets[key].get("career_url", "")
            if url and any(spam in url.lower() for spam in spam_domains):
                del targets[key]
                removed += 1

        # Also ADD any companies from FIXES that aren't in targets yet
        # This ensures all known companies get scraped without waiting for LLM discovery
        added = 0
        for key, fix in FIXES.items():
            if key not in targets:
                # Build a clean name from the key
                name = key.replace("_", " ").replace("/", " / ").title()
                # Fix common capitalizations
                name = name.replace("Ai", "AI").replace("Llc", "LLC").replace("Inc", "Inc.")
                targets[key] = {
                    "name": name,
                    "career_url": fix["career_url"],
                    "platform": fix["platform"],
                    "platform_cfg": None,
                    "last_scraped": None,
                    "job_count": 0,
                    "added": datetime.now().strftime("%Y-%m-%d"),
                    "discovery": {"source": "run_full_pipeline:FIXES"},
                }
                added += 1

        if fixed or removed or added:
            targets_file.write_text(json.dumps(targets, indent=2))
            parts = []
            if fixed: parts.append(f"{fixed} career URLs fixed")
            if added: parts.append(f"{added} new companies added")
            if removed: parts.append(f"{removed} bad entries removed")
            print(f"  {' + '.join(parts)} in custom_targets.json")
    except Exception as e:
        print(f"  Warning: could not fix targets: {e}")


def heal_failing_targets(base: Path):
    """
    Run target_healer.py if it exists, skipping Greenhouse targets
    (those work via Playwright fallback) and recently-scraped targets.
    Non-fatal — errors are printed but don't stop the pipeline.
    """
    import json
    import sqlite3

    healer = base / "target_healer.py"
    if not healer.exists():
        return

    targets_file = base / "custom_targets.json"
    if not targets_file.exists():
        return

    try:
        targets = json.loads(targets_file.read_text())
    except Exception:
        return

    # Build list of genuinely failing targets (not Greenhouse, not recently scraped,
    # not already in DB with jobs)
    db_counts = {}
    db_path = base / "jobs.db"
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            rows = con.execute(
                "SELECT company, COUNT(*) FROM jobs GROUP BY company"
            ).fetchall()
            con.close()
            db_counts = {r[0].lower(): r[1] for r in rows}
        except Exception:
            pass

    failing = []
    for key, t in targets.items():
        name = t.get("name", key)
        platform = t.get("platform", "")
        if platform == "greenhouse":
            continue  # Playwright fallback handles these
        if db_counts.get(name.lower(), 0) > 0:
            continue  # Already has jobs in DB
        if t.get("last_scraped") and t.get("job_count", 0) == 0:
            failing.append(name)

    if not failing:
        print("  Step 3: No failing targets to heal")
        return

    print(f"  Step 3: Found {len(failing)} targets with 0 jobs after scraping")

    # Check for dead URLs (quick HEAD requests, no Playwright)
    import urllib.request
    dead = []
    for name in failing[:20]:  # cap at 20 to keep it fast
        key = name.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
        t = targets.get(key, {})
        url = t.get("career_url", "")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            dead.append(name)

    if dead:
        print(f"  Dead URLs found: {', '.join(dead[:5])}")
    else:
        print(f"  No dead URLs found in failing targets -- failures are likely JS/auth issues")


if __name__ == "__main__":
    main()

"""
scraper_agent.py — Self-Healing Scraper Agent
==============================================
Runs scraper.py, detects errors, feeds them to the local LLM,
applies the fix, and reruns. Loops until clean or max attempts hit.

Usage:
    python scraper_agent.py              # auto-fix and run
    python scraper_agent.py --dry-run    # show proposed fix, don't apply
    python scraper_agent.py --max 5      # max fix attempts (default 3)

The agent:
  1. Runs scraper.py and captures all output
  2. Parses errors per-company (e.g. "[Amazon] Unhandled error: ...")
  3. Feeds the error + the relevant function code to llama3.1:8b
  4. Applies the fix to scraper.py (with backup)
  5. Reruns — repeat until 0 errors or max attempts reached
  6. Reports final job counts to jobs.db

Install: pip install ollama
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import ollama
except ImportError:
    print("[!] Run: pip install ollama")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SCRAPER_PATH = Path(__file__).parent / "scraper.py"
BACKUP_DIR   = Path(__file__).parent / "scraper_backups"
MODEL        = "llama3.1:8b"
MAX_ATTEMPTS = 3

BACKUP_DIR.mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str, level: str = "info"):
    icons = {"info": "·", "ok": "✓", "warn": "⚠", "error": "✗", "fix": "⚡", "run": "▶"}
    print(f"  [{timestamp()}] {icons.get(level,'·')} {msg}")


def backup_scraper(attempt: int):
    """Save a timestamped backup before applying any fix."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"scraper_attempt{attempt}_{ts}.py"
    shutil.copy(SCRAPER_PATH, dest)
    return dest


def run_scraper(timeout: int = 120) -> tuple[str, str, int]:
    """
    Run scraper.py once (no schedule loop).
    Returns (stdout, stderr, returncode).
    We inject --once flag handled below via env var.
    """
    env = os.environ.copy()
    env["SCRAPER_ONCE"] = "1"  # signals scraper to run once and exit
    result = subprocess.run(
        [sys.executable, str(SCRAPER_PATH)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def parse_errors(stdout: str, stderr: str) -> list[dict]:
    """
    Extract structured errors from scraper output.
    Returns list of {company, error, line} dicts.
    """
    errors = []
    combined = stdout + "\n" + stderr

    # Match patterns like:
    #   [Amazon] Unhandled error: ...
    #   [Amazon] Error: ...
    #   [Amazon] ... Error: ...
    error_patterns = [
        r'\[(\w+)\]\s+(?:Unhandled error|Error):\s+(.+)',
        r'\[(\w+)\]\s+\w+ error:\s+(.+)',
        r'Traceback.*?(?:Error|Exception):\s+(.+)',
    ]
    seen = set()
    for pat in error_patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE | re.DOTALL):
            groups = m.groups()
            if len(groups) == 2:
                company, error = groups[0].strip(), groups[1].strip()[:300]
            else:
                company, error = "Unknown", groups[0].strip()[:300]
            key = f"{company}:{error[:60]}"
            if key not in seen:
                seen.add(key)
                errors.append({"company": company, "error": error})

    # Also catch Python tracebacks from stderr
    if "Traceback" in stderr:
        tb_lines = stderr.strip().split("\n")
        last_error = tb_lines[-1] if tb_lines else "Unknown error"
        if not any(e["error"] == last_error for e in errors):
            errors.append({"company": "scraper", "error": last_error})

    return errors


def extract_function(code: str, company: str) -> str:
    """Extract the scrape_<company> function from scraper.py source."""
    company_lower = company.lower().replace("yc", "yc").replace("netapp", "netapp")
    fn_name = f"scrape_{company_lower}"

    # Find function start
    start = code.find(f"def {fn_name}(")
    if start == -1:
        # Try partial match
        for line_start in [f"def scrape_{company_lower[:4]}"]:
            start = code.find(line_start)
            if start != -1:
                break
    if start == -1:
        return f"# Function scrape_{company_lower} not found"

    # Find next function def at same indent level
    rest = code[start:]
    lines = rest.split("\n")
    fn_lines = [lines[0]]
    for line in lines[1:]:
        if line.startswith("def ") and fn_lines:
            break
        fn_lines.append(line)

    return "\n".join(fn_lines)


def ask_llm_to_fix(scraper_code: str, error: dict) -> str:
    """
    Ask the local LLM to fix the broken scraper function.
    Returns the fixed function code only.
    """
    company  = error["company"]
    err_msg  = error["error"]
    fn_code  = extract_function(scraper_code, company)

    prompt = f"""You are an expert Python developer fixing a web scraper.

The scraper for {company} is failing with this error:
{err_msg}

Here is the broken function:
```python
{fn_code}
```

Fix the function so it no longer produces this error. Common causes:
- API returned a dict/list where a string was expected — use isinstance() checks
- JSON structure changed — add .get() with fallbacks  
- Missing field — add default values
- Type error in SQLite binding — ensure all values are str/int/None

Rules:
- Return ONLY the complete fixed Python function, nothing else
- No explanations, no markdown fences, no comments about what changed
- Keep the same function name and signature
- The function must return a list of job dicts
- All dict values must be str, int, or None — never dict or list

Fixed function:"""

    response = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1},  # low temp for code fixes
    )
    return response["message"]["content"].strip()


def extract_code_from_response(response: str) -> str:
    """Strip markdown fences if LLM added them anyway."""
    # Remove ```python ... ``` or ``` ... ```
    response = re.sub(r'^```(?:python)?\n?', '', response, flags=re.MULTILINE)
    response = re.sub(r'\n?```$', '', response, flags=re.MULTILINE)
    return response.strip()


def apply_fix(scraper_code: str, company: str, fixed_fn: str) -> str:
    """Replace the old function with the fixed one in scraper source."""
    company_lower = company.lower()
    fn_name = f"scrape_{company_lower}"

    start = scraper_code.find(f"def {fn_name}(")
    if start == -1:
        log(f"Could not find {fn_name} to replace", "warn")
        return scraper_code

    # Find end of function (next top-level def or end of file)
    rest  = scraper_code[start:]
    lines = rest.split("\n")
    end_idx = len(lines)
    for i, line in enumerate(lines[1:], 1):
        if line.startswith("def ") or line.startswith("# ──"):
            end_idx = i
            break

    old_fn = "\n".join(lines[:end_idx])
    return scraper_code.replace(old_fn, fixed_fn, 1)


def parse_job_counts(stdout: str) -> dict:
    """Extract per-company job counts from scraper output."""
    counts = {}
    for m in re.finditer(r'\[[\d:]+\]\s+(\w+)\s+→\s+(\d+) new jobs', stdout):
        counts[m.group(1)] = int(m.group(2))
    total_m = re.search(r'Done\.\s+(\d+) new jobs added', stdout)
    counts["TOTAL"] = int(total_m.group(1)) if total_m else 0
    return counts


# ── Main agent loop ───────────────────────────────────────────────────────────

def run_agent(max_attempts: int = MAX_ATTEMPTS, dry_run: bool = False):
    print(f"\n{'═'*55}")
    print(f"  Scraper Self-Healing Agent")
    print(f"  Model: {MODEL}  |  Max attempts: {max_attempts}")
    print(f"{'═'*55}\n")

    # Check Ollama is running
    try:
        ollama.list()
    except Exception:
        print("[!] Ollama not running — open the Ollama desktop app first.\n")
        sys.exit(1)

    if not SCRAPER_PATH.exists():
        print(f"[!] scraper.py not found at {SCRAPER_PATH}\n")
        sys.exit(1)

    attempt = 0
    while attempt <= max_attempts:
        attempt += 1
        log(f"Run {attempt}/{max_attempts+1} — executing scraper.py", "run")

        try:
            stdout, stderr, returncode = run_scraper(timeout=180)
        except subprocess.TimeoutExpired:
            log("Scraper timed out after 3 minutes", "warn")
            stdout, stderr = "", "TimeoutExpired"

        # Print scraper output
        for line in stdout.strip().split("\n"):
            if line.strip():
                print(f"    {line}")

        # Parse results
        counts = parse_job_counts(stdout)
        errors = parse_errors(stdout, stderr)

        if counts.get("TOTAL", 0) > 0:
            log(f"{counts['TOTAL']} new jobs saved — checking for remaining errors", "ok")

        if not errors:
            log("No errors detected — scraper is clean!", "ok")
            print(f"\n  Final counts: {counts}\n")
            break

        log(f"Found {len(errors)} error(s): {[e['company'] for e in errors]}", "warn")

        if attempt > max_attempts:
            log(f"Max attempts ({max_attempts}) reached — stopping", "warn")
            log("Remaining errors need manual review", "warn")
            for e in errors:
                print(f"    [{e['company']}] {e['error']}")
            break

        # Fix each error
        scraper_code = SCRAPER_PATH.read_text(encoding="utf-8")

        for error in errors:
            company = error["company"]
            err_msg = error["error"]
            log(f"Asking LLM to fix [{company}]: {err_msg[:80]}...", "fix")

            try:
                fixed_response = ask_llm_to_fix(scraper_code, error)
                fixed_fn       = extract_code_from_response(fixed_response)

                if "def scrape_" not in fixed_fn:
                    log(f"LLM response didn't contain a valid function for {company} — skipping", "warn")
                    continue

                if dry_run:
                    log(f"DRY RUN — proposed fix for {company}:", "fix")
                    print("\n" + "─"*50)
                    print(fixed_fn[:800])
                    print("─"*50 + "\n")
                    continue

                # Backup before touching the file
                backup_path = backup_scraper(attempt)
                log(f"Backed up scraper to {backup_path.name}", "info")

                # Apply fix
                new_code = apply_fix(scraper_code, company, fixed_fn)
                if new_code == scraper_code:
                    log(f"Fix produced no change for {company} — skipping", "warn")
                    continue

                SCRAPER_PATH.write_text(new_code, encoding="utf-8")
                scraper_code = new_code  # update for next error in same loop
                log(f"Fix applied for [{company}]", "ok")

            except Exception as e:
                log(f"LLM fix failed for {company}: {e}", "error")

        if dry_run:
            log("Dry run complete — no changes written", "info")
            break

        log("Fixes applied — waiting 2s before rerun", "info")
        time.sleep(2)

    print(f"\n{'═'*55}")
    print(f"  Agent finished after {attempt} run(s)")
    print(f"{'═'*55}\n")


# ── Patch scraper.py to support SCRAPER_ONCE env var ─────────────────────────

def patch_scraper_for_once_mode():
    """
    Adds SCRAPER_ONCE support to scraper.py so the agent
    can run it once without the infinite schedule loop.
    Already patched if the env check is present.
    """
    code = SCRAPER_PATH.read_text(encoding="utf-8")
    if "SCRAPER_ONCE" in code:
        return  # already patched

    old_main = '''if __name__ == "__main__":
    init_db()
    print("TPM Job Scraper starting up...")
    print("Targets: Google, Amazon, Microsoft, Meta, Apple, NetApp, YC startups")
    print("Titles:  Technical Program Manager / Project Manager / TPM / Senior TPM")
    print(f"Schedule: daily at 08:00  |  DB: {DB_PATH}\\n")

    # Run once immediately on startup
    run_scrape()

    # Then schedule daily at 8am
    schedule.every().day.at("08:00").do(run_scrape)
    while True:
        schedule.run_pending()
        time.sleep(60)'''

    new_main = '''if __name__ == "__main__":
    import os as _os
    init_db()
    print("TPM Job Scraper starting up...")
    print("Targets: Google, Amazon, Microsoft, Meta, Apple, NetApp, YC startups")
    print("Titles:  Technical Program Manager / Project Manager / TPM / Senior TPM")

    run_scrape()

    # Skip schedule loop when called by the self-healing agent
    if _os.environ.get("SCRAPER_ONCE") == "1":
        sys.exit(0)

    print(f"Schedule: daily at 08:00  |  DB: {DB_PATH}\\n")
    schedule.every().day.at("08:00").do(run_scrape)
    while True:
        schedule.run_pending()
        time.sleep(60)'''

    if old_main in code:
        patched = code.replace(old_main, new_main)
        SCRAPER_PATH.write_text(patched, encoding="utf-8")
        print("  [·] Patched scraper.py with SCRAPER_ONCE support")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Self-healing scraper agent")
    parser.add_argument("--max",     type=int,  default=3,     help="Max fix attempts (default 3)")
    parser.add_argument("--dry-run", action="store_true",      help="Show fixes without applying")
    parser.add_argument("--model",   type=str,  default=MODEL, help="Ollama model to use")
    args = parser.parse_args()

    MODEL = args.model
    patch_scraper_for_once_mode()
    run_agent(max_attempts=args.max, dry_run=args.dry_run)

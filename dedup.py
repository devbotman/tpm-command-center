"""
dedup.py — Job deduplication
=============================
Finds and removes duplicate jobs in jobs.db.

Two jobs are considered duplicates if they share the same
(company, title) case-insensitively and both have status='new'.
When merging, the kept row is the one with:
  - the best source (custom: ATS > linkedin > other)
  - the highest score (if scored)
  - the fullest description

Usage:
    python dedup.py              # dry run — shows what would be removed
    python dedup.py --apply      # actually delete duplicates
    python dedup.py --apply --verbose
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobs.db"

# Source priority — higher = preferred (keep this row, delete others)
_SOURCE_PRIORITY = {
    "custom:greenhouse": 10,
    "greenhouse_api": 10,
    "custom:boards.greenhouse.io": 10,
    "custom:ashby": 10,
    "custom:lever": 10,
    "amazon.jobs": 9,
    "careers.google.com": 9,
    "custom:workday": 8,
    "custom:oracle": 8,
    "custom:smartrecruiters": 8,
    "linkedin.com/top-picks": 5,
    "builtinboston.com": 4,
    "linkedin.com": 3,
}


def _source_score(source: str) -> int:
    return _SOURCE_PRIORITY.get(source or "", 1)


def get_db():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def find_duplicates(con: sqlite3.Connection) -> list[dict]:
    """
    Return list of duplicate groups.
    Each group: {'keep': row_id, 'delete': [row_id, ...], 'company': str, 'title': str}
    """
    rows = con.execute("""
        SELECT id, company, title, source, score, description, url, found_date
        FROM jobs
        WHERE status = 'new'
        ORDER BY company, title
    """).fetchall()

    # Group by (lower(company), lower(title))
    groups: dict[tuple, list] = {}
    for row in rows:
        key = (row["company"].lower().strip(), row["title"].lower().strip())
        groups.setdefault(key, []).append(dict(row))

    duplicates = []
    for key, group in groups.items():
        if len(group) <= 1:
            continue

        # Rank rows: prefer ATS source > score > description length > recency
        def rank(r):
            return (
                _source_score(r.get("source", "")),
                r.get("score") or 0,
                len(r.get("description") or ""),
                r.get("found_date") or "",
            )

        group.sort(key=rank, reverse=True)
        keep = group[0]
        delete_ids = [r["id"] for r in group[1:]]

        duplicates.append({
            "company": keep["company"],
            "title":   keep["title"],
            "keep_id": keep["id"],
            "keep_source": keep.get("source", ""),
            "delete_ids": delete_ids,
            "count": len(group),
        })

    return duplicates


def run_dedup(apply: bool = False, verbose: bool = True) -> dict:
    """
    Find and optionally remove duplicate jobs.

    Args:
        apply:   If True, delete duplicates from DB. If False, dry run only.
        verbose: Print results.

    Returns:
        {'groups': int, 'removed': int, 'dry_run': bool}
    """
    con = get_db()
    duplicates = find_duplicates(con)

    total_removable = sum(len(d["delete_ids"]) for d in duplicates)

    if verbose:
        print(f"  Dedup: found {len(duplicates)} duplicate groups ({total_removable} redundant rows)")
        for d in duplicates[:10]:
            print(f"    [{d['count']}x] {d['company']} — {d['title'][:50]}")
            print(f"         keep={d['keep_id']} ({d['keep_source']}), remove={d['delete_ids']}")

    removed = 0
    if apply and duplicates:
        for d in duplicates:
            for del_id in d["delete_ids"]:
                con.execute("DELETE FROM jobs WHERE id=?", (del_id,))
                removed += 1
        con.commit()
        if verbose:
            print(f"  Dedup: removed {removed} duplicate rows")
    elif not apply and verbose:
        print(f"  Dedup: dry run — use --apply to delete {total_removable} rows")

    con.close()
    return {"groups": len(duplicates), "removed": removed, "dry_run": not apply}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Deduplicate jobs in jobs.db")
    parser.add_argument("--apply",   action="store_true", help="Actually delete duplicates (default: dry run)")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()
    run_dedup(apply=args.apply, verbose=args.verbose)

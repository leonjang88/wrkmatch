"""Nightly ATS scan: parallel discovery honoring cached platform/slug, then run_scan.

Run from the repo root (cron does): .venv/bin/python scripts/nightly_scan.py
DB path override via WRKMATCH_DB env var.
"""
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from wrkmatch.db import init_db, get_companies_with_contacts
from wrkmatch.scan import discover_company_jobs, run_scan

DB = os.environ.get("WRKMATCH_DB", str(REPO_ROOT / "data" / "wrkmatch.db"))
MAX_WORKERS = int(os.environ.get("WRKMATCH_SCAN_WORKERS", "16"))


def main() -> None:
    t0 = time.time()
    conn = init_db(DB)
    companies = [r["company"] for r in get_companies_with_contacts(conn)]
    cached = {}
    for c in companies:
        row = conn.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE name = ?", (c,)
        ).fetchone()
        cached[c] = (row["ats_platform"], row["ats_slug"]) if row else (None, None)
    n_cached = sum(1 for p, s in cached.values() if p and s)
    print(f"[{time.strftime('%F %T')}] scanning {len(companies)} companies ({n_cached} cached ATS)", flush=True)

    results = {}
    done = 0
    lock = threading.Lock()

    def work(c):
        p, s = cached[c]
        return c, discover_company_jobs(c, cached_platform=p, cached_slug=s)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(work, c) for c in companies]
        for fut in as_completed(futs):
            c, res = fut.result()
            with lock:
                results[c] = res
                done += 1
                if done % 100 == 0:
                    print(f"  probed {done}/{len(companies)} at {time.time()-t0:.0f}s", flush=True)

    stats = run_scan(
        conn,
        companies=companies,
        pm_only=True,
        discover=lambda company, cached_platform=None, cached_slug=None: results[company],
    )
    print(f"[{time.strftime('%F %T')}] done in {time.time()-t0:.0f}s: {stats}", flush=True)


if __name__ == "__main__":
    main()

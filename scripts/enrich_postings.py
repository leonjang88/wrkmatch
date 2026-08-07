"""Backfill enrichment (salary, remote, native ATS detail) for open postings
that haven't been enriched yet.

Run from the repo root: .venv/bin/python scripts/enrich_postings.py
DB path override via WRKMATCH_DB env var.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from wrkmatch.db import init_db
from wrkmatch.enrich import enrich_posting

DB = os.environ.get("WRKMATCH_DB", str(REPO_ROOT / "data" / "wrkmatch.db"))
MAX_WORKERS = int(os.environ.get("WRKMATCH_ENRICH_WORKERS", "8"))


def _enrich_one(posting_id: int) -> bool:
    # Each worker opens its own connection -- sqlite3 connections aren't
    # thread-safe to share, and init_db is cheap/idempotent to call per task
    # (same pattern app/server.py uses per-request).
    conn = init_db(DB)
    try:
        row = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
        if row is None:
            return False
        return enrich_posting(conn, row)
    finally:
        conn.close()


def main() -> None:
    t0 = time.time()
    conn = init_db(DB)
    rows = conn.execute(
        "SELECT id FROM postings WHERE enriched_at IS NULL AND status = 'open'"
    ).fetchall()
    posting_ids = [r["id"] for r in rows]
    conn.close()

    print(f"[{time.strftime('%F %T')}] enriching {len(posting_ids)} postings", flush=True)

    ok = 0
    failed = 0
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_enrich_one, pid): pid for pid in posting_ids}
        for fut in as_completed(futs):
            done += 1
            try:
                if fut.result():
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            if done % 50 == 0:
                print(f"  enriched {done}/{len(posting_ids)} at {time.time()-t0:.0f}s", flush=True)

    print(
        f"[{time.strftime('%F %T')}] done in {time.time()-t0:.0f}s: "
        f"{ok} enriched, {failed} failed/skipped (will retry next run if network-related)",
        flush=True,
    )


if __name__ == "__main__":
    main()

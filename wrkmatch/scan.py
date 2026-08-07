from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from .ats_clients import ATS_FUNCS, Job
from .db import (
    finish_scan,
    get_companies_with_contacts,
    mark_postings_status,
    record_posting,
    start_scan,
    upsert_company,
)
from .filters import filter_pm_jobs
from .normalize import slug_candidates

DiscoverFunc = Callable[..., Tuple[Optional[str], Optional[str], List[Job]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _func_for_platform(platform: str):
    for func in ATS_FUNCS:
        if func.__name__ == f"{platform}_jobs":
            return func
    return None


def discover_company_jobs(
    company: str,
    cached_platform: Optional[str] = None,
    cached_slug: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], List[Job]]:
    """Resolve a company's ATS platform/slug and fetch its current job list.

    With both cached_platform and cached_slug given, calls only the matching
    ATS client with cached_slug directly -- no slug guessing, no fallback to
    full discovery on a miss. Otherwise probes slug_candidates(company)
    against ATS_FUNCS, first hit wins.
    """
    if cached_platform is not None and cached_slug is not None:
        func = _func_for_platform(cached_platform)
        if func is None:
            return (cached_platform, cached_slug, [])
        try:
            jobs = func(cached_slug)
        except Exception:
            jobs = []
        return (cached_platform, cached_slug, jobs)

    for cand in slug_candidates(company):
        for func in ATS_FUNCS:
            try:
                jobs = func(cand)
            except Exception:
                jobs = []
            if jobs:
                return (jobs[0].source, cand, jobs)
    return (None, None, [])


def run_scan(
    conn: sqlite3.Connection,
    companies: Optional[List[str]] = None,
    pm_only: bool = True,
    discover: Optional[DiscoverFunc] = None,
) -> dict:
    """Incremental scan orchestrator. See wrkmatch/scan.py's test module
    docstring (tests/test_scan.py) for the full contract.
    """
    if discover is None:
        discover = discover_company_jobs

    if companies is None:
        companies = [row["company"] for row in get_companies_with_contacts(conn)]

    scan_id = start_scan(conn)
    companies_scanned = 0
    postings_found = 0
    new_postings = 0
    marked_gone = 0

    for company in companies:
        cached_row = conn.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE name = ?", (company,)
        ).fetchone()
        cached_platform = cached_row["ats_platform"] if cached_row else None
        cached_slug = cached_row["ats_slug"] if cached_row else None

        platform, slug, raw_jobs = discover(
            company, cached_platform=cached_platform, cached_slug=cached_slug
        )

        jobs = filter_pm_jobs(raw_jobs) if pm_only else raw_jobs

        company_id = upsert_company(
            conn, company, ats_platform=platform, ats_slug=slug, last_scanned=_now()
        )

        result_urls = set()
        for job in jobs:
            existing = conn.execute(
                "SELECT id FROM postings WHERE url = ?", (job.url,)
            ).fetchone()
            record_posting(
                conn,
                company_id,
                title=job.title,
                url=job.url,
                location=job.location,
                ats_platform=platform,
                department=job.department,
                posted_at=job.posted_at,
            )
            result_urls.add(job.url)
            if existing is None:
                new_postings += 1
        postings_found += len(jobs)

        if raw_jobs:
            open_rows = conn.execute(
                "SELECT url FROM postings WHERE company_id = ? AND status = 'open'",
                (company_id,),
            ).fetchall()
            gone_urls = [r["url"] for r in open_rows if r["url"] not in result_urls]
            if gone_urls:
                marked_gone += mark_postings_status(conn, gone_urls, "gone")

        if result_urls:
            mark_postings_status(conn, list(result_urls), "open")

        companies_scanned += 1

    finish_scan(conn, scan_id, companies_scanned, postings_found, new_postings)

    return {
        "scan_id": scan_id,
        "companies_scanned": companies_scanned,
        "postings_found": postings_found,
        "new_postings": new_postings,
        "marked_gone": marked_gone,
    }

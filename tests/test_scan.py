"""Interface contract for a NOT-YET-IMPLEMENTED module wrkmatch/scan.py.

wrkmatch/scan.py does not exist yet; these tests fail on import (ImportError)
until it's implemented. That's expected. The contract they define:

    discover_company_jobs(company, cached_platform=None, cached_slug=None)
        -> tuple[str | None, str | None, list[Job]]

        With cached_platform AND cached_slug both given: calls ONLY the one
        ATS client function matching cached_platform (by its Job.source name,
        e.g. "greenhouse" -> greenhouse_jobs), passed cached_slug directly.
        No slug guessing, no other ATS clients probed. Returns
        (cached_platform, cached_slug, jobs) whatever that one call yields --
        including (cached_platform, cached_slug, []) when it yields nothing.
        There is NO fallback to full discovery on a cache miss; a stale/dead
        cached slug just produces an empty result this scan.

        Without a cache (cached_platform/cached_slug are None, or only one of
        the two given): probes wrkmatch.normalize.slug_candidates(company)
        against wrkmatch.ats_clients.ATS_FUNCS, the same "first hit wins"
        logic as fetch.py's try_company. Nothing found across every
        candidate/client combination -> (None, None, []). The returned
        platform string is whatever the winning client's Job.source is.

    run_scan(conn, companies=None, pm_only=True, discover=None) -> dict
        The incremental scan orchestrator. `discover` defaults to
        discover_company_jobs but is injectable so tests never hit the
        network -- fakes here return canned (platform, slug, jobs) tuples.

        Company list resolution:
          - companies=None -> scan wrkmatch.db.get_companies_with_contacts(conn),
            using each row's (already-normalized) 'company' string.
          - an explicit `companies` list overrides the db lookup entirely and
            is passed through to `discover` verbatim (no normalization --
            normalization only happens on the companies=None path, because
            get_companies_with_contacts already normalizes for you).

        Per company, in order:
          1. Look up the company's existing companies-table row (if any) to
             get its previously-cached ats_platform/ats_slug, and call
             discover(company, cached_platform=<that or None>, cached_slug=<that or None>).
             A company scanned for the very first time has no row yet, so it
             gets called with cached_platform=None, cached_slug=None.
          2. If pm_only: filter_pm_jobs(jobs) before anything else below.
             If not pm_only: use the discover jobs as-is.
          3. upsert_company(conn, company, ats_platform=<discovered>,
             ats_slug=<discovered>, last_scanned=<now>) -- always runs, even
             when discover found nothing, so last_scanned still advances.
          4. record_posting(conn, company_id, ...) for each (filtered) job.
          5. Gone-marking (pinned precisely -- this is the trap in this
             module): a previously-'open' posting belonging to this company
             is marked 'gone' if and only if:
               (a) discover's RAW job list for this company (BEFORE pm_only
                   filtering) is non-empty -- proof the board actually
                   responded this scan, as opposed to a network failure
                   (ATS clients return [] on any error) which must leave
                   existing postings untouched rather than wiping them out, and
               (b) the posting's url does not appear in the (filtered, if
                   pm_only) result set used for record_posting in step 4.
             Postings already 'closed' are never touched by this rule -- only
             'open' ones are candidates for 'gone'.
          6. Reappearance: any url present in this scan's (filtered) results
             is explicitly marked 'open' via mark_postings_status, regardless
             of its prior status (open, closed, or gone). A board relisting a
             previously-closed/gone posting makes it live again.

        Scan history: start_scan(conn) at the beginning; finish_scan(conn, ...)
        at the end with companies_scanned (count of companies processed),
        postings_found (total filtered jobs recorded across all companies,
        this scan only -- not a cumulative db count), and new_postings (how
        many of those were brand-new urls, not previously in postings).

        Returns:
            {"scan_id": int, "companies_scanned": int, "postings_found": int,
             "new_postings": int, "marked_gone": int}

        companies=[] (empty, explicit) -> a scan row is still recorded, with
        all counters at 0.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import pytest
from requests_mock import ANY as ANY_URL

from wrkmatch.ats_clients import Job
from wrkmatch.db import (
    get_open_postings,
    mark_postings_status,
    upsert_contacts,
)
from wrkmatch.normalize import normalize_company_name
from wrkmatch.scan import discover_company_jobs, run_scan


# --- shared helpers ------------------------------------------------------------

def make_contact(
    first_name="Jane",
    last_name="Doe",
    company="Acme Widgets",
    position="Product Manager",
    connected_on="01 Jan 2023",
):
    return {
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "position": position,
        "connected_on": connected_on,
    }


def make_job(
    url,
    title="Product Manager",
    source="greenhouse",
    company="Acme Widgets",
    location="Remote",
    department="Product",
    posted_at=None,
):
    return Job(
        company=company,
        source=source,
        title=title,
        location=location,
        department=department,
        url=url,
        posted_at=posted_at,
    )


def make_fake_discover(responses: dict):
    """Build a discover_company_jobs-shaped callable backed by a dict of
    company -> (platform, slug, jobs). Unknown companies yield (None, None, []).
    Every call (including cached_platform/cached_slug it was invoked with) is
    recorded on the returned callable's `.calls` list for inspection.
    """
    calls = []

    def _discover(company, cached_platform=None, cached_slug=None):
        calls.append({
            "company": company,
            "cached_platform": cached_platform,
            "cached_slug": cached_slug,
        })
        return responses.get(company, (None, None, []))

    _discover.calls = calls
    return _discover


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def posting_status(conn: sqlite3.Connection, url: str) -> str:
    return conn.execute("SELECT status FROM postings WHERE url = ?", (url,)).fetchone()[0]


def assert_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, f"{value!r} is not timezone-aware"
    return dt


# --- discover_company_jobs: cached path -----------------------------------------

def test_discover_company_jobs_cached_path_calls_only_that_one_client(requests_mock):
    # cached_slug is deliberately unrelated to slug_candidates("Acme Widgets"),
    # so a correct cached call only succeeds if cached_slug is used directly
    # (not re-derived from the company name).
    requests_mock.get(
        "https://api.lever.co/v0/postings/unusual-cached-slug",
        json=[{
            "text": "Senior Product Manager",
            "categories": {"team": "Product", "location": "Remote"},
            "hostedUrl": "https://jobs.lever.co/acme/abc123",
            "createdAt": 1700000000000,
        }],
        headers={"Content-Type": "application/json"},
    )

    platform, slug, jobs = discover_company_jobs(
        "Acme Widgets", cached_platform="lever", cached_slug="unusual-cached-slug"
    )

    assert platform == "lever"
    assert slug == "unusual-cached-slug"
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Product Manager"
    assert jobs[0].url == "https://jobs.lever.co/acme/abc123"
    # Exactly one HTTP call: no discovery probing across other candidates/clients.
    assert requests_mock.call_count == 1


def test_discover_company_jobs_cached_path_miss_returns_empty_no_fallback(requests_mock):
    requests_mock.get(
        "https://api.lever.co/v0/postings/unusual-cached-slug",
        json=[],
        headers={"Content-Type": "application/json"},
    )

    result = discover_company_jobs(
        "Acme Widgets", cached_platform="lever", cached_slug="unusual-cached-slug"
    )

    assert result == ("lever", "unusual-cached-slug", [])
    # A miss on the cached client does not trigger discovery over other clients.
    assert requests_mock.call_count == 1


# --- discover_company_jobs: discovery path (no cache) ---------------------------

def test_discover_company_jobs_nothing_found_returns_none_none_empty(requests_mock):
    requests_mock.get(ANY_URL, status_code=404)
    requests_mock.post(ANY_URL, status_code=404)

    result = discover_company_jobs("Totally Unknown Company")

    assert result == (None, None, [])


def test_discover_company_jobs_discovers_via_probing(requests_mock):
    # Every candidate/client combination 404s except greenhouse @ "acme" --
    # regardless of slug_candidates' internal set-iteration order, that is
    # the only combination that can possibly succeed.
    requests_mock.get(ANY_URL, status_code=404)
    requests_mock.post(ANY_URL, status_code=404)
    requests_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        json={"jobs": [{
            "title": "Product Manager",
            "offices": [{"name": "Acme Inc"}],
            "location": {"name": "Remote"},
            "departments": [{"name": "Product"}],
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "updated_at": "2024-01-01T00:00:00Z",
        }]},
        headers={"Content-Type": "application/json"},
    )

    platform, slug, jobs = discover_company_jobs("Acme")

    assert platform == "greenhouse"
    assert slug == "acme"
    assert len(jobs) == 1
    assert jobs[0].url == "https://boards.greenhouse.io/acme/jobs/1"


# --- run_scan: company list resolution -------------------------------------------

def test_run_scan_none_companies_uses_get_companies_with_contacts(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    discover = make_fake_discover({key: ("greenhouse", "acme", [job])})

    result = run_scan(db_conn, discover=discover, pm_only=False)

    assert result["companies_scanned"] == 1
    assert discover.calls[0]["company"] == key


def test_run_scan_explicit_companies_list_overrides_db_lookup(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Ignored Co")], source="leon")
    job = make_job(url="https://boards.greenhouse.io/explicit/jobs/1")
    discover = make_fake_discover({"Explicit Co": ("greenhouse", "explicit", [job])})

    result = run_scan(db_conn, companies=["Explicit Co"], discover=discover, pm_only=False)

    assert result["companies_scanned"] == 1
    assert len(discover.calls) == 1
    # Explicit list items pass through verbatim -- not normalized.
    assert discover.calls[0]["company"] == "Explicit Co"
    assert all(c["company"] != "Ignored Co" for c in discover.calls)
    names = {row[0] for row in db_conn.execute("SELECT name FROM companies").fetchall()}
    assert "Ignored Co" not in names
    assert normalize_company_name("Ignored Co") not in names


def test_run_scan_empty_companies_list_returns_zeros_but_records_scan_row(db_conn):
    result = run_scan(db_conn, companies=[], discover=make_fake_discover({}))

    assert result["companies_scanned"] == 0
    assert result["postings_found"] == 0
    assert result["new_postings"] == 0
    assert result["marked_gone"] == 0
    assert isinstance(result["scan_id"], int)

    row = db_conn.execute(
        "SELECT companies_scanned, postings_found, new_postings, finished_at FROM scans WHERE id = ?",
        (result["scan_id"],),
    ).fetchone()
    assert tuple(row[:3]) == (0, 0, 0)
    assert row[3] is not None


def test_run_scan_return_dict_has_expected_keys(db_conn):
    result = run_scan(db_conn, companies=[], discover=make_fake_discover({}))
    assert set(result.keys()) == {
        "scan_id", "companies_scanned", "postings_found", "new_postings", "marked_gone",
    }


# --- run_scan: per-company processing --------------------------------------------

def test_run_scan_upserts_company_platform_slug_and_last_scanned(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    discover = make_fake_discover({key: ("greenhouse", "acme-slug", [job])})

    run_scan(db_conn, discover=discover, pm_only=False)

    row = db_conn.execute(
        "SELECT ats_platform, ats_slug, last_scanned FROM companies WHERE name = ?", (key,)
    ).fetchone()
    assert row is not None
    assert tuple(row[:2]) == ("greenhouse", "acme-slug")
    assert_iso_utc(row[2])


def test_run_scan_pm_only_true_filters_non_pm_jobs(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    pm_job = make_job(url="https://boards.greenhouse.io/acme/jobs/1", title="Product Manager")
    eng_job = make_job(url="https://boards.greenhouse.io/acme/jobs/2", title="Software Engineer")
    discover = make_fake_discover({key: ("greenhouse", "acme", [pm_job, eng_job])})

    result = run_scan(db_conn, discover=discover, pm_only=True)

    assert result["postings_found"] == 1
    urls = {p["url"] for p in get_open_postings(db_conn)}
    assert urls == {pm_job.url}


def test_run_scan_pm_only_false_keeps_all_jobs(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    pm_job = make_job(url="https://boards.greenhouse.io/acme/jobs/1", title="Product Manager")
    eng_job = make_job(url="https://boards.greenhouse.io/acme/jobs/2", title="Software Engineer")
    discover = make_fake_discover({key: ("greenhouse", "acme", [pm_job, eng_job])})

    result = run_scan(db_conn, discover=discover, pm_only=False)

    assert result["postings_found"] == 2
    urls = {p["url"] for p in get_open_postings(db_conn)}
    assert urls == {pm_job.url, eng_job.url}


def test_run_scan_records_scan_history_row(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    discover = make_fake_discover({key: ("greenhouse", "acme", [job])})

    result = run_scan(db_conn, discover=discover, pm_only=False)

    row = db_conn.execute(
        "SELECT companies_scanned, postings_found, new_postings, finished_at FROM scans WHERE id = ?",
        (result["scan_id"],),
    ).fetchone()
    assert tuple(row[:3]) == (result["companies_scanned"], result["postings_found"], result["new_postings"])
    assert tuple(row[:3]) == (1, 1, 1)
    assert row[3] is not None


# --- run_scan: incremental semantics ----------------------------------------------

def test_run_scan_second_run_same_job_preserves_first_seen_no_new_postings(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    discover = make_fake_discover({key: ("greenhouse", "acme", [job])})

    r1 = run_scan(db_conn, discover=discover, pm_only=False)
    first_seen_1 = db_conn.execute(
        "SELECT first_seen FROM postings WHERE url = ?", (job.url,)
    ).fetchone()[0]

    time.sleep(0.02)
    r2 = run_scan(db_conn, discover=discover, pm_only=False)
    row = db_conn.execute(
        "SELECT first_seen, last_seen FROM postings WHERE url = ?", (job.url,)
    ).fetchone()

    assert r1["new_postings"] == 1
    assert r2["new_postings"] == 0
    assert row[0] == first_seen_1
    assert row[1] >= first_seen_1
    assert count_rows(db_conn, "postings") == 1


def test_run_scan_passes_stored_platform_slug_as_cache_on_next_run(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    discover = make_fake_discover({key: ("greenhouse", "acme-slug", [job])})

    run_scan(db_conn, discover=discover, pm_only=False)
    run_scan(db_conn, discover=discover, pm_only=False)

    assert len(discover.calls) == 2
    assert discover.calls[0]["cached_platform"] is None
    assert discover.calls[0]["cached_slug"] is None
    assert discover.calls[1]["cached_platform"] == "greenhouse"
    assert discover.calls[1]["cached_slug"] == "acme-slug"


def test_run_scan_new_postings_and_postings_found_counts_across_companies(db_conn):
    upsert_contacts(
        db_conn,
        [
            make_contact(company="Acme Widgets", last_name="A"),
            make_contact(company="Beta Systems", last_name="B"),
        ],
        source="leon",
    )
    key_a = normalize_company_name("Acme Widgets")
    key_b = normalize_company_name("Beta Systems")
    job_a1 = make_job(url="https://boards.greenhouse.io/acme/jobs/a1")
    job_b1 = make_job(url="https://jobs.lever.co/beta/b1")

    r1 = run_scan(
        db_conn,
        discover=make_fake_discover({
            key_a: ("greenhouse", "acme", [job_a1]),
            key_b: ("lever", "beta", [job_b1]),
        }),
        pm_only=False,
    )
    assert r1["companies_scanned"] == 2
    assert r1["new_postings"] == 2
    assert r1["postings_found"] == 2

    job_b2 = make_job(url="https://jobs.lever.co/beta/b2")
    r2 = run_scan(
        db_conn,
        discover=make_fake_discover({
            key_a: ("greenhouse", "acme", [job_a1]),
            key_b: ("lever", "beta", [job_b1, job_b2]),
        }),
        pm_only=False,
    )
    assert r2["new_postings"] == 1
    assert r2["postings_found"] == 3


# --- run_scan: gone-marking rule (pinned precisely) -------------------------------

def test_run_scan_marks_disappeared_posting_gone_when_board_responds(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job1 = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    job2 = make_job(url="https://boards.greenhouse.io/acme/jobs/2")

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1, job2])}),
        pm_only=False,
    )

    result = run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1])}),
        pm_only=False,
    )

    assert posting_status(db_conn, job1.url) == "open"
    assert posting_status(db_conn, job2.url) == "gone"
    assert result["marked_gone"] == 1


def test_run_scan_leaves_postings_untouched_when_discover_returns_nothing(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job1 = make_job(url="https://boards.greenhouse.io/acme/jobs/1")

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1])}),
        pm_only=False,
    )

    # Simulates a transient failure: the cached client returned [] this scan.
    result = run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [])}),
        pm_only=False,
    )

    assert posting_status(db_conn, job1.url) == "open"
    assert result["marked_gone"] == 0


def test_run_scan_does_not_mark_gone_for_already_closed_postings(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job1 = make_job(url="https://boards.greenhouse.io/acme/jobs/1")

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1])}),
        pm_only=False,
    )
    mark_postings_status(db_conn, [job1.url], "closed")

    job2 = make_job(url="https://boards.greenhouse.io/acme/jobs/2")
    result = run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job2])}),
        pm_only=False,
    )

    # Only 'open' postings are candidates for 'gone'; already-'closed' stays put.
    assert posting_status(db_conn, job1.url) == "closed"
    assert result["marked_gone"] == 0


def test_run_scan_resurrects_gone_posting_when_url_reappears(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    job1 = make_job(url="https://boards.greenhouse.io/acme/jobs/1")
    job2 = make_job(url="https://boards.greenhouse.io/acme/jobs/2")

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1, job2])}),
        pm_only=False,
    )
    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1])}),
        pm_only=False,
    )
    assert posting_status(db_conn, job2.url) == "gone"

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [job1, job2])}),
        pm_only=False,
    )
    assert posting_status(db_conn, job2.url) == "open"


def test_run_scan_gone_marking_uses_raw_discover_count_not_filtered_count(db_conn):
    """A board that responds with only non-PM jobs still counts as 'responded'
    for the gone-marking gate (pm_only filtering happens AFTER that gate is
    evaluated). A previously-open PM posting whose url is absent from the
    now-empty (post-filter) result set gets marked gone, even though the raw
    discover result was non-empty.
    """
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    key = normalize_company_name("Acme Widgets")
    pm_job = make_job(url="https://boards.greenhouse.io/acme/jobs/pm-1", title="Product Manager")

    run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [pm_job])}),
        pm_only=True,
    )

    eng_job = make_job(url="https://boards.greenhouse.io/acme/jobs/eng-1", title="Software Engineer")
    result = run_scan(
        db_conn,
        discover=make_fake_discover({key: ("greenhouse", "acme", [eng_job])}),
        pm_only=True,
    )

    assert posting_status(db_conn, pm_job.url) == "gone"
    assert result["marked_gone"] == 1
    assert result["postings_found"] == 0

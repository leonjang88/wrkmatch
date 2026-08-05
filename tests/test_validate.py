"""Interface contract for a NOT-YET-IMPLEMENTED module wrkmatch/validate.py.

wrkmatch/validate.py does not exist yet; these tests fail on import (ImportError)
until it's implemented. That's expected. The contract they define:

    CLOSED_SIGNALS: dict[str, list[str]]
        Maps a source name to a list of lowercase closed-signal phrases plus a
        'generic' key that applies regardless of source:

            {
                "generic": [
                    "no longer accepting applications",
                    "this job is no longer available",
                    "position has been filled",
                    "this position is closed",
                    "job not found",
                    "posting not found",
                    "this role has been filled",
                    "no longer active",
                ],
                "greenhouse": ["the job you are looking for is no longer open"],
                "lever": ["this posting is no longer accepting applications"],
                "workday": ["this job is no longer available"],
            }

    check_posting(url, source=None, timeout=6) -> str
        Fetches `url` via requests (following redirects) and returns one of
        'open', 'closed', 'gone':

        - HTTP 404 or 410 -> 'gone' (regardless of body content).
        - HTTP 200: body is searched, case-insensitively, for any phrase in
          the applicable signal set. `source=None` checks the union of every
          list in CLOSED_SIGNALS (generic + every platform). `source="X"`
          checks only CLOSED_SIGNALS["generic"] + CLOSED_SIGNALS.get("X", [])
          -- phrases pinned under other platforms are NOT considered.
          A match -> 'closed'; no match -> 'open'.
        - Connection error, timeout, or any 5xx status -> 'open'. These are
          transient-failure signals and must NOT be interpreted as closed
          (benefit of the doubt).
        - 3xx redirects are followed by requests itself; the final response
          (its status and body) is what gets evaluated by the rules above.

    validate_postings(conn, limit=None) -> dict
        Pulls open postings via wrkmatch.db.get_open_postings(conn) (optionally
        capped to the first `limit`), calls check_posting(posting["url"],
        source=posting.get("ats_platform")) for each, and for any posting whose
        result is 'closed' or 'gone', calls wrkmatch.db.mark_postings_status
        with the matching urls and that status. Returns:

            {"checked": N, "closed": N, "gone": N, "still_open": N}

        An empty db (no open postings) returns all-zero counts and makes no
        HTTP requests at all.
"""
from __future__ import annotations

import requests
import pytest

from wrkmatch.db import get_open_postings, record_posting, upsert_company
from wrkmatch.validate import CLOSED_SIGNALS, check_posting, validate_postings


def _seed_posting(conn, url, ats_platform=None, company="Acme Widgets", title="Product Manager"):
    company_id = upsert_company(conn, company)
    return record_posting(conn, company_id, title, url, ats_platform=ats_platform)


# --- check_posting: gone -----------------------------------------------------

def test_check_posting_404_returns_gone(requests_mock):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    requests_mock.get(url, status_code=404)
    assert check_posting(url) == "gone"


def test_check_posting_410_returns_gone(requests_mock):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    requests_mock.get(url, status_code=410)
    assert check_posting(url) == "gone"


# --- check_posting: closed / open on 200 -------------------------------------

def test_check_posting_200_with_generic_closed_phrase_returns_closed(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(url, status_code=200, text="Sorry, this position has been filled.")
    assert check_posting(url) == "closed"


def test_check_posting_200_without_closed_signal_returns_open(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(
        url, status_code=200,
        text="Product Manager - Acme Widgets. Apply now to join our team!",
    )
    assert check_posting(url) == "open"


def test_check_posting_case_insensitive_matching(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(url, status_code=200, text="THIS POSITION IS CLOSED.")
    assert check_posting(url) == "closed"


# --- check_posting: transient failures default to open -----------------------

def test_check_posting_connection_error_returns_open(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(url, exc=requests.ConnectionError)
    assert check_posting(url) == "open"


def test_check_posting_timeout_returns_open(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(url, exc=requests.Timeout)
    assert check_posting(url) == "open"


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_check_posting_5xx_returns_open(requests_mock, status_code):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(url, status_code=status_code, text="this position is closed")
    assert check_posting(url) == "open"


# --- check_posting: redirects --------------------------------------------------

def test_check_posting_redirect_followed_closed_phrase_on_final_page_returns_closed(requests_mock):
    original_url = "https://boards.greenhouse.io/acme/jobs/999"
    final_url = "https://boards.greenhouse.io/acme"
    requests_mock.get(original_url, status_code=302, headers={"Location": final_url})
    requests_mock.get(final_url, status_code=200, text="This job is no longer available.")
    assert check_posting(original_url) == "closed"


# --- check_posting: source narrowing ------------------------------------------

def test_check_posting_source_matches_its_own_platform_phrase(requests_mock):
    url = "https://boards.greenhouse.io/acme/jobs/1"
    requests_mock.get(
        url, status_code=200,
        text="The job you are looking for is no longer open.",
    )
    assert check_posting(url, source="greenhouse") == "closed"


def test_check_posting_source_narrowing_excludes_other_platform_phrase(requests_mock):
    # Greenhouse-specific phrase does not overlap any generic phrase, so it
    # must NOT trigger closure when a *different* source is given.
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(
        url, status_code=200,
        text="The job you are looking for is no longer open.",
    )
    assert check_posting(url, source="lever") == "open"


def test_check_posting_source_none_checks_union_across_all_platforms(requests_mock):
    url = "https://jobs.lever.co/acme/abc"
    requests_mock.get(
        url, status_code=200,
        text="The job you are looking for is no longer open.",
    )
    assert check_posting(url, source=None) == "closed"


def test_closed_signals_structure():
    assert set(CLOSED_SIGNALS.keys()) >= {"generic", "greenhouse", "lever", "workday"}
    assert "no longer accepting applications" in CLOSED_SIGNALS["generic"]
    assert "position has been filled" in CLOSED_SIGNALS["generic"]
    assert "the job you are looking for is no longer open" in CLOSED_SIGNALS["greenhouse"]
    assert "this posting is no longer accepting applications" in CLOSED_SIGNALS["lever"]
    assert "this job is no longer available" in CLOSED_SIGNALS["workday"]


# --- validate_postings ---------------------------------------------------------

def test_validate_postings_empty_db_returns_zero_summary_no_http_calls(db_conn, requests_mock):
    summary = validate_postings(db_conn)
    assert summary == {"checked": 0, "closed": 0, "gone": 0, "still_open": 0}
    assert len(requests_mock.request_history) == 0


def test_validate_postings_marks_closed_and_gone_updates_db_and_summary(db_conn, requests_mock):
    open_url = "https://jobs.lever.co/acme/open-1"
    closed_url = "https://jobs.lever.co/acme/closed-1"
    gone_url = "https://jobs.lever.co/acme/gone-1"

    _seed_posting(db_conn, open_url, ats_platform="lever")
    _seed_posting(db_conn, closed_url, ats_platform="lever")
    _seed_posting(db_conn, gone_url, ats_platform="lever")

    requests_mock.get(open_url, status_code=200, text="Apply now, Product Manager role open!")
    requests_mock.get(closed_url, status_code=200, text="This posting is no longer accepting applications.")
    requests_mock.get(gone_url, status_code=404)

    summary = validate_postings(db_conn)

    assert summary == {"checked": 3, "closed": 1, "gone": 1, "still_open": 1}

    remaining_open = {p["url"] for p in get_open_postings(db_conn)}
    assert remaining_open == {open_url}


def test_validate_postings_respects_limit(db_conn, requests_mock):
    urls = [f"https://jobs.lever.co/acme/job-{i}" for i in range(3)]
    for url in urls:
        _seed_posting(db_conn, url, ats_platform="lever")
        # Mock every url so the test fails loudly (NoMockAddress) if the
        # implementation ever queries more than `limit` postings.
        requests_mock.get(url, status_code=200, text="Apply now!")

    summary = validate_postings(db_conn, limit=2)

    assert summary["checked"] == 2
    assert len(requests_mock.request_history) == 2

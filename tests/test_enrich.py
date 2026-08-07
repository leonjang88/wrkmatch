"""Tests for wrkmatch/enrich.py: salary/remote extraction from posting detail
text, per-platform detail fetching (mocked via requests_mock), and the
enrich_posting() persistence step.
"""
from __future__ import annotations

import sqlite3

import pytest

from wrkmatch.db import init_db, record_posting, upsert_company
from wrkmatch.enrich import (
    EnrichmentSource,
    enrich_posting,
    extract_remote,
    extract_salary,
    fetch_posting_detail,
)


# --- extract_salary ------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Salary range: $150,000 - $180,000 annually.", (150000, 180000, "USD")),
        ("Compensation: $150k–$180k depending on experience.", (150000, 180000, "USD")),
        ("Base pay 150,000—180,000 USD.", (150000, 180000, "USD")),
        (
            "Base: $185,000.00 Max: $220,000.00 depending on level.",
            (185000, 220000, "USD"),
        ),
        ("This role pays up to $170k per year.", (None, 170000, "USD")),
        ("The salary for this role is $160,000.", (160000, 160000, "USD")),
        ("Starting at $95,000 for this position.", (95000, None, "USD")),
        ("We pay a stipend of $5,000 for relocation.", (None, None, None)),  # below $20k floor
        ("Executive package: $5,000,000 total comp.", (None, None, None)),  # above $2M ceiling
        ("Range is $180,000 - $150,000 depending on level.", (150000, 180000, "USD")),  # swapped
        ("No compensation info provided in this posting.", (None, None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
    ],
)
def test_extract_salary_cases(text, expected):
    assert extract_salary(text) == expected


# --- extract_remote --------------------------------------------------------------

def test_extract_remote_location_remote_word():
    assert extract_remote("", "Remote") == "remote"


def test_extract_remote_text_fully_remote_phrase():
    assert extract_remote("This is a fully remote position.", "United States") == "remote"


def test_extract_remote_bare_remote_word_in_text_alone_is_not_enough():
    # A passing mention of "remote" in the description (without location
    # saying remote, and without the stronger "fully remote"/"remote-first"
    # phrasing) shouldn't be enough to classify as remote.
    assert extract_remote("Occasional remote work is possible.", "Boston, MA") is None


def test_extract_remote_hybrid_in_location():
    assert extract_remote("", "Hybrid - Boston, MA") == "hybrid"


def test_extract_remote_hybrid_in_text():
    assert extract_remote("This is a hybrid role, 3 days in office.", "Boston, MA") == "hybrid"


def test_extract_remote_hybrid_beats_remote_when_both_present_in_text():
    assert extract_remote("Fully remote but with hybrid options.", "Anywhere") == "hybrid"


def test_extract_remote_location_remote_plus_text_hybrid_resolves_hybrid():
    # Per the documented precedence: an explicit location of "Remote" does NOT
    # win over hybrid language in the description -- hybrid is more specific.
    assert extract_remote("This is a hybrid team.", "Remote") == "hybrid"


def test_extract_remote_onsite_phrase():
    assert extract_remote("This is an on-site only role.", "Boston, MA") == "onsite"


def test_extract_remote_none_when_no_signal():
    assert extract_remote("Great benefits and a fun team.", "Boston, MA") is None


def test_extract_remote_handles_none_inputs():
    assert extract_remote(None, None) is None


# --- fetch_posting_detail: greenhouse --------------------------------------------

def test_fetch_posting_detail_greenhouse(requests_mock):
    requests_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/12345",
        json={"content": "<p>Great role. $150,000 - $180,000</p>"},
    )
    source = fetch_posting_detail(
        "https://boards.greenhouse.io/acme/jobs/12345", "greenhouse"
    )
    assert isinstance(source, EnrichmentSource)
    assert source.text == "Great role. $150,000 - $180,000"
    assert source.native_salary_min is None


def test_fetch_posting_detail_greenhouse_unparseable_url_returns_none(requests_mock):
    assert fetch_posting_detail("https://example.com/not-a-gh-url", "greenhouse") is None
    assert requests_mock.call_count == 0


# --- fetch_posting_detail: lever --------------------------------------------------

def test_fetch_posting_detail_lever_description_plain(requests_mock):
    requests_mock.get(
        "https://api.lever.co/v0/postings/acme/abc-123",
        json={"descriptionPlain": "Do great things.", "salaryRange": None},
    )
    source = fetch_posting_detail("https://jobs.lever.co/acme/abc-123", "lever")
    assert source.text == "Do great things."
    assert source.native_salary_min is None
    assert source.native_salary_max is None


def test_fetch_posting_detail_lever_native_salary_wins_over_text_regex(requests_mock):
    requests_mock.get(
        "https://api.lever.co/v0/postings/acme/abc-123",
        json={
            "descriptionPlain": "Pays $50,000 mentioned in passing, ignore this number.",
            "salaryRange": {"min": 150000, "max": 180000, "currency": "USD"},
        },
    )
    source = fetch_posting_detail("https://jobs.lever.co/acme/abc-123", "lever")
    assert source.native_salary_min == 150000
    assert source.native_salary_max == 180000
    assert source.native_currency == "USD"


# --- fetch_posting_detail: workday --------------------------------------------------

def test_fetch_posting_detail_workday(requests_mock):
    url = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Boston/Product-Manager_R1234"
    requests_mock.get(
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/Boston/Product-Manager_R1234",
        json={"jobPostingInfo": {"jobDescription": "<p>Own the roadmap.</p>"}},
    )
    source = fetch_posting_detail(url, "workday")
    assert source.text == "Own the roadmap."


def test_fetch_posting_detail_workday_unparseable_url_returns_none():
    assert fetch_posting_detail("https://example.com/careers/job/1", "workday") is None


# --- fetch_posting_detail: smartrecruiters --------------------------------------------

def test_fetch_posting_detail_smartrecruiters_joins_sections(requests_mock):
    requests_mock.get(
        "https://api.smartrecruiters.com/v1/companies/acme/postings/999",
        json={
            "jobAd": {
                "sections": {
                    "jobDescription": {"title": "About", "text": "<p>Build things.</p>"},
                    "qualifications": {"title": "Qualifications", "text": "5+ years experience."},
                }
            }
        },
    )
    source = fetch_posting_detail("https://jobs.smartrecruiters.com/acme/999", "smartrecruiters")
    assert "Build things." in source.text
    assert "5+ years experience." in source.text


# --- fetch_posting_detail: ashby --------------------------------------------------

def test_fetch_posting_detail_ashby_matches_by_id_and_reads_compensation(requests_mock):
    requests_mock.get(
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true",
        json={
            "jobs": [
                {
                    "id": "other-job-id",
                    "descriptionHtml": "<p>Wrong job.</p>",
                },
                {
                    "id": "abc123def456",
                    "descriptionHtml": "<p>Right job.</p>",
                    "compensation": {
                        "summaryComponents": [
                            {"minValue": 140000, "maxValue": 170000, "currencyCode": "USD"}
                        ]
                    },
                },
            ]
        },
    )
    source = fetch_posting_detail(
        "https://jobs.ashbyhq.com/acme/abc123def456", "ashby"
    )
    assert source.text == "Right job."
    assert source.native_salary_min == 140000
    assert source.native_salary_max == 170000
    assert source.native_currency == "USD"


# --- fetch_posting_detail: no-detail platforms + unknown -------------------------

@pytest.mark.parametrize("platform", ["recruitee", "personio", "workable"])
def test_fetch_posting_detail_skip_platforms_return_none_no_network(requests_mock, platform):
    assert fetch_posting_detail("https://example.com/whatever", platform) is None
    assert requests_mock.call_count == 0


def test_fetch_posting_detail_unknown_platform_returns_none():
    assert fetch_posting_detail("https://example.com/whatever", None) is None
    assert fetch_posting_detail("https://example.com/whatever", "bamboohr") is None


# --- enrich_posting ---------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(tmp_path / "enrich_test.db")
    yield conn
    conn.close()


def _seed_posting(conn: sqlite3.Connection, url: str, platform: str, location: str = "Remote") -> dict:
    company_id = upsert_company(conn, "acme widgets", ats_platform=platform, ats_slug="acme")
    record_posting(conn, company_id, title="Product Manager", url=url, location=location, ats_platform=platform)
    row = conn.execute("SELECT * FROM postings WHERE url = ?", (url,)).fetchone()
    return dict(row)


def test_enrich_posting_updates_row_from_extracted_fields(db_conn, requests_mock):
    url = "https://boards.greenhouse.io/acme/jobs/1"
    posting = _seed_posting(db_conn, url, "greenhouse", location="Remote")
    requests_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
        json={"content": "<p>Fully remote. Pays $150,000 - $180,000.</p>"},
    )

    ok = enrich_posting(db_conn, posting)
    assert ok is True

    row = db_conn.execute("SELECT * FROM postings WHERE url = ?", (url,)).fetchone()
    assert row["salary_min"] == 150000
    assert row["salary_max"] == 180000
    assert row["salary_currency"] == "USD"
    assert row["remote"] == "remote"
    assert row["enriched_at"] is not None


def test_enrich_posting_sets_enriched_at_even_when_extraction_finds_nothing(db_conn, requests_mock):
    url = "https://boards.greenhouse.io/acme/jobs/2"
    posting = _seed_posting(db_conn, url, "greenhouse", location="Boston, MA")
    requests_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/2",
        json={"content": "<p>Nothing salary-related here.</p>"},
    )

    ok = enrich_posting(db_conn, posting)
    assert ok is True

    row = db_conn.execute("SELECT * FROM postings WHERE url = ?", (url,)).fetchone()
    assert row["salary_min"] is None
    assert row["salary_max"] is None
    assert row["enriched_at"] is not None


def test_enrich_posting_leaves_enriched_at_null_on_network_error(db_conn, requests_mock):
    import requests

    url = "https://boards.greenhouse.io/acme/jobs/3"
    posting = _seed_posting(db_conn, url, "greenhouse")
    requests_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/3",
        exc=requests.exceptions.ConnectTimeout,
    )

    ok = enrich_posting(db_conn, posting)
    assert ok is False

    row = db_conn.execute("SELECT * FROM postings WHERE url = ?", (url,)).fetchone()
    assert row["enriched_at"] is None


def test_enrich_posting_skip_platform_still_sets_enriched_at(db_conn):
    url = "https://acme.recruitee.com/o/product-manager"
    posting = _seed_posting(db_conn, url, "recruitee")

    ok = enrich_posting(db_conn, posting)
    assert ok is True

    row = db_conn.execute("SELECT * FROM postings WHERE url = ?", (url,)).fetchone()
    assert row["enriched_at"] is not None
    assert row["salary_min"] is None

"""Interface contract for wrkmatch/db.py — a sqlite3-stdlib persistence layer.

wrkmatch/db.py does not exist yet; these tests fail on import until it's implemented.
That's expected. The contract they define:

    init_db(path) -> sqlite3.Connection
        Creates contacts/companies/postings/scans tables if missing. Safe to call
        twice against the same path (idempotent schema creation).

    upsert_contacts(conn, contacts, source) -> int
        contacts: list[dict] with keys first_name, last_name, company, position,
        connected_on. Inserts rows, skipping ones that already exist for the same
        (first_name, last_name, company, source). Returns the number of rows
        newly inserted (dupes don't count).

    upsert_company(conn, name, ats_platform=None, ats_slug=None, last_scanned=None) -> int
        `name` is the already-normalized company name (unique key). Repeat calls
        update the existing row (by name) instead of inserting a duplicate.
        Returns the company id either way.

    record_posting(conn, company_id, title, url, location=None, ats_platform=None) -> int
        `url` is the unique key. New url -> insert with first_seen == last_seen ==
        now, status='open'. Existing url -> update last_seen (and other mutable
        fields) but leave first_seen untouched. Returns the posting id either way.

    mark_postings_status(conn, urls, status) -> int
        Bulk status transition for postings matching any of `urls`. Returns the
        number of rows updated. Empty `urls` is a no-op.

    start_scan(conn) -> int
        Inserts a scan row with started_at=now, finished_at=NULL. Returns scan id.

    finish_scan(conn, scan_id, companies_scanned, postings_found, new_postings) -> None
        Sets finished_at=now and the three stat columns on the given scan row.

    get_contacts_by_company(conn, normalized_name) -> list[dict]
        Contacts whose (raw) company normalizes to `normalized_name`, matched
        case-insensitively regardless of how the raw company string was cased.

    get_open_postings(conn) -> list[dict]
        All postings with status='open', each dict including a 'company_name'
        key from the joined companies row.

    get_companies_with_contacts(conn) -> list[dict]
        One dict per distinct normalized company present in contacts, each with
        'company' (normalized name) and 'contact_count'. Companies with zero
        contacts are omitted.

    All timestamps (first_seen, last_seen, started_at, finished_at, last_scanned)
    are ISO-8601 UTC strings parseable by datetime.fromisoformat.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from wrkmatch.db import (
    finish_scan,
    get_companies_with_contacts,
    get_contacts_by_company,
    get_open_postings,
    get_setting,
    init_db,
    mark_postings_status,
    record_posting,
    set_setting,
    start_scan,
    upsert_company,
    upsert_contacts,
)
from wrkmatch.normalize import normalize_company_name


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


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def assert_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, f"{value!r} is not timezone-aware"
    return dt


# --- init_db -----------------------------------------------------------------

def test_init_db_creates_expected_tables(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    assert {"contacts", "companies", "postings", "scans"} <= table_names(conn)


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    init_db(path).close()
    conn = init_db(path)  # second call must not raise
    assert {"contacts", "companies", "postings", "scans"} <= table_names(conn)
    conn.close()


# --- upsert_contacts -----------------------------------------------------------

def test_upsert_contacts_inserts_new_rows(db_conn):
    n = upsert_contacts(db_conn, [make_contact(), make_contact(first_name="Bob")], source="leon")
    assert n == 2
    assert count_rows(db_conn, "contacts") == 2


def test_upsert_contacts_dedupes_same_name_company_source(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")
    n_second = upsert_contacts(db_conn, [make_contact()], source="leon")
    assert n_second == 0
    assert count_rows(db_conn, "contacts") == 1


def test_upsert_contacts_same_contact_different_source_is_separate_row(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")
    n_wife = upsert_contacts(db_conn, [make_contact()], source="wife")
    assert n_wife == 1
    assert count_rows(db_conn, "contacts") == 2


def test_upsert_contacts_same_name_different_company_not_deduped(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    n = upsert_contacts(db_conn, [make_contact(company="Beta Systems")], source="leon")
    assert n == 1
    assert count_rows(db_conn, "contacts") == 2


def test_upsert_contacts_empty_list_is_noop(db_conn):
    n = upsert_contacts(db_conn, [], source="leon")
    assert n == 0
    assert count_rows(db_conn, "contacts") == 0


def test_upsert_contacts_stores_all_fields(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")
    row = db_conn.execute(
        "SELECT first_name, last_name, company, position, connected_on, source FROM contacts"
    ).fetchone()
    assert tuple(row) == ("Jane", "Doe", "Acme Widgets", "Product Manager", "01 Jan 2023", "leon")


# --- upsert_contacts: url/email/rating (schema v2) --------------------------

def test_upsert_contacts_stores_url_and_email(db_conn):
    contact = make_contact()
    contact["url"] = "https://linkedin.com/in/janedoe"
    contact["email"] = "jane@example.com"
    upsert_contacts(db_conn, [contact], source="leon")

    row = db_conn.execute("SELECT url, email FROM contacts").fetchone()
    assert tuple(row) == ("https://linkedin.com/in/janedoe", "jane@example.com")


def test_upsert_contacts_url_and_email_default_to_none(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")
    row = db_conn.execute("SELECT url, email FROM contacts").fetchone()
    assert tuple(row) == (None, None)


def test_upsert_contacts_default_rating_is_ok(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")
    row = db_conn.execute("SELECT rating FROM contacts").fetchone()
    assert row[0] == "ok"


def test_upsert_contacts_backfills_null_url_and_email_on_reupsert(db_conn):
    upsert_contacts(db_conn, [make_contact()], source="leon")  # no url/email yet

    contact_with_details = make_contact()
    contact_with_details["url"] = "https://linkedin.com/in/janedoe"
    contact_with_details["email"] = "jane@example.com"
    n = upsert_contacts(db_conn, [contact_with_details], source="leon")

    assert n == 0  # dupe, not a new row
    assert count_rows(db_conn, "contacts") == 1
    row = db_conn.execute("SELECT url, email FROM contacts").fetchone()
    assert tuple(row) == ("https://linkedin.com/in/janedoe", "jane@example.com")


def test_upsert_contacts_does_not_clobber_existing_non_null_url(db_conn):
    contact = make_contact()
    contact["url"] = "https://linkedin.com/in/janedoe-original"
    upsert_contacts(db_conn, [contact], source="leon")

    contact_new_url = make_contact()
    contact_new_url["url"] = "https://linkedin.com/in/janedoe-changed"
    upsert_contacts(db_conn, [contact_new_url], source="leon")

    row = db_conn.execute("SELECT url FROM contacts").fetchone()
    assert row[0] == "https://linkedin.com/in/janedoe-original"


def test_upsert_contacts_does_not_clobber_existing_non_null_email(db_conn):
    contact = make_contact()
    contact["email"] = "jane-original@example.com"
    upsert_contacts(db_conn, [contact], source="leon")

    contact_new_email = make_contact()
    contact_new_email["email"] = "jane-changed@example.com"
    upsert_contacts(db_conn, [contact_new_email], source="leon")

    row = db_conn.execute("SELECT email FROM contacts").fetchone()
    assert row[0] == "jane-original@example.com"


# --- settings ----------------------------------------------------------------

def test_set_and_get_setting_roundtrip(db_conn):
    set_setting(db_conn, "last_scan_at", "2024-01-01T00:00:00+00:00")
    assert get_setting(db_conn, "last_scan_at") == "2024-01-01T00:00:00+00:00"


def test_get_setting_returns_none_when_missing_by_default(db_conn):
    assert get_setting(db_conn, "nonexistent") is None


def test_get_setting_returns_given_default_when_missing(db_conn):
    assert get_setting(db_conn, "nonexistent", default="fallback") == "fallback"


def test_set_setting_repeat_call_overwrites_existing_value(db_conn):
    set_setting(db_conn, "key", "v1")
    set_setting(db_conn, "key", "v2")
    assert get_setting(db_conn, "key") == "v2"


# --- upsert_company --------------------------------------------------------

def test_upsert_company_inserts_and_returns_id(db_conn):
    company_id = upsert_company(db_conn, "acme widgets", ats_platform="greenhouse", ats_slug="acme")
    assert isinstance(company_id, int)
    assert count_rows(db_conn, "companies") == 1


def test_upsert_company_repeat_call_does_not_duplicate(db_conn):
    id1 = upsert_company(db_conn, "acme widgets", ats_platform="greenhouse", ats_slug="acme")
    id2 = upsert_company(db_conn, "acme widgets", ats_platform="greenhouse", ats_slug="acme-v2")
    assert id1 == id2
    assert count_rows(db_conn, "companies") == 1


def test_upsert_company_repeat_call_updates_fields(db_conn):
    upsert_company(db_conn, "acme widgets", ats_platform="greenhouse", ats_slug="acme")
    upsert_company(db_conn, "acme widgets", ats_platform="lever", ats_slug="acme-new")
    row = db_conn.execute(
        "SELECT ats_platform, ats_slug FROM companies WHERE name = 'acme widgets'"
    ).fetchone()
    assert tuple(row) == ("lever", "acme-new")


def test_upsert_company_different_names_are_separate_rows(db_conn):
    upsert_company(db_conn, "acme widgets")
    upsert_company(db_conn, "beta systems")
    assert count_rows(db_conn, "companies") == 2


# --- record_posting ---------------------------------------------------------

def test_record_posting_new_url_sets_open_with_matching_first_last_seen(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    posting_id = record_posting(
        db_conn, company_id, title="Product Manager", url="https://boards.greenhouse.io/acme/jobs/1"
    )
    row = db_conn.execute(
        "SELECT status, first_seen, last_seen FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()
    status, first_seen, last_seen = row
    assert status == "open"
    assert first_seen == last_seen
    assert_iso_utc(first_seen)


def test_record_posting_existing_url_preserves_first_seen_updates_last_seen(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    url = "https://boards.greenhouse.io/acme/jobs/1"
    posting_id = record_posting(db_conn, company_id, title="Product Manager", url=url)
    first_seen_1 = db_conn.execute(
        "SELECT first_seen FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()[0]

    time.sleep(0.02)
    posting_id_2 = record_posting(db_conn, company_id, title="Product Manager", url=url)

    first_seen_2, last_seen_2 = db_conn.execute(
        "SELECT first_seen, last_seen FROM postings WHERE id = ?", (posting_id_2,)
    ).fetchone()

    assert posting_id_2 == posting_id
    assert first_seen_2 == first_seen_1
    assert last_seen_2 > first_seen_1


def test_record_posting_existing_url_does_not_duplicate_row(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    url = "https://boards.greenhouse.io/acme/jobs/1"
    record_posting(db_conn, company_id, title="Product Manager", url=url)
    record_posting(db_conn, company_id, title="Senior Product Manager", url=url)
    assert count_rows(db_conn, "postings") == 1


def test_record_posting_different_urls_are_separate_rows(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    record_posting(db_conn, company_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1")
    record_posting(db_conn, company_id, title="Senior PM", url="https://boards.greenhouse.io/acme/jobs/2")
    assert count_rows(db_conn, "postings") == 2


# --- mark_postings_status ---------------------------------------------------

def test_mark_postings_status_updates_matching_urls(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    url_a = "https://boards.greenhouse.io/acme/jobs/1"
    url_b = "https://boards.greenhouse.io/acme/jobs/2"
    url_c = "https://boards.greenhouse.io/acme/jobs/3"
    record_posting(db_conn, company_id, title="PM", url=url_a)
    record_posting(db_conn, company_id, title="Senior PM", url=url_b)
    record_posting(db_conn, company_id, title="Staff PM", url=url_c)

    updated = mark_postings_status(db_conn, [url_a, url_b], "closed")
    assert updated == 2

    statuses = {
        row[0]: row[1]
        for row in db_conn.execute("SELECT url, status FROM postings").fetchall()
    }
    assert statuses[url_a] == "closed"
    assert statuses[url_b] == "closed"
    assert statuses[url_c] == "open"


def test_mark_postings_status_empty_urls_is_noop(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    url = "https://boards.greenhouse.io/acme/jobs/1"
    record_posting(db_conn, company_id, title="PM", url=url)

    updated = mark_postings_status(db_conn, [], "closed")
    assert updated == 0

    status = db_conn.execute("SELECT status FROM postings WHERE url = ?", (url,)).fetchone()[0]
    assert status == "open"


# --- scans -------------------------------------------------------------------

def test_start_scan_returns_id_and_sets_started_at(db_conn):
    scan_id = start_scan(db_conn)
    assert isinstance(scan_id, int)
    started_at, finished_at = db_conn.execute(
        "SELECT started_at, finished_at FROM scans WHERE id = ?", (scan_id,)
    ).fetchone()
    assert_iso_utc(started_at)
    assert finished_at is None


def test_finish_scan_sets_finished_at_and_stats(db_conn):
    scan_id = start_scan(db_conn)
    finish_scan(db_conn, scan_id, companies_scanned=10, postings_found=25, new_postings=3)

    row = db_conn.execute(
        "SELECT finished_at, companies_scanned, postings_found, new_postings FROM scans WHERE id = ?",
        (scan_id,),
    ).fetchone()
    finished_at, companies_scanned, postings_found, new_postings = row
    assert_iso_utc(finished_at)
    assert (companies_scanned, postings_found, new_postings) == (10, 25, 3)


# --- get_contacts_by_company --------------------------------------------------

def test_get_contacts_by_company_matches_normalized_name(db_conn):
    upsert_contacts(db_conn, [make_contact(company="ACME WIDGETS", last_name="Doe")], source="leon")
    upsert_contacts(db_conn, [make_contact(company="Beta Systems", last_name="Ray")], source="leon")

    results = get_contacts_by_company(db_conn, normalize_company_name("Acme Widgets"))
    assert len(results) == 1
    assert results[0]["last_name"] == "Doe"


def test_get_contacts_by_company_no_match_returns_empty_list(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    assert get_contacts_by_company(db_conn, normalize_company_name("Nonexistent Co")) == []


# --- get_open_postings ---------------------------------------------------------

def test_get_open_postings_excludes_closed(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    open_url = "https://boards.greenhouse.io/acme/jobs/1"
    closed_url = "https://boards.greenhouse.io/acme/jobs/2"
    record_posting(db_conn, company_id, title="Open Role", url=open_url)
    record_posting(db_conn, company_id, title="Closed Role", url=closed_url)
    mark_postings_status(db_conn, [closed_url], "closed")

    open_postings = get_open_postings(db_conn)
    urls = {p["url"] for p in open_postings}
    assert urls == {open_url}


def test_get_open_postings_includes_company_name(db_conn):
    company_id = upsert_company(db_conn, "acme widgets")
    record_posting(db_conn, company_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1")

    postings = get_open_postings(db_conn)
    assert len(postings) == 1
    assert postings[0]["company_name"] == "acme widgets"


# --- get_companies_with_contacts ----------------------------------------------

def test_get_companies_with_contacts_counts_per_normalized_company(db_conn):
    upsert_contacts(
        db_conn,
        [
            make_contact(company="Acme Widgets", last_name="Doe"),
            make_contact(company="ACME WIDGETS", last_name="Ray"),
            make_contact(company="Beta Systems", last_name="Kim"),
        ],
        source="leon",
    )

    results = {row["company"]: row["contact_count"] for row in get_companies_with_contacts(db_conn)}
    assert results[normalize_company_name("Acme Widgets")] == 2
    assert results[normalize_company_name("Beta Systems")] == 1


def test_get_companies_with_contacts_omits_companies_with_no_contacts(db_conn):
    upsert_contacts(db_conn, [make_contact(company="Acme Widgets")], source="leon")
    results = [row["company"] for row in get_companies_with_contacts(db_conn)]
    assert normalize_company_name("Nonexistent Co") not in results

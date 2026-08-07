"""Tests for the v1 -> v2 schema migration in wrkmatch/db.py.

Builds a database using the OLD v1 schema (pre-settings table, pre
url/email/rating/priority/user_status columns), seeds it with v1-shaped rows,
then calls init_db on that path and asserts:
  - the new columns exist with the documented defaults
  - old data survives untouched
  - the settings table is present and usable
  - init_db is safe to call twice against the same (already-migrated) path
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from wrkmatch.db import get_setting, init_db, set_setting

# Copied verbatim from the pre-v2 SCHEMA in wrkmatch/db.py — intentionally
# without the new columns/table so this test exercises a genuine v1 database.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    company TEXT NOT NULL,
    position TEXT,
    connected_on TEXT,
    source TEXT NOT NULL,
    UNIQUE(first_name, last_name, company, source)
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ats_platform TEXT,
    ats_slug TEXT,
    last_scanned TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT,
    url TEXT NOT NULL UNIQUE,
    location TEXT,
    ats_platform TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    companies_scanned INTEGER,
    postings_found INTEGER,
    new_postings INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_v1_db(path) -> None:
    """Create a database on disk using the old v1 schema and seed one row
    per table (contacts/companies/postings), exactly as a real pre-migration
    wrkmatch db would look.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)

    conn.execute(
        """
        INSERT INTO contacts (first_name, last_name, company, position, connected_on, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Jane", "Doe", "Acme Widgets", "Product Manager", "01 Jan 2023", "leon"),
    )
    conn.execute(
        "INSERT INTO companies (name, ats_platform, ats_slug, last_scanned) VALUES (?, ?, ?, ?)",
        ("acme widgets", "greenhouse", "acme", _now()),
    )
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name = 'acme widgets'"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO postings
            (company_id, title, url, location, ats_platform, first_seen, last_seen, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            "Product Manager",
            "https://boards.greenhouse.io/acme/jobs/1",
            "Remote",
            "greenhouse",
            _now(),
            _now(),
            "open",
        ),
    )
    conn.commit()
    conn.close()


def test_migration_adds_new_contact_columns_with_defaults(tmp_path):
    path = tmp_path / "v1.db"
    _build_v1_db(path)

    conn = init_db(path)
    contact = conn.execute("SELECT * FROM contacts").fetchone()

    assert contact["first_name"] == "Jane"
    assert contact["last_name"] == "Doe"
    assert contact["company"] == "Acme Widgets"
    assert contact["position"] == "Product Manager"
    assert contact["connected_on"] == "01 Jan 2023"
    assert contact["source"] == "leon"
    assert contact["url"] is None
    assert contact["email"] is None
    assert contact["rating"] == "ok"
    conn.close()


def test_migration_adds_company_priority_column_with_default(tmp_path):
    path = tmp_path / "v1.db"
    _build_v1_db(path)

    conn = init_db(path)
    company = conn.execute("SELECT * FROM companies WHERE name = 'acme widgets'").fetchone()

    assert company["ats_platform"] == "greenhouse"
    assert company["ats_slug"] == "acme"
    assert company["priority"] == "normal"
    conn.close()


def test_migration_adds_posting_user_status_column_with_default(tmp_path):
    path = tmp_path / "v1.db"
    _build_v1_db(path)

    conn = init_db(path)
    posting = conn.execute(
        "SELECT * FROM postings WHERE url = 'https://boards.greenhouse.io/acme/jobs/1'"
    ).fetchone()

    assert posting["status"] == "open"
    assert posting["user_status"] == "none"
    conn.close()


def test_migration_creates_usable_settings_table(tmp_path):
    path = tmp_path / "v1.db"
    _build_v1_db(path)

    conn = init_db(path)
    assert get_setting(conn, "missing_key") is None
    assert get_setting(conn, "missing_key", default="fallback") == "fallback"

    set_setting(conn, "last_scan_at", "2024-01-01T00:00:00+00:00")
    assert get_setting(conn, "last_scan_at") == "2024-01-01T00:00:00+00:00"
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "v1.db"
    _build_v1_db(path)

    init_db(path).close()
    conn = init_db(path)  # second call, now against an already-migrated db, must not raise

    contact = conn.execute("SELECT * FROM contacts").fetchone()
    assert contact["rating"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1

    company = conn.execute("SELECT * FROM companies").fetchone()
    assert company["priority"] == "normal"

    posting = conn.execute("SELECT * FROM postings").fetchone()
    assert posting["user_status"] == "none"

    conn.close()

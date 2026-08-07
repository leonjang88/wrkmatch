"""Interface contract for two NOT-YET-IMPLEMENTED additions to wrkmatch/io_utils.py.

wrkmatch/io_utils.py currently only has read_connections(file_or_path) -> pd.DataFrame
(see that function's docstring for its CSV-quirk handling: 'Notes:' preamble, mixed
encodings, varying column names, Company derived from Position via ' at '). These
tests fail on import until the two new functions below exist. That's expected.

The contract:

    connections_to_contacts(df: pd.DataFrame) -> list[dict]
        Converts a read_connections() DataFrame into the dict shape
        wrkmatch.db.upsert_contacts expects: one dict per row with keys
        first_name, last_name, company, position, connected_on.
        - first_name/last_name/company are always strings (never None, never
          the literal "nan") because contacts.first_name/last_name/company are
          NOT NULL columns in the db schema. Missing/NaN source data becomes "".
        - position/connected_on may be None when the source column is absent
          or the cell is NaN (db columns are nullable) but must never be the
          literal "nan" or "NaN" either.
        - Column lookup is case-insensitive-ish over read_connections' output:
          "First Name" -> first_name, "Last Name" -> last_name, "Company"
          (guaranteed present by read_connections) -> company, "Position" ->
          position, "Connected On" -> connected_on.
        - Row order is preserved; one dict per DataFrame row.

    load_csvs_to_db(conn, sources: dict[str, str]) -> dict
        sources maps a source label -> CSV path, e.g. {"leon": "a.csv", "wife": "b.csv"}.
        For each entry: read_connections(path) -> connections_to_contacts(df) ->
        db.upsert_contacts(conn, contacts, source=label).
        Returns {label: {"rows": N, "inserted": M}, ...} where N is the number of
        contact rows produced from that CSV (post read_connections filtering) and
        M is how many of those were newly inserted (upsert_contacts' return value).
        - Idempotent: re-running with the same sources a second time against the
          same conn inserts 0 new rows (M == 0) while "rows" stays the same.
        - The same person appearing in both CSVs (different source labels) is
          two separate contact rows, since upsert_contacts's uniqueness key
          includes source.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from wrkmatch.db import init_db
from wrkmatch.io_utils import connections_to_contacts, load_csvs_to_db, read_connections


# --- fixtures: realistic CSV files on disk -----------------------------------

NORMAL_CSV = """First Name,Last Name,Full Name,Company,Position,Connected On
Ava,Nguyen,Ava Nguyen,Acme Inc,Senior PM at Acme Inc,01 Jan 2023
Noah,Schmidt,Noah Schmidt,Globex GmbH,Engineer at Globex GmbH,02 Feb 2023
"""

# LinkedIn's real export prefixes a few "Notes:" lines before the true header.
NOTES_PREAMBLE_CSV = """Notes:
"When exporting your connection data, you may notice that some of the third
column has entries for the person's location."

First Name,Last Name,Full Name,Company,Position,Connected On
Priya,Shah,Priya Shah,Initech AG,Data Scientist at Initech AG,03 Mar 2023
Ava,Nguyen,Ava Nguyen,Acme Inc,Staff PM at Acme Inc,04 Apr 2023
"""

# No explicit Company column -> read_connections derives it from " at " in Position.
DERIVED_COMPANY_CSV = """First Name,Last Name,Full Name,Position,Connected On
Liam,Rossi,Liam Rossi,Designer at Beta Systems,05 May 2023
"""

# A row with a blank company gets dropped entirely by read_connections.
BLANK_COMPANY_CSV = """First Name,Last Name,Full Name,Company,Position,Connected On
Emma,Weber,Emma Weber,,Freelancer,06 Jun 2023
Ava,Nguyen,Ava Nguyen,Acme Inc,Senior PM at Acme Inc,01 Jan 2023
"""

# Newer LinkedIn exports include URL and Email Address columns.
WITH_URL_EMAIL_CSV = """First Name,Last Name,Full Name,URL,Email Address,Company,Position,Connected On
Ava,Nguyen,Ava Nguyen,https://linkedin.com/in/avanguyen,ava@example.com,Acme Inc,Senior PM at Acme Inc,01 Jan 2023
"""


def _write_utf16(path, text: str) -> None:
    path.write_bytes(text.encode("utf-16"))


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(tmp_path / "wrkmatch_test.db")
    yield conn
    conn.close()


# --- connections_to_contacts --------------------------------------------------

def test_connections_to_contacts_basic_shape(tmp_path):
    path = tmp_path / "normal.csv"
    path.write_text(NORMAL_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 2
    assert contacts[0] == {
        "first_name": "Ava",
        "last_name": "Nguyen",
        "company": "Acme Inc",
        "position": "Senior PM at Acme Inc",
        "connected_on": "01 Jan 2023",
        "url": None,
        "email": None,
    }


def test_connections_to_contacts_returns_list_of_dicts(tmp_path):
    path = tmp_path / "normal.csv"
    path.write_text(NORMAL_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert isinstance(contacts, list)
    assert all(isinstance(c, dict) for c in contacts)
    expected_keys = {"first_name", "last_name", "company", "position", "connected_on", "url", "email"}
    assert all(set(c.keys()) == expected_keys for c in contacts)


def test_connections_to_contacts_strips_notes_preamble(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text(NOTES_PREAMBLE_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 2
    assert {c["first_name"] for c in contacts} == {"Priya", "Ava"}


def test_connections_to_contacts_derived_company_column(tmp_path):
    path = tmp_path / "derived.csv"
    path.write_text(DERIVED_COMPANY_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 1
    assert contacts[0]["company"] == "Beta Systems"


def test_connections_to_contacts_missing_optional_column_is_none_not_nan(tmp_path):
    # No "Connected On" column at all in this export.
    csv_text = "First Name,Last Name,Company,Position\nZoe,Park,Acme Inc,PM at Acme Inc\n"
    path = tmp_path / "no_date.csv"
    path.write_text(csv_text, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 1
    connected_on = contacts[0]["connected_on"]
    assert connected_on in (None, "")
    assert connected_on != "nan"


def test_connections_to_contacts_never_leaks_nan_string(tmp_path):
    # Blank cell (not a missing column) for Connected On -> pandas reads it as NaN.
    csv_text = (
        "First Name,Last Name,Company,Position,Connected On\n"
        "Zoe,Park,Acme Inc,PM at Acme Inc,\n"
    )
    path = tmp_path / "blank_cell.csv"
    path.write_text(csv_text, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 1
    for value in contacts[0].values():
        assert value != "nan"
        assert value != "NaN"


def test_connections_to_contacts_required_fields_are_never_none(tmp_path):
    path = tmp_path / "normal.csv"
    path.write_text(NORMAL_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    for c in contacts:
        assert c["first_name"] is not None
        assert c["last_name"] is not None
        assert c["company"] is not None
        assert isinstance(c["first_name"], str)
        assert isinstance(c["last_name"], str)
        assert isinstance(c["company"], str)


def test_connections_to_contacts_includes_url_and_email(tmp_path):
    path = tmp_path / "with_url_email.csv"
    path.write_text(WITH_URL_EMAIL_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 1
    assert contacts[0]["url"] == "https://linkedin.com/in/avanguyen"
    assert contacts[0]["email"] == "ava@example.com"


def test_connections_to_contacts_missing_url_email_columns_is_none(tmp_path):
    # NORMAL_CSV has no URL / Email Address columns at all.
    path = tmp_path / "normal.csv"
    path.write_text(NORMAL_CSV, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    for c in contacts:
        assert "url" in c
        assert "email" in c
        assert c["url"] is None
        assert c["email"] is None


def test_connections_to_contacts_blank_url_email_cells_are_none_not_nan(tmp_path):
    csv_text = (
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Zoe,Park,,,Acme Inc,PM at Acme Inc,01 Jan 2023\n"
    )
    path = tmp_path / "blank_url_email.csv"
    path.write_text(csv_text, encoding="utf-8")
    df = read_connections(path)

    contacts = connections_to_contacts(df)

    assert len(contacts) == 1
    assert contacts[0]["url"] is None
    assert contacts[0]["email"] is None


def test_connections_to_contacts_empty_dataframe_returns_empty_list(tmp_path):
    path = tmp_path / "blank_company.csv"
    # Every row has a blank Company, so read_connections drops all of them.
    path.write_text(
        "First Name,Last Name,Company,Position\nSam,Lee,,Freelancer\n", encoding="utf-8"
    )
    df = read_connections(path)

    assert connections_to_contacts(df) == []


# --- load_csvs_to_db ------------------------------------------------------------

def test_load_csvs_to_db_basic_summary(db_conn, tmp_path):
    leon_path = tmp_path / "leon.csv"
    leon_path.write_text(NORMAL_CSV, encoding="utf-8")

    summary = load_csvs_to_db(db_conn, {"leon": str(leon_path)})

    assert summary == {"leon": {"rows": 2, "inserted": 2}}
    row_count = db_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert row_count == 2


def test_load_csvs_to_db_multiple_sources(db_conn, tmp_path):
    leon_path = tmp_path / "leon.csv"
    leon_path.write_text(NORMAL_CSV, encoding="utf-8")
    wife_path = tmp_path / "wife.csv"
    wife_path.write_text(NOTES_PREAMBLE_CSV, encoding="utf-8")

    summary = load_csvs_to_db(db_conn, {"leon": str(leon_path), "wife": str(wife_path)})

    assert summary["leon"] == {"rows": 2, "inserted": 2}
    assert summary["wife"] == {"rows": 2, "inserted": 2}
    total = db_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert total == 4


def test_load_csvs_to_db_handles_utf16_encoding(db_conn, tmp_path):
    utf16_path = tmp_path / "utf16.csv"
    _write_utf16(utf16_path, NORMAL_CSV)

    summary = load_csvs_to_db(db_conn, {"leon": str(utf16_path)})

    assert summary["leon"]["rows"] == 2
    assert summary["leon"]["inserted"] == 2


def test_load_csvs_to_db_rerun_is_idempotent(db_conn, tmp_path):
    leon_path = tmp_path / "leon.csv"
    leon_path.write_text(NORMAL_CSV, encoding="utf-8")

    first = load_csvs_to_db(db_conn, {"leon": str(leon_path)})
    second = load_csvs_to_db(db_conn, {"leon": str(leon_path)})

    assert first["leon"]["inserted"] == 2
    assert second["leon"]["rows"] == 2
    assert second["leon"]["inserted"] == 0
    total = db_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert total == 2


def test_load_csvs_to_db_overlapping_contact_across_sources_is_two_rows(db_conn, tmp_path):
    # Ava Nguyen @ Acme Inc appears in both CSVs.
    leon_path = tmp_path / "leon.csv"
    leon_path.write_text(NORMAL_CSV, encoding="utf-8")
    wife_path = tmp_path / "wife.csv"
    wife_path.write_text(NOTES_PREAMBLE_CSV, encoding="utf-8")

    load_csvs_to_db(db_conn, {"leon": str(leon_path), "wife": str(wife_path)})

    rows = db_conn.execute(
        "SELECT source FROM contacts WHERE first_name = 'Ava' AND last_name = 'Nguyen' "
        "AND company = 'Acme Inc'"
    ).fetchall()
    sources = {r[0] for r in rows}
    assert sources == {"leon", "wife"}
    assert len(rows) == 2


def test_load_csvs_to_db_drops_blank_company_rows(db_conn, tmp_path):
    path = tmp_path / "blank_company.csv"
    path.write_text(BLANK_COMPANY_CSV, encoding="utf-8")

    summary = load_csvs_to_db(db_conn, {"leon": str(path)})

    # Emma Weber has a blank Company and is dropped by read_connections;
    # only Ava Nguyen survives.
    assert summary["leon"] == {"rows": 1, "inserted": 1}


def test_load_csvs_to_db_empty_sources_returns_empty_summary(db_conn):
    assert load_csvs_to_db(db_conn, {}) == {}

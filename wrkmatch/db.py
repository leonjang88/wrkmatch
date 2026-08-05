from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

SCHEMA = """
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


def init_db(path) -> sqlite3.Connection:
    """Open (creating if needed) the wrkmatch sqlite db and apply the schema.
    Safe to call repeatedly against the same path.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_contacts(conn: sqlite3.Connection, contacts: List[dict], source: str) -> int:
    """Insert contacts, skipping ones that already exist for the same
    (first_name, last_name, company, source). Returns count newly inserted.
    """
    inserted = 0
    for c in contacts:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO contacts
                (first_name, last_name, company, position, connected_on, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (c["first_name"], c["last_name"], c["company"], c.get("position"), c.get("connected_on"), source),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    last_scanned: Optional[str] = None,
) -> int:
    """Insert or update a company by (normalized) name. Returns the company id."""
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO companies (name, ats_platform, ats_slug, last_scanned) VALUES (?, ?, ?, ?)",
            (name, ats_platform, ats_slug, last_scanned),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    company_id = row["id"]
    conn.execute(
        "UPDATE companies SET ats_platform = ?, ats_slug = ?, last_scanned = ? WHERE id = ?",
        (ats_platform, ats_slug, last_scanned, company_id),
    )
    conn.commit()
    return company_id


def record_posting(
    conn: sqlite3.Connection,
    company_id: int,
    title: str,
    url: str,
    location: Optional[str] = None,
    ats_platform: Optional[str] = None,
) -> int:
    """Insert a new posting or update an existing one (matched by url).
    New postings get first_seen == last_seen == now and status='open'.
    Existing postings keep first_seen and get last_seen (and mutable fields) updated.
    """
    now = _now()
    row = conn.execute("SELECT id FROM postings WHERE url = ?", (url,)).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO postings
                (company_id, title, url, location, ats_platform, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (company_id, title, url, location, ats_platform, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    posting_id = row["id"]
    conn.execute(
        """
        UPDATE postings SET company_id = ?, title = ?, location = ?, ats_platform = ?, last_seen = ?
        WHERE id = ?
        """,
        (company_id, title, location, ats_platform, now, posting_id),
    )
    conn.commit()
    return posting_id


def mark_postings_status(conn: sqlite3.Connection, urls: List[str], status: str) -> int:
    """Bulk status transition for postings matching any of `urls`. Returns rows updated."""
    if not urls:
        return 0
    placeholders = ",".join("?" for _ in urls)
    cur = conn.execute(
        f"UPDATE postings SET status = ? WHERE url IN ({placeholders})",
        (status, *urls),
    )
    conn.commit()
    return cur.rowcount


def start_scan(conn: sqlite3.Connection) -> int:
    """Insert a scan row with started_at=now, finished_at=NULL. Returns scan id."""
    cur = conn.execute(
        "INSERT INTO scans (started_at, finished_at) VALUES (?, NULL)",
        (_now(),),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    companies_scanned: int,
    postings_found: int,
    new_postings: int,
) -> None:
    """Sets finished_at=now and the three stat columns on the given scan row."""
    conn.execute(
        """
        UPDATE scans
        SET finished_at = ?, companies_scanned = ?, postings_found = ?, new_postings = ?
        WHERE id = ?
        """,
        (_now(), companies_scanned, postings_found, new_postings, scan_id),
    )
    conn.commit()


def get_contacts_by_company(conn: sqlite3.Connection, normalized_name: str) -> List[dict]:
    """Contacts whose raw company, normalized, equals `normalized_name`."""
    from .normalize import normalize_company_name

    rows = conn.execute("SELECT * FROM contacts").fetchall()
    return [dict(r) for r in rows if normalize_company_name(r["company"]) == normalized_name]


def get_open_postings(conn: sqlite3.Connection) -> List[dict]:
    """All postings with status='open', each including a 'company_name' key."""
    rows = conn.execute(
        """
        SELECT postings.*, companies.name AS company_name
        FROM postings
        JOIN companies ON postings.company_id = companies.id
        WHERE postings.status = 'open'
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_companies_with_contacts(conn: sqlite3.Connection) -> List[dict]:
    """One dict per distinct normalized company present in contacts, with a contact_count.
    Companies with zero contacts are omitted (there's nothing to aggregate over for them).
    """
    from .normalize import normalize_company_name

    rows = conn.execute("SELECT company FROM contacts").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        key = normalize_company_name(r["company"])
        counts[key] = counts.get(key, 0) + 1
    return [{"company": name, "contact_count": count} for name, count in counts.items()]

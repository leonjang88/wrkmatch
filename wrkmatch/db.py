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
    url TEXT,
    email TEXT,
    rating TEXT NOT NULL DEFAULT 'ok',
    UNIQUE(first_name, last_name, company, source)
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ats_platform TEXT,
    ats_slug TEXT,
    last_scanned TEXT,
    priority TEXT NOT NULL DEFAULT 'normal'
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
    status TEXT NOT NULL DEFAULT 'open',
    user_status TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    companies_scanned INTEGER,
    postings_found INTEGER,
    new_postings INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# columns added after the original (v1) schema, keyed by table.
# Applied via idempotent ALTER TABLE so existing v1 databases pick them up.
_MIGRATED_COLUMNS = {
    "contacts": [
        ("url", "TEXT"),
        ("email", "TEXT"),
        ("rating", "TEXT NOT NULL DEFAULT 'ok'"),
    ],
    "companies": [
        ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
    ],
    "postings": [
        ("user_status", "TEXT NOT NULL DEFAULT 'none'"),
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns introduced after the v1 schema to an existing database.
    Uses PRAGMA table_info to detect what's missing, so it's safe to call
    repeatedly and against both fresh (already-current) and older v1 databases.
    """
    for table, columns in _MIGRATED_COLUMNS.items():
        existing = _existing_columns(conn, table)
        for col_name, col_def in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
    conn.commit()


def init_db(path) -> sqlite3.Connection:
    """Open (creating if needed) the wrkmatch sqlite db and apply the schema.
    Safe to call repeatedly against the same path, including against an
    existing v1 database that predates the settings table and the
    url/email/rating/priority/user_status columns.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def upsert_contacts(conn: sqlite3.Connection, contacts: List[dict], source: str) -> int:
    """Insert contacts, skipping ones that already exist for the same
    (first_name, last_name, company, source). Returns count newly inserted.

    When a contact is skipped as a dupe, its url/email are backfilled onto the
    existing row wherever that row's url/email are still NULL — existing
    non-NULL url/email are never overwritten.
    """
    inserted = 0
    for c in contacts:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO contacts
                (first_name, last_name, company, position, connected_on, source, url, email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["first_name"],
                c["last_name"],
                c["company"],
                c.get("position"),
                c.get("connected_on"),
                source,
                c.get("url"),
                c.get("email"),
            ),
        )
        if cur.rowcount:
            inserted += cur.rowcount
        else:
            conn.execute(
                """
                UPDATE contacts
                SET url = COALESCE(url, ?), email = COALESCE(email, ?)
                WHERE first_name = ? AND last_name = ? AND company = ? AND source = ?
                """,
                (c.get("url"), c.get("email"), c["first_name"], c["last_name"], c["company"], source),
            )
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


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Look up a settings value by key. Returns `default` when the key is unset."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or update a settings value by key."""
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


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

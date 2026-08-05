from __future__ import annotations
import sqlite3
from typing import Optional

import requests

from .ats_clients import DEFAULT_HEADERS
from .db import get_open_postings, mark_postings_status

CLOSED_SIGNALS: dict[str, list[str]] = {
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


def _signals_for(source: Optional[str]) -> list[str]:
    if source is None:
        signals: list[str] = []
        for phrases in CLOSED_SIGNALS.values():
            signals.extend(phrases)
        return signals
    return CLOSED_SIGNALS["generic"] + CLOSED_SIGNALS.get(source, [])


def check_posting(url: str, source: Optional[str] = None, timeout: int = 6) -> str:
    """Fetch `url` and classify it as 'open', 'closed', or 'gone'."""
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    except requests.RequestException:
        return "open"

    if r.status_code in (404, 410):
        return "gone"
    if r.status_code >= 500:
        return "open"
    if r.status_code == 200:
        body = r.text.lower()
        if any(phrase in body for phrase in _signals_for(source)):
            return "closed"
        return "open"
    return "open"


def validate_postings(conn: sqlite3.Connection, limit: Optional[int] = None) -> dict:
    """Re-check open postings against their live URLs and update their status."""
    postings = get_open_postings(conn)
    if limit is not None:
        postings = postings[:limit]

    closed_urls: list[str] = []
    gone_urls: list[str] = []
    still_open = 0

    for posting in postings:
        result = check_posting(posting["url"], source=posting.get("ats_platform"))
        if result == "closed":
            closed_urls.append(posting["url"])
        elif result == "gone":
            gone_urls.append(posting["url"])
        else:
            still_open += 1

    if closed_urls:
        mark_postings_status(conn, closed_urls, "closed")
    if gone_urls:
        mark_postings_status(conn, gone_urls, "gone")

    return {
        "checked": len(postings),
        "closed": len(closed_urls),
        "gone": len(gone_urls),
        "still_open": still_open,
    }

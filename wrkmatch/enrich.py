from __future__ import annotations

import html as html_module
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

from .ats_clients import DEFAULT_HEADERS

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(raw: Optional[str]) -> Optional[str]:
    """HTML-unescape entities and drop tags, collapsing whitespace. None/empty in, None out."""
    if not raw:
        return None
    text = html_module.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


@dataclass
class EnrichmentSource:
    """What fetch_posting_detail() found for one posting.

    `text` is the plain-text job description (for extract_salary/extract_remote
    to run over). native_* fields carry structured data straight from the ATS
    API when that API exposes it (Lever's salaryRange, Ashby's compensation
    summary) -- those beat regex extraction whenever present, since they're
    ground truth rather than a guess.
    """

    text: Optional[str] = None
    native_salary_min: Optional[int] = None
    native_salary_max: Optional[int] = None
    native_currency: Optional[str] = None
    native_remote: Optional[str] = None


# --- salary extraction -------------------------------------------------------

_SALARY_MIN = 20_000
_SALARY_MAX = 2_000_000

_UP_TO_RE = re.compile(r"\b(up to|maximum of|max of|no more than)\b", re.IGNORECASE)
# Deliberately does NOT include a bare "from" -- too common in unrelated
# phrases ("work from home") to safely signal "this is a minimum-only salary".
_STARTING_AT_RE = re.compile(r"\b(starting at|starting from|minimum of|at least)\b", re.IGNORECASE)

# Two alternatives: a $-prefixed amount (commas/decimal optional, "k" suffix
# optional), or a bare comma-grouped amount ("150,000") with no $ -- the bare
# form is only trusted as money at all once the caller has confirmed the
# surrounding text carries a USD signal (see extract_salary).
_MONEY_RE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
    r"|(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*([kK])?"
)


def extract_salary(text: Optional[str]) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Pull a USD salary range out of free-text job description content.

    Recognizes "$150,000 - $180,000", "$150k-$180k" (hyphen/en-dash/em-dash),
    "150,000-180,000 USD" (bare numbers, trusted as money once "USD" appears
    anywhere in the text), "$185,000.00 Max: $220,000.00" (two $ amounts with
    other text between them), and single values ("$160,000" -> min=max, "up to
    $170k" -> (None, max)). Amounts under $20,000 or over $2,000,000 (after
    k-expansion) are treated as not-a-salary and the whole match is rejected.
    A min > max pair is swapped. Currency is always "USD" (the only one this
    parses) when anything matches; otherwise returns (None, None, None).
    """
    if not text:
        return (None, None, None)

    has_dollar = "$" in text
    has_usd_word = bool(re.search(r"\bUSD\b", text, re.IGNORECASE))
    if not has_dollar and not has_usd_word:
        return (None, None, None)

    amounts = []
    for m in _MONEY_RE.finditer(text):
        if m.group(1) is not None:
            raw, k = m.group(1), m.group(2)
        else:
            raw, k = m.group(3), m.group(4)
        val = float(raw.replace(",", ""))
        if k:
            val *= 1000
        amounts.append(int(round(val)))

    if not amounts:
        return (None, None, None)

    def _valid(v: int) -> bool:
        return _SALARY_MIN <= v <= _SALARY_MAX

    if len(amounts) == 1:
        val = amounts[0]
        if not _valid(val):
            return (None, None, None)
        if _UP_TO_RE.search(text):
            return (None, val, "USD")
        if _STARTING_AT_RE.search(text):
            return (val, None, "USD")
        return (val, val, "USD")

    lo, hi = amounts[0], amounts[1]
    if not (_valid(lo) and _valid(hi)):
        return (None, None, None)
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi, "USD")


# --- remote extraction --------------------------------------------------------

_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_LOCATION_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_TEXT_REMOTE_RE = re.compile(r"fully remote|remote[- ]first|100%\s*remote", re.IGNORECASE)
_ONSITE_RE = re.compile(r"on[- ]?site only|in[- ]?office", re.IGNORECASE)


def extract_remote(text: Optional[str], location: Optional[str]) -> Optional[str]:
    """Classify a posting as 'remote' | 'hybrid' | 'onsite' | None.

    Precedence: hybrid beats remote whenever a hybrid signal appears anywhere
    (in the location field OR the description text) -- hybrid is the more
    restrictive, more specific truth, so it wins even over an explicit
    location of "Remote" combined with "hybrid" language in the description
    (a location field that says "Remote" is often just an ATS default, not a
    precise claim). Only once hybrid is ruled out do we check for remote
    (location field says "remote", or the text uses strong remote-only
    phrasing like "fully remote"/"remote-first"/"100% remote" -- a bare
    "remote" mention in the description alone is too weak/ambiguous to count,
    since many hybrid/onsite postings mention "remote" in passing). Then
    onsite-only phrasing. Otherwise None (unknown).
    """
    text = text or ""
    location = location or ""

    if _HYBRID_RE.search(text) or _HYBRID_RE.search(location):
        return "hybrid"
    if _LOCATION_REMOTE_RE.search(location) or _TEXT_REMOTE_RE.search(text):
        return "remote"
    if _ONSITE_RE.search(text) or _ONSITE_RE.search(location):
        return "onsite"
    return None


# --- per-platform detail fetch -------------------------------------------------

def _parse_greenhouse_url(url: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if not m:
        m = re.search(r"([a-zA-Z0-9-]+)\.greenhouse\.io/jobs/(\d+)", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _parse_lever_url(url: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"jobs\.lever\.co/([^/]+)/([a-zA-Z0-9-]+)", url)
    if not m:
        return None
    return m.group(1), m.group(2)


_WORKDAY_URL_RE = re.compile(r"^https://([^.]+)\.([^.]+)\.myworkdayjobs\.com/en-US/([^/]+)(/.+)$")


def _parse_workday_url(url: str) -> Optional[Tuple[str, str, str, str]]:
    m = _WORKDAY_URL_RE.match(url)
    if not m:
        return None
    return m.groups()  # type: ignore[return-value]


def _parse_smartrecruiters_url(url: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"jobs\.smartrecruiters\.com/([^/]+)/([^/?]+)", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _parse_ashby_url(url: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"(?:jobs\.)?ashbyhq\.com/([^/]+)/([0-9a-fA-F-]{6,})", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _fetch_greenhouse(url: str, timeout: int) -> Optional[EnrichmentSource]:
    parsed = _parse_greenhouse_url(url)
    if parsed is None:
        return None
    slug, job_id = parsed
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return EnrichmentSource(text=_strip_html(data.get("content")))


def _fetch_lever(url: str, timeout: int) -> Optional[EnrichmentSource]:
    parsed = _parse_lever_url(url)
    if parsed is None:
        return None
    slug, posting_id = parsed
    api_url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
    r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    text = data.get("descriptionPlain") or _strip_html(data.get("description"))
    salary_range = data.get("salaryRange")
    native_min = native_max = native_currency = None
    if isinstance(salary_range, dict):
        native_min = salary_range.get("min")
        native_max = salary_range.get("max")
        native_currency = salary_range.get("currency")
    return EnrichmentSource(
        text=text,
        native_salary_min=native_min,
        native_salary_max=native_max,
        native_currency=native_currency,
    )


def _fetch_workday(url: str, timeout: int) -> Optional[EnrichmentSource]:
    parsed = _parse_workday_url(url)
    if parsed is None:
        return None
    slug, host, site, external_path = parsed
    api_url = f"https://{slug}.{host}.myworkdayjobs.com/wday/cxs/{slug}/{site}{external_path}"
    r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    desc = (data.get("jobPostingInfo") or {}).get("jobDescription")
    return EnrichmentSource(text=_strip_html(desc))


def _fetch_smartrecruiters(url: str, timeout: int) -> Optional[EnrichmentSource]:
    parsed = _parse_smartrecruiters_url(url)
    if parsed is None:
        return None
    slug, posting_id = parsed
    api_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
    r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    sections = ((data.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for sec in sections.values():
        if isinstance(sec, dict) and sec.get("text"):
            stripped = _strip_html(sec["text"])
            if stripped:
                parts.append(stripped)
    return EnrichmentSource(text=" ".join(parts) or None)


def _fetch_ashby(url: str, timeout: int) -> Optional[EnrichmentSource]:
    parsed = _parse_ashby_url(url)
    if parsed is None:
        return None
    slug, job_id = parsed
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if job is None:
        return None
    text = _strip_html(job.get("descriptionHtml"))
    comp = job.get("compensation") or {}
    components = comp.get("summaryComponents") or []
    native_min = native_max = native_currency = None
    if components:
        first = components[0]
        native_min = first.get("minValue")
        native_max = first.get("maxValue")
        native_currency = first.get("currencyCode")
    return EnrichmentSource(
        text=text,
        native_salary_min=native_min,
        native_salary_max=native_max,
        native_currency=native_currency,
    )


# Platforms whose list payload is already everything we get -- no separate
# detail endpoint worth calling.
_NO_DETAIL_PLATFORMS = {"recruitee", "personio", "workable"}

_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "workday": _fetch_workday,
    "smartrecruiters": _fetch_smartrecruiters,
    "ashby": _fetch_ashby,
}


def fetch_posting_detail(
    url: str, ats_platform: Optional[str], timeout: int = 8
) -> Optional[EnrichmentSource]:
    """Fetch and extract detail-page content for one posting, keyed by ATS platform.

    Returns None when there's nothing more to learn -- either the platform
    doesn't expose a detail endpoint worth hitting (recruitee/personio/workable:
    their list payload already is everything we get), the platform is
    unknown/None, or `url` doesn't match the shape expected for `ats_platform`
    (can't derive the board slug / posting id from it), or the API responded
    but with a non-200 status or a body that isn't JSON. All of those are
    permanent for this posting -- retrying won't help -- so callers should
    treat None the same as "fetched, nothing found".

    Raises requests.RequestException (timeout, connection error, DNS failure,
    ...) on an actual network-level failure, uncaught, so callers can tell
    that apart from the permanent no-detail cases above and retry later.
    """
    if ats_platform in _NO_DETAIL_PLATFORMS or ats_platform not in _FETCHERS:
        return None
    return _FETCHERS[ats_platform](url, timeout)


def enrich_posting(conn: sqlite3.Connection, posting_row) -> bool:
    """Fetch detail, extract salary/remote, and persist onto one postings row.

    posting_row must support dict-style access to at least "id", "url",
    "ats_platform", and "location" (a sqlite3.Row or plain dict both work).

    Returns True and sets enriched_at=now when the fetch completed (even if
    nothing was extracted from it -- enriched_at is still stamped so this
    posting isn't refetched forever). Returns False, leaving enriched_at NULL,
    only on an actual network error, so the next enrichment run retries it.
    """
    url = posting_row["url"]
    platform = posting_row["ats_platform"]
    location = posting_row["location"]

    try:
        source = fetch_posting_detail(url, platform)
    except requests.RequestException:
        return False

    now = _now()

    if source is None:
        conn.execute(
            "UPDATE postings SET enriched_at = ? WHERE id = ?", (now, posting_row["id"])
        )
        conn.commit()
        return True

    text = source.text or ""

    if source.native_salary_min is not None or source.native_salary_max is not None:
        salary_min = source.native_salary_min
        salary_max = source.native_salary_max
        salary_currency = source.native_currency or "USD"
    else:
        salary_min, salary_max, salary_currency = extract_salary(text)

    remote = source.native_remote or extract_remote(text, location)

    conn.execute(
        """
        UPDATE postings
        SET salary_min = ?, salary_max = ?, salary_currency = ?, remote = ?, enriched_at = ?
        WHERE id = ?
        """,
        (salary_min, salary_max, salary_currency, remote, now, posting_row["id"]),
    )
    conn.commit()
    return True

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from wrkmatch.db import get_contacts_by_company, get_setting, init_db, set_setting
from wrkmatch.normalize import normalize_company_name

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"

RATING_POINTS = {"strong": 2, "ok": 1, "weak": 0}
DEFAULT_WEIGHTS = {"contacts": 3.0, "postings": 1.0, "freshness": 2.0}
PRIORITY_RANK = {"normal": 0, "deprioritized": 1, "hidden": 2}
ATS_PLATFORMS = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "smartrecruiters",
    "workday",
    "personio",
)
FRESHNESS_WINDOW = timedelta(days=7)

METRO_PATTERNS = {
    "boston": r"boston|cambridge|somerville|massachusetts|\bma\b",
}
DEFAULT_FILTERS = {
    "location_enabled": True,
    "include_remote": True,
    "metro": "boston",
    "salary_min": None,
    "include_unknown_salary": True,
}


class PriorityIn(BaseModel):
    priority: Literal["normal", "deprioritized", "hidden"]


class AtsIn(BaseModel):
    platform: Literal[ATS_PLATFORMS]  # type: ignore[valid-type]
    slug: str


class RatingIn(BaseModel):
    rating: Literal["strong", "ok", "weak"]


class UserStatusIn(BaseModel):
    user_status: Literal["none", "done", "ignored"]


class WeightsIn(BaseModel):
    contacts: float
    postings: float
    freshness: float


class FiltersIn(BaseModel):
    location_enabled: bool
    include_remote: bool
    metro: str
    salary_min: Optional[int] = None
    include_unknown_salary: bool

    @field_validator("metro")
    @classmethod
    def _metro_known(cls, v: str) -> str:
        if v not in METRO_PATTERNS:
            raise ValueError(f"unknown metro: {v!r}")
        return v


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _search_url(name: str) -> str:
    query = f'"{name}" careers'
    return "https://www.google.com/search?q=" + urllib.parse.quote(query)


def _get_weights(conn: sqlite3.Connection) -> dict:
    raw = get_setting(conn, "weights")
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return dict(DEFAULT_WEIGHTS)
    weights = dict(DEFAULT_WEIGHTS)
    for key in DEFAULT_WEIGHTS:
        if key in data:
            weights[key] = float(data[key])
    return weights


def _score_components(weights: dict, contact_points: int, open_postings: int, new_postings_7d: int):
    components = {
        "contacts": weights["contacts"] * contact_points,
        "postings": weights["postings"] * open_postings,
        "freshness": weights["freshness"] * new_postings_7d,
    }
    score = round(sum(components.values()), 1)
    return score, components


def _get_filters(conn: sqlite3.Connection) -> dict:
    raw = get_setting(conn, "filters")
    if not raw:
        return dict(DEFAULT_FILTERS)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return dict(DEFAULT_FILTERS)
    filters = dict(DEFAULT_FILTERS)
    for key in DEFAULT_FILTERS:
        if key in data:
            filters[key] = data[key]
    return filters


def _location_matches(location: Optional[str], remote: Optional[str], metro: str, include_remote: bool) -> bool:
    loc = location or ""
    if remote == "remote":
        return True
    if include_remote and re.search(r"\bremote\b", loc, re.IGNORECASE):
        return True
    pattern = METRO_PATTERNS.get(metro)
    if pattern and re.search(pattern, loc, re.IGNORECASE):
        return True
    return False


def _salary_matches(
    salary_min: Optional[int], salary_max: Optional[int], filter_salary_min: Optional[int], include_unknown_salary: bool
) -> bool:
    if filter_salary_min is None:
        return True
    if salary_min is None and salary_max is None:
        return include_unknown_salary
    lo = salary_min if salary_min is not None else salary_max
    hi = salary_max if salary_max is not None else salary_min
    return hi >= filter_salary_min


def _posting_matches_filters(
    location: Optional[str],
    remote: Optional[str],
    salary_min: Optional[int],
    salary_max: Optional[int],
    filters: dict,
    bypass_location: bool = False,
) -> bool:
    """Whether one posting satisfies the current global filters.

    location part: skipped entirely when filters["location_enabled"] is False
    or `bypass_location` is set (the ?all_locations=1 query param) -- true.
    Otherwise true iff the posting is remote (remote column, or include_remote
    and the location text says "remote"), or its location text matches the
    configured metro pattern.

    salary part: always evaluated (bypass_location does not affect it). True
    when filters["salary_min"] is unset, or the posting's [min,max] range
    (missing bound defaulted to the other) overlaps [salary_min, inf), or the
    posting has no salary data at all and include_unknown_salary is set.
    """
    if filters.get("location_enabled") and not bypass_location:
        location_ok = _location_matches(location, remote, filters["metro"], filters["include_remote"])
    else:
        location_ok = True
    salary_ok = _salary_matches(salary_min, salary_max, filters.get("salary_min"), filters["include_unknown_salary"])
    return location_ok and salary_ok


def create_app(db_path: str) -> FastAPI:
    db_path = str(db_path)
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="wrkmatch")
    app.state.db_path = db_path

    def _conn() -> sqlite3.Connection:
        return init_db(app.state.db_path)

    @app.get("/")
    def index():
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return JSONResponse({"message": "wrkmatch UI not built yet"})

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/summary")
    def summary():
        conn = _conn()
        try:
            contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            companies_total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            companies_with_ats = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE ats_platform IS NOT NULL"
            ).fetchone()[0]
            open_postings = conn.execute(
                "SELECT COUNT(*) FROM postings WHERE status = 'open'"
            ).fetchone()[0]

            filters = _get_filters(conn)
            matching_postings = 0
            for p in conn.execute(
                "SELECT location, remote, salary_min, salary_max FROM postings WHERE status = 'open'"
            ).fetchall():
                if _posting_matches_filters(p["location"], p["remote"], p["salary_min"], p["salary_max"], filters):
                    matching_postings += 1

            scan_row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            if scan_row is None:
                last_scan = None
            else:
                last_scan = {
                    "finished_at": scan_row["finished_at"],
                    "companies_scanned": scan_row["companies_scanned"],
                    "postings_found": scan_row["postings_found"],
                    "new_postings": scan_row["new_postings"],
                }

            return {
                "contacts": contacts,
                "companies_total": companies_total,
                "companies_with_ats": companies_with_ats,
                "open_postings": open_postings,
                "matching_postings": matching_postings,
                "last_scan": last_scan,
                "weights": _get_weights(conn),
            }
        finally:
            conn.close()

    @app.get("/api/companies")
    def list_companies(include_hidden: int = 0, all_locations: int = 0):
        conn = _conn()
        try:
            weights = _get_weights(conn)
            filters = _get_filters(conn)
            companies = conn.execute("SELECT * FROM companies").fetchall()

            contacts_by_norm: dict[str, list[sqlite3.Row]] = {}
            for c in conn.execute("SELECT * FROM contacts").fetchall():
                key = normalize_company_name(c["company"])
                contacts_by_norm.setdefault(key, []).append(c)

            postings_by_company: dict[int, list[dict]] = {}
            for p in conn.execute(
                "SELECT company_id, first_seen, location, remote, salary_min, salary_max FROM postings "
                "WHERE status = 'open' AND user_status != 'ignored'"
            ).fetchall():
                postings_by_company.setdefault(p["company_id"], []).append(dict(p))

            cutoff = datetime.now(timezone.utc) - FRESHNESS_WINDOW

            result = []
            companies_excluded_by_filters = 0
            for company in companies:
                if company["priority"] == "hidden" and not include_hidden:
                    continue

                all_open = postings_by_company.get(company["id"], [])
                if not all_open:
                    continue

                matching = [
                    p
                    for p in all_open
                    if _posting_matches_filters(
                        p["location"], p["remote"], p["salary_min"], p["salary_max"],
                        filters, bypass_location=bool(all_locations),
                    )
                ]
                if not matching:
                    companies_excluded_by_filters += 1
                    continue

                open_postings = len(matching)
                new_postings_7d = sum(1 for p in matching if _parse_iso(p["first_seen"]) >= cutoff)

                company_contacts = contacts_by_norm.get(company["name"], [])
                contact_count = len(company_contacts)
                contact_points = sum(RATING_POINTS.get(c["rating"], 0) for c in company_contacts)

                score, components = _score_components(weights, contact_points, open_postings, new_postings_7d)

                result.append(
                    {
                        "id": company["id"],
                        "name": company["name"],
                        "priority": company["priority"],
                        "ats_platform": company["ats_platform"],
                        "contact_count": contact_count,
                        "contact_points": contact_points,
                        "open_postings": open_postings,
                        "new_postings_7d": new_postings_7d,
                        "score": score,
                        "components": components,
                    }
                )

            result.sort(key=lambda r: (PRIORITY_RANK.get(r["priority"], 1), -r["score"], r["name"]))
            return {
                "companies": result,
                "filters": filters,
                "companies_excluded_by_filters": companies_excluded_by_filters,
            }
        finally:
            conn.close()

    @app.get("/api/companies/{company_id}")
    def company_detail(company_id: int):
        conn = _conn()
        try:
            company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            if company is None:
                raise HTTPException(status_code=404, detail="Company not found")

            contacts = get_contacts_by_company(conn, company["name"])
            rating_rank = {"strong": 0, "ok": 1, "weak": 2}
            contacts.sort(key=lambda c: (rating_rank.get(c["rating"], 1), c["last_name"] or ""))
            contacts_out = [
                {
                    "id": c["id"],
                    "first_name": c["first_name"],
                    "last_name": c["last_name"],
                    "position": c["position"],
                    "rating": c["rating"],
                    "url": c["url"],
                    "email": c["email"],
                    "source": c["source"],
                    "connected_on": c["connected_on"],
                }
                for c in contacts
            ]

            postings = conn.execute(
                "SELECT * FROM postings WHERE company_id = ? AND status = 'open' "
                "ORDER BY first_seen DESC",
                (company_id,),
            ).fetchall()
            cutoff = datetime.now(timezone.utc) - FRESHNESS_WINDOW
            filters = _get_filters(conn)
            postings_out = [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "url": p["url"],
                    "location": p["location"],
                    "department": p["department"],
                    "posted_at": p["posted_at"],
                    "salary_min": p["salary_min"],
                    "salary_max": p["salary_max"],
                    "salary_currency": p["salary_currency"],
                    "remote": p["remote"],
                    "first_seen": p["first_seen"],
                    "last_seen": p["last_seen"],
                    "status": p["status"],
                    "user_status": p["user_status"],
                    "is_new": _parse_iso(p["first_seen"]) >= cutoff,
                    "matches_filters": _posting_matches_filters(
                        p["location"], p["remote"], p["salary_min"], p["salary_max"], filters
                    ),
                }
                for p in postings
            ]

            return {
                "id": company["id"],
                "name": company["name"],
                "priority": company["priority"],
                "ats_platform": company["ats_platform"],
                "ats_slug": company["ats_slug"],
                "last_scanned": company["last_scanned"],
                "contacts": contacts_out,
                "postings": postings_out,
            }
        finally:
            conn.close()

    @app.get("/api/postings")
    def list_postings(all: int = 0):
        """Flat, jobs-first listing across every company.

        Base scope: open postings from non-hidden companies. Companies with
        priority='hidden' are excluded from the base scope ALWAYS, in every
        mode including `all=1` -- hidden means the user deliberately ignored
        that company, and the Jobs "show all" reveal is for filtered-out or
        done/ignored rows, not for resurfacing ignored companies. The
        Companies tab's explicit hidden-reveal (?include_hidden=1) remains
        the one intentional path back to a hidden company.

        `postings` in the response is further narrowed, by default, to rows
        where matches_filters is true AND user_status == 'none' -- the
        "things worth looking at right now" view. `all=1` skips that
        narrowing (every row in the hidden-free base scope is returned,
        including non-matching and done/ignored ones); each row still
        carries user_status and matches_filters so the client can
        group/dim them.

        `total` is the size of the (always hidden-free) base scope, before
        the default-mode matches_filters/user_status narrowing; `matching`
        is how many of those have matches_filters true, regardless of
        user_status -- both are reported the same way under `all=1` too, so
        the client can show "N of M" context even while looking at
        everything.
        """
        conn = _conn()
        try:
            filters = _get_filters(conn)

            contacts_by_norm: dict[str, list[sqlite3.Row]] = {}
            for c in conn.execute("SELECT * FROM contacts").fetchall():
                key = normalize_company_name(c["company"])
                contacts_by_norm.setdefault(key, []).append(c)

            cutoff = datetime.now(timezone.utc) - FRESHNESS_WINDOW

            query = (
                "SELECT postings.*, companies.name AS company_name, "
                "companies.priority AS company_priority "
                "FROM postings JOIN companies ON postings.company_id = companies.id "
                "WHERE postings.status = 'open' AND companies.priority != 'hidden'"
            )
            rows = conn.execute(query).fetchall()

            total = len(rows)
            matching = 0
            enriched = []
            for p in rows:
                matches = _posting_matches_filters(
                    p["location"], p["remote"], p["salary_min"], p["salary_max"], filters
                )
                if matches:
                    matching += 1

                company_contacts = contacts_by_norm.get(p["company_name"], [])
                contact_count = len(company_contacts)
                contact_points = sum(RATING_POINTS.get(c["rating"], 0) for c in company_contacts)

                enriched.append(
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "url": p["url"],
                        "location": p["location"],
                        "department": p["department"],
                        "posted_at": p["posted_at"],
                        "salary_min": p["salary_min"],
                        "salary_max": p["salary_max"],
                        "salary_currency": p["salary_currency"],
                        "remote": p["remote"],
                        "first_seen": p["first_seen"],
                        "user_status": p["user_status"],
                        "is_new": _parse_iso(p["first_seen"]) >= cutoff,
                        "matches_filters": matches,
                        "company_id": p["company_id"],
                        "company_name": p["company_name"],
                        "company_priority": p["company_priority"],
                        "contact_count": contact_count,
                        "contact_points": contact_points,
                    }
                )

            if not all:
                enriched = [p for p in enriched if p["matches_filters"] and p["user_status"] == "none"]

            def _sort_date(p: dict) -> datetime:
                raw = p["posted_at"] or p["first_seen"]
                try:
                    return _parse_iso(raw)
                except (ValueError, TypeError):
                    return _parse_iso(p["first_seen"])

            enriched.sort(key=lambda p: (0 if p["is_new"] else 1, -_sort_date(p).timestamp(), p["title"] or ""))

            return {"postings": enriched, "total": total, "matching": matching}
        finally:
            conn.close()

    @app.get("/api/coverage")
    def coverage(include_hidden: int = 0):
        conn = _conn()
        try:
            companies_total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            companies_with_ats = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE ats_platform IS NOT NULL"
            ).fetchone()[0]

            contacts_by_norm: dict[str, int] = {}
            for c in conn.execute("SELECT company FROM contacts").fetchall():
                key = normalize_company_name(c["company"])
                contacts_by_norm[key] = contacts_by_norm.get(key, 0) + 1

            no_ats = []
            hidden_count = 0
            for company in conn.execute(
                "SELECT * FROM companies WHERE ats_platform IS NULL"
            ).fetchall():
                contact_count = contacts_by_norm.get(company["name"], 0)
                if contact_count < 1:
                    continue
                if company["priority"] == "hidden":
                    hidden_count += 1
                    if not include_hidden:
                        continue
                no_ats.append(
                    {
                        "id": company["id"],
                        "name": company["name"],
                        "contact_count": contact_count,
                        "priority": company["priority"],
                        "search_url": _search_url(company["name"]),
                    }
                )
            no_ats.sort(key=lambda r: (-r["contact_count"], r["name"]))

            return {
                "companies_total": companies_total,
                "companies_with_ats": companies_with_ats,
                "no_ats": no_ats,
                "hidden_count": hidden_count,
            }
        finally:
            conn.close()

    @app.post("/api/companies/{company_id}/priority")
    def set_priority(company_id: int, body: PriorityIn):
        conn = _conn()
        try:
            row = conn.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Company not found")
            conn.execute("UPDATE companies SET priority = ? WHERE id = ?", (body.priority, company_id))
            conn.commit()
            return {"ok": True, "priority": body.priority}
        finally:
            conn.close()

    @app.post("/api/companies/{company_id}/ats")
    def set_ats(company_id: int, body: AtsIn):
        conn = _conn()
        try:
            row = conn.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Company not found")
            conn.execute(
                "UPDATE companies SET ats_platform = ?, ats_slug = ? WHERE id = ?",
                (body.platform, body.slug, company_id),
            )
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/contacts/{contact_id}/rating")
    def set_rating(contact_id: int, body: RatingIn):
        conn = _conn()
        try:
            row = conn.execute("SELECT id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Contact not found")
            conn.execute("UPDATE contacts SET rating = ? WHERE id = ?", (body.rating, contact_id))
            conn.commit()
            return {"ok": True, "rating": body.rating}
        finally:
            conn.close()

    @app.post("/api/postings/{posting_id}/user_status")
    def set_user_status(posting_id: int, body: UserStatusIn):
        conn = _conn()
        try:
            row = conn.execute("SELECT id FROM postings WHERE id = ?", (posting_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Posting not found")
            conn.execute(
                "UPDATE postings SET user_status = ? WHERE id = ?", (body.user_status, posting_id)
            )
            conn.commit()
            return {"ok": True, "user_status": body.user_status}
        finally:
            conn.close()

    @app.get("/api/settings/weights")
    def get_weights():
        conn = _conn()
        try:
            return _get_weights(conn)
        finally:
            conn.close()

    @app.put("/api/settings/weights")
    def put_weights(body: WeightsIn):
        conn = _conn()
        try:
            weights = {"contacts": body.contacts, "postings": body.postings, "freshness": body.freshness}
            set_setting(conn, "weights", json.dumps(weights))
            return {"ok": True, "weights": weights}
        finally:
            conn.close()

    @app.get("/api/settings/filters")
    def get_filters():
        conn = _conn()
        try:
            return _get_filters(conn)
        finally:
            conn.close()

    @app.put("/api/settings/filters")
    def put_filters(body: FiltersIn):
        conn = _conn()
        try:
            filters = {
                "location_enabled": body.location_enabled,
                "include_remote": body.include_remote,
                "metro": body.metro,
                "salary_min": body.salary_min,
                "include_unknown_salary": body.include_unknown_salary,
            }
            set_setting(conn, "filters", json.dumps(filters))
            return {"ok": True, "filters": filters}
        finally:
            conn.close()

    return app


app = create_app(os.environ.get("WRKMATCH_DB", str(REPO_ROOT / "data" / "wrkmatch.db")))

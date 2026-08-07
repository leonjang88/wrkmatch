from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
                "last_scan": last_scan,
                "weights": _get_weights(conn),
            }
        finally:
            conn.close()

    @app.get("/api/companies")
    def list_companies(include_hidden: int = 0):
        conn = _conn()
        try:
            weights = _get_weights(conn)
            companies = conn.execute("SELECT * FROM companies").fetchall()

            contacts_by_norm: dict[str, list[sqlite3.Row]] = {}
            for c in conn.execute("SELECT * FROM contacts").fetchall():
                key = normalize_company_name(c["company"])
                contacts_by_norm.setdefault(key, []).append(c)

            postings_by_company: dict[int, list[str]] = {}
            for p in conn.execute(
                "SELECT company_id, first_seen FROM postings "
                "WHERE status = 'open' AND user_status != 'ignored'"
            ).fetchall():
                postings_by_company.setdefault(p["company_id"], []).append(p["first_seen"])

            cutoff = datetime.now(timezone.utc) - FRESHNESS_WINDOW

            result = []
            for company in companies:
                if company["priority"] == "hidden" and not include_hidden:
                    continue

                first_seens = postings_by_company.get(company["id"], [])
                open_postings = len(first_seens)
                if open_postings == 0:
                    continue

                new_postings_7d = sum(1 for fs in first_seens if _parse_iso(fs) >= cutoff)

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
            return {"companies": result}
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
            postings_out = [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "url": p["url"],
                    "location": p["location"],
                    "first_seen": p["first_seen"],
                    "last_seen": p["last_seen"],
                    "status": p["status"],
                    "user_status": p["user_status"],
                    "is_new": _parse_iso(p["first_seen"]) >= cutoff,
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

    @app.get("/api/coverage")
    def coverage():
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
            for company in conn.execute(
                "SELECT * FROM companies WHERE ats_platform IS NULL"
            ).fetchall():
                if company["priority"] == "hidden":
                    continue
                contact_count = contacts_by_norm.get(company["name"], 0)
                if contact_count < 1:
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

    return app


app = create_app(os.environ.get("WRKMATCH_DB", str(REPO_ROOT / "data" / "wrkmatch.db")))

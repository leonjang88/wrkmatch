"""Interface contract for a NOT-YET-IMPLEMENTED module app/server.py.

app/server.py does not exist yet; these tests fail on import until it's
implemented. That's expected. The contract they define:

    create_app(db_path: str) -> FastAPI
        Builds the wrkmatch API. GET / serves app/static/index.html (or a
        200 placeholder if that file doesn't exist yet); /static is mounted
        to app/static. Every /api/* route below opens its own db connection
        against `db_path` (wrkmatch.db.init_db is idempotent/cheap to call
        per-request).

    Scoring (GET /api/companies): contact_points = sum over a company's
    contacts of {strong: 2, ok: 1, weak: 0}[rating]. components =
    {"contacts": w_contacts * contact_points, "postings": w_postings *
    open_postings, "freshness": w_freshness * new_postings_7d}; score =
    round(sum(components.values()), 1). Weights come from settings key
    "weights" (JSON), defaulting to {"contacts": 3.0, "postings": 1.0,
    "freshness": 2.0}. open_postings excludes user_status='ignored'.
    new_postings_7d = open, non-ignored postings with first_seen within the
    last 7 days (UTC).

See docstrings inline on each test for the endpoint it targets.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wrkmatch.db import init_db, record_posting, upsert_company, upsert_contacts
from wrkmatch.normalize import normalize_company_name

from app.server import create_app


# --- seeding helpers -----------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def set_contact_rating(conn, contact_id: int, rating: str) -> None:
    conn.execute("UPDATE contacts SET rating = ? WHERE id = ?", (rating, contact_id))
    conn.commit()


def set_company_priority(conn, company_id: int, priority: str) -> None:
    conn.execute("UPDATE companies SET priority = ? WHERE id = ?", (priority, company_id))
    conn.commit()


def set_posting_user_status(conn, posting_id: int, user_status: str) -> None:
    conn.execute("UPDATE postings SET user_status = ? WHERE id = ?", (user_status, posting_id))
    conn.commit()


def set_posting_first_seen(conn, posting_id: int, first_seen: str) -> None:
    conn.execute("UPDATE postings SET first_seen = ? WHERE id = ?", (first_seen, posting_id))
    conn.commit()


def contact_id_of(conn, first_name: str, last_name: str) -> int:
    row = conn.execute(
        "SELECT id FROM contacts WHERE first_name = ? AND last_name = ?", (first_name, last_name)
    ).fetchone()
    return row["id"]


def posting_id_of(conn, url: str) -> int:
    return conn.execute("SELECT id FROM postings WHERE url = ?", (url,)).fetchone()["id"]


def make_contact(first_name="Jane", last_name="Doe", company="Acme Widgets", position="PM"):
    return {"first_name": first_name, "last_name": last_name, "company": company, "position": position}


@pytest.fixture
def seed(tmp_path):
    """Fresh sqlite db + seeding connection. Yields the raw conn for direct
    seeding/inspection; tests build a TestClient separately against the same
    file so the API reads back what was seeded.
    """
    db_path = tmp_path / "wrkmatch_api_test.db"
    conn = init_db(db_path)
    yield conn, db_path
    conn.close()


def client_for(db_path) -> TestClient:
    return TestClient(create_app(str(db_path)))


# --- GET / -----------------------------------------------------------------

def test_index_returns_200_without_static_index_html(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_asset_references_all_resolve(seed):
    """Every src/href asset reference in the served index.html must resolve
    to a 200 via the same TestClient. Regression test for a bug where
    index.html used relative paths ("style.css", "app.js") while assets are
    mounted at /static/, so the browser 404'd on both and no JS ever ran.
    """
    _, db_path = seed
    client = client_for(db_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")

    refs = re.findall(r'\b(?:src|href)="([^"]+)"', resp.text)
    asset_refs = [r for r in refs if not r.startswith("#") and not r.startswith("http")]
    assert asset_refs, "expected index.html to reference at least one local asset"
    for ref in asset_refs:
        asset_resp = client.get(ref)
        assert asset_resp.status_code == 200, f"asset reference {ref!r} returned {asset_resp.status_code}"


# --- GET /api/summary --------------------------------------------------------

def test_summary_counts_with_no_scan_yet(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(conn, [make_contact()], source="leon")
    record_posting(conn, company_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1")

    client = client_for(db_path)
    body = client.get("/api/summary").json()

    assert body["contacts"] == 1
    assert body["companies_total"] == 1
    assert body["companies_with_ats"] == 0
    assert body["open_postings"] == 1
    assert body["last_scan"] is None
    assert body["weights"] == {"contacts": 3.0, "postings": 1.0, "freshness": 2.0}


def test_summary_reports_latest_scan_and_ats_count(seed):
    from wrkmatch.db import finish_scan, start_scan

    conn, db_path = seed
    upsert_company(conn, "acme widgets", ats_platform="greenhouse", ats_slug="acme")
    upsert_company(conn, "beta systems")

    scan_id_1 = start_scan(conn)
    finish_scan(conn, scan_id_1, companies_scanned=1, postings_found=1, new_postings=1)
    scan_id_2 = start_scan(conn)
    finish_scan(conn, scan_id_2, companies_scanned=2, postings_found=5, new_postings=2)

    client = client_for(db_path)
    body = client.get("/api/summary").json()

    assert body["companies_total"] == 2
    assert body["companies_with_ats"] == 1
    assert body["last_scan"]["companies_scanned"] == 2
    assert body["last_scan"]["postings_found"] == 5
    assert body["last_scan"]["new_postings"] == 2
    assert body["last_scan"]["finished_at"] is not None


# --- GET /api/companies ------------------------------------------------------

def test_companies_scoring_math_with_custom_weights(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(
        conn,
        [
            make_contact(first_name="A", last_name="Strong"),
            make_contact(first_name="B", last_name="Ok"),
            make_contact(first_name="C", last_name="Weak"),
        ],
        source="leon",
    )
    set_contact_rating(conn, contact_id_of(conn, "A", "Strong"), "strong")
    set_contact_rating(conn, contact_id_of(conn, "B", "Ok"), "ok")
    set_contact_rating(conn, contact_id_of(conn, "C", "Weak"), "weak")

    record_posting(conn, company_id, title="PM 1", url="https://boards.greenhouse.io/acme/jobs/1")
    record_posting(conn, company_id, title="PM 2", url="https://boards.greenhouse.io/acme/jobs/2")

    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('weights', ?)",
        (json.dumps({"contacts": 2.0, "postings": 5.0, "freshness": 0.0}),),
    )
    conn.commit()

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert len(body["companies"]) == 1
    company = body["companies"][0]

    # contact_points = strong(2) + ok(1) + weak(0) = 3
    assert company["contact_points"] == 3
    assert company["open_postings"] == 2
    assert company["components"]["contacts"] == pytest.approx(6.0)  # 2.0 * 3
    assert company["components"]["postings"] == pytest.approx(10.0)  # 5.0 * 2
    assert company["components"]["freshness"] == pytest.approx(0.0)
    assert company["score"] == pytest.approx(16.0)


def test_companies_ignored_postings_excluded_from_open_count_and_score(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(conn, [make_contact()], source="leon")
    record_posting(conn, company_id, title="PM 1", url="https://boards.greenhouse.io/acme/jobs/1")
    record_posting(conn, company_id, title="PM 2", url="https://boards.greenhouse.io/acme/jobs/2")
    set_posting_user_status(conn, posting_id_of(conn, "https://boards.greenhouse.io/acme/jobs/2"), "ignored")

    client = client_for(db_path)
    company = client_for(db_path).get("/api/companies").json()["companies"][0]
    assert company["open_postings"] == 1


def test_companies_without_open_postings_are_excluded(seed):
    conn, db_path = seed
    upsert_company(conn, "acme widgets")  # no postings at all
    upsert_contacts(conn, [make_contact()], source="leon")

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"] == []


def test_companies_all_postings_ignored_means_excluded(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(conn, [make_contact()], source="leon")
    record_posting(conn, company_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1")
    set_posting_user_status(conn, posting_id_of(conn, "https://boards.greenhouse.io/acme/jobs/1"), "ignored")

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"] == []


def test_companies_hidden_excluded_by_default_and_included_with_flag(seed):
    conn, db_path = seed
    normal_id = upsert_company(conn, "acme widgets")
    hidden_id = upsert_company(conn, "hidden co")
    set_company_priority(conn, hidden_id, "hidden")
    record_posting(conn, normal_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1")
    record_posting(conn, hidden_id, title="PM", url="https://boards.greenhouse.io/hidden/jobs/1")

    client = client_for(db_path)
    default_names = {c["name"] for c in client.get("/api/companies").json()["companies"]}
    assert default_names == {"acme widgets"}

    all_names = {c["name"] for c in client.get("/api/companies?include_hidden=1").json()["companies"]}
    assert all_names == {"acme widgets", "hidden co"}


def test_companies_deprioritized_sorts_below_normal_regardless_of_score(seed):
    conn, db_path = seed
    dep_id = upsert_company(conn, "high score deprioritized")
    normal_id = upsert_company(conn, "low score normal")
    set_company_priority(conn, dep_id, "deprioritized")

    # give the deprioritized company many more contacts/postings so its raw
    # score would outrank the normal one if priority weren't applied first
    upsert_contacts(
        conn,
        [make_contact(first_name=f"P{i}", last_name="X", company="high score deprioritized") for i in range(5)],
        source="leon",
    )
    upsert_contacts(conn, [make_contact(company="low score normal")], source="leon")
    record_posting(conn, dep_id, title="PM", url="https://boards.greenhouse.io/dep/jobs/1")
    record_posting(conn, normal_id, title="PM", url="https://boards.greenhouse.io/norm/jobs/1")

    client = client_for(db_path)
    names_in_order = [c["name"] for c in client.get("/api/companies").json()["companies"]]
    assert names_in_order == ["low score normal", "high score deprioritized"]


def test_companies_sorted_by_score_desc_then_name_asc(seed):
    conn, db_path = seed
    low_id = upsert_company(conn, "low co")
    high_id = upsert_company(conn, "high co")
    tie_a_id = upsert_company(conn, "tie a")
    tie_b_id = upsert_company(conn, "tie b")

    record_posting(conn, low_id, title="PM", url="https://x/low/1")
    record_posting(conn, high_id, title="PM", url="https://x/high/1")
    record_posting(conn, high_id, title="PM2", url="https://x/high/2")
    record_posting(conn, tie_a_id, title="PM", url="https://x/tiea/1")
    record_posting(conn, tie_b_id, title="PM", url="https://x/tieb/1")

    client = client_for(db_path)
    names_in_order = [c["name"] for c in client.get("/api/companies").json()["companies"]]
    # high co has 2 open (and new) postings, scoring above the rest; low/tie a/tie
    # b each have 1 open+new posting and tie on score, so they fall back to name asc
    assert names_in_order == ["high co", "low co", "tie a", "tie b"]


def test_companies_new_postings_7d_uses_first_seen_window(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Old", url="https://x/old")
    record_posting(conn, company_id, title="New", url="https://x/new")
    old_iso = iso(datetime.now(timezone.utc) - timedelta(days=10))
    new_iso = iso(datetime.now(timezone.utc) - timedelta(days=1))
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/old"), old_iso)
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/new"), new_iso)

    client = client_for(db_path)
    company = client.get("/api/companies").json()["companies"][0]
    assert company["open_postings"] == 2
    assert company["new_postings_7d"] == 1


# --- GET /api/companies/{id} --------------------------------------------------

def test_company_detail_contact_sort_and_is_new_and_postings_scope(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(
        conn,
        [
            make_contact(first_name="Z", last_name="Weakerson"),
            make_contact(first_name="A", last_name="Strongface"),
            make_contact(first_name="M", last_name="Middleman"),
            make_contact(first_name="B", last_name="Strongfoot"),
        ],
        source="leon",
    )
    set_contact_rating(conn, contact_id_of(conn, "Z", "Weakerson"), "weak")
    set_contact_rating(conn, contact_id_of(conn, "A", "Strongface"), "strong")
    set_contact_rating(conn, contact_id_of(conn, "M", "Middleman"), "ok")
    set_contact_rating(conn, contact_id_of(conn, "B", "Strongfoot"), "strong")

    record_posting(conn, company_id, title="Open New", url="https://x/new")
    record_posting(conn, company_id, title="Open Old", url="https://x/old")
    record_posting(conn, company_id, title="Closed", url="https://x/closed")
    from wrkmatch.db import mark_postings_status

    mark_postings_status(conn, ["https://x/closed"], "closed")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/new"), iso(datetime.now(timezone.utc)))
    set_posting_first_seen(
        conn, posting_id_of(conn, "https://x/old"), iso(datetime.now(timezone.utc) - timedelta(days=30))
    )

    client = client_for(db_path)
    body = client.get(f"/api/companies/{company_id}").json()

    contact_names = [(c["first_name"], c["rating"]) for c in body["contacts"]]
    # strong (last_name asc: Strongface before Strongfoot), then ok, then weak
    assert contact_names == [("A", "strong"), ("B", "strong"), ("M", "ok"), ("Z", "weak")]

    posting_urls = [p["url"] for p in body["postings"]]
    assert posting_urls == ["https://x/new", "https://x/old"]  # first_seen desc, closed excluded

    is_new_by_url = {p["url"]: p["is_new"] for p in body["postings"]}
    assert is_new_by_url["https://x/new"] is True
    assert is_new_by_url["https://x/old"] is False


def test_company_detail_404_for_unknown_id(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.get("/api/companies/999")
    assert resp.status_code == 404


# --- GET /api/coverage --------------------------------------------------------

def test_coverage_ranking_search_url_and_hidden_exclusion(seed):
    conn, db_path = seed
    # names deliberately avoid normalize_company_name's suffix stripping
    # (e.g. trailing " co"/"corp") so companies.name matches contacts.company as-is
    a_id = upsert_company(conn, "acme stealth")
    b_id = upsert_company(conn, "beta lowvis")
    hidden_id = upsert_company(conn, "gamma unlisted")
    has_ats_id = upsert_company(conn, "delta visible", ats_platform="greenhouse", ats_slug="x")
    set_company_priority(conn, hidden_id, "hidden")

    upsert_contacts(
        conn,
        [
            make_contact(first_name="P1", last_name="X", company="acme stealth"),
            make_contact(first_name="P2", last_name="X", company="acme stealth"),
            make_contact(first_name="P3", last_name="X", company="beta lowvis"),
            make_contact(first_name="P4", last_name="X", company="gamma unlisted"),
        ],
        source="leon",
    )

    client = client_for(db_path)
    body = client.get("/api/coverage").json()

    assert body["companies_total"] == 4
    assert body["companies_with_ats"] == 1

    names_in_order = [c["name"] for c in body["no_ats"]]
    assert names_in_order == ["acme stealth", "beta lowvis"]  # hidden + has_ats excluded

    no_ats_entry = body["no_ats"][0]
    expected_q = urllib.parse.quote('"acme stealth" careers')
    assert no_ats_entry["search_url"] == f"https://www.google.com/search?q={expected_q}"
    assert no_ats_entry["contact_count"] == 2


def test_coverage_excludes_companies_with_no_contacts(seed):
    conn, db_path = seed
    upsert_company(conn, "no contacts co")  # ats_platform None, but zero contacts

    client = client_for(db_path)
    body = client.get("/api/coverage").json()
    assert body["no_ats"] == []


# --- POST /api/companies/{id}/priority ---------------------------------------

def test_set_priority_happy_path_and_persistence(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    client = client_for(db_path)

    resp = client.post(f"/api/companies/{company_id}/priority", json={"priority": "deprioritized"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "priority": "deprioritized"}

    row = conn.execute("SELECT priority FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["priority"] == "deprioritized"


def test_set_priority_404_unknown_company(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.post("/api/companies/999/priority", json={"priority": "hidden"})
    assert resp.status_code == 404


def test_set_priority_422_invalid_value(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    client = client_for(db_path)
    resp = client.post(f"/api/companies/{company_id}/priority", json={"priority": "nonsense"})
    assert resp.status_code == 422


# --- POST /api/companies/{id}/ats ---------------------------------------------

def test_set_ats_happy_path_and_persistence(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    client = client_for(db_path)

    resp = client.post(f"/api/companies/{company_id}/ats", json={"platform": "lever", "slug": "acme"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    row = conn.execute(
        "SELECT ats_platform, ats_slug FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert (row["ats_platform"], row["ats_slug"]) == ("lever", "acme")


def test_set_ats_404_unknown_company(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.post("/api/companies/999/ats", json={"platform": "lever", "slug": "x"})
    assert resp.status_code == 404


def test_set_ats_422_invalid_platform(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    client = client_for(db_path)
    resp = client.post(f"/api/companies/{company_id}/ats", json={"platform": "bamboohr", "slug": "x"})
    assert resp.status_code == 422


# --- POST /api/contacts/{id}/rating -------------------------------------------

def test_set_rating_happy_path_and_persistence(seed):
    conn, db_path = seed
    upsert_contacts(conn, [make_contact()], source="leon")
    contact_id = contact_id_of(conn, "Jane", "Doe")
    client = client_for(db_path)

    resp = client.post(f"/api/contacts/{contact_id}/rating", json={"rating": "strong"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "rating": "strong"}

    row = conn.execute("SELECT rating FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    assert row["rating"] == "strong"


def test_set_rating_404_unknown_contact(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.post("/api/contacts/999/rating", json={"rating": "strong"})
    assert resp.status_code == 404


def test_set_rating_422_invalid_value(seed):
    conn, db_path = seed
    upsert_contacts(conn, [make_contact()], source="leon")
    contact_id = contact_id_of(conn, "Jane", "Doe")
    client = client_for(db_path)
    resp = client.post(f"/api/contacts/{contact_id}/rating", json={"rating": "amazing"})
    assert resp.status_code == 422


# --- POST /api/postings/{id}/user_status --------------------------------------

def test_set_user_status_happy_path_and_persistence(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1")
    posting_id = posting_id_of(conn, "https://x/1")
    client = client_for(db_path)

    resp = client.post(f"/api/postings/{posting_id}/user_status", json={"user_status": "done"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "user_status": "done"}

    row = conn.execute("SELECT user_status FROM postings WHERE id = ?", (posting_id,)).fetchone()
    assert row["user_status"] == "done"


def test_set_user_status_404_unknown_posting(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.post("/api/postings/999/user_status", json={"user_status": "done"})
    assert resp.status_code == 404


def test_set_user_status_422_invalid_value(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1")
    posting_id = posting_id_of(conn, "https://x/1")
    client = client_for(db_path)
    resp = client.post(f"/api/postings/{posting_id}/user_status", json={"user_status": "maybe"})
    assert resp.status_code == 422


# --- /api/settings/weights -----------------------------------------------------

def test_get_weights_defaults(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.get("/api/settings/weights")
    assert resp.status_code == 200
    assert resp.json() == {"contacts": 3.0, "postings": 1.0, "freshness": 2.0}


def test_put_weights_roundtrip_and_affects_scoring(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(conn, [make_contact()], source="leon")
    set_contact_rating(conn, contact_id_of(conn, "Jane", "Doe"), "strong")
    record_posting(conn, company_id, title="PM", url="https://x/1")

    client = client_for(db_path)
    put_resp = client.put(
        "/api/settings/weights", json={"contacts": 10.0, "postings": 0.0, "freshness": 0.0}
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == {"ok": True, "weights": {"contacts": 10.0, "postings": 0.0, "freshness": 0.0}}

    get_resp = client.get("/api/settings/weights")
    assert get_resp.json() == {"contacts": 10.0, "postings": 0.0, "freshness": 0.0}

    company = client.get("/api/companies").json()["companies"][0]
    # strong contact = 2 points * weight 10.0 = 20.0; postings/freshness weighted to 0
    assert company["score"] == pytest.approx(20.0)

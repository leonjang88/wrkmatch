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


def set_posting_posted_at(conn, posting_id: int, posted_at) -> None:
    conn.execute("UPDATE postings SET posted_at = ? WHERE id = ?", (posted_at, posting_id))
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

    record_posting(conn, company_id, title="PM 1", url="https://boards.greenhouse.io/acme/jobs/1", location="Remote")
    record_posting(conn, company_id, title="PM 2", url="https://boards.greenhouse.io/acme/jobs/2", location="Remote")

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
    record_posting(conn, company_id, title="PM 1", url="https://boards.greenhouse.io/acme/jobs/1", location="Remote")
    record_posting(conn, company_id, title="PM 2", url="https://boards.greenhouse.io/acme/jobs/2", location="Remote")
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
    record_posting(conn, normal_id, title="PM", url="https://boards.greenhouse.io/acme/jobs/1", location="Remote")
    record_posting(conn, hidden_id, title="PM", url="https://boards.greenhouse.io/hidden/jobs/1", location="Remote")

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
    record_posting(conn, dep_id, title="PM", url="https://boards.greenhouse.io/dep/jobs/1", location="Remote")
    record_posting(conn, normal_id, title="PM", url="https://boards.greenhouse.io/norm/jobs/1", location="Remote")

    client = client_for(db_path)
    names_in_order = [c["name"] for c in client.get("/api/companies").json()["companies"]]
    assert names_in_order == ["low score normal", "high score deprioritized"]


def test_companies_sorted_by_score_desc_then_name_asc(seed):
    conn, db_path = seed
    low_id = upsert_company(conn, "low co")
    high_id = upsert_company(conn, "high co")
    tie_a_id = upsert_company(conn, "tie a")
    tie_b_id = upsert_company(conn, "tie b")

    record_posting(conn, low_id, title="PM", url="https://x/low/1", location="Remote")
    record_posting(conn, high_id, title="PM", url="https://x/high/1", location="Remote")
    record_posting(conn, high_id, title="PM2", url="https://x/high/2", location="Remote")
    record_posting(conn, tie_a_id, title="PM", url="https://x/tiea/1", location="Remote")
    record_posting(conn, tie_b_id, title="PM", url="https://x/tieb/1", location="Remote")

    client = client_for(db_path)
    names_in_order = [c["name"] for c in client.get("/api/companies").json()["companies"]]
    # high co has 2 open (and new) postings, scoring above the rest; low/tie a/tie
    # b each have 1 open+new posting and tie on score, so they fall back to name asc
    assert names_in_order == ["high co", "low co", "tie a", "tie b"]


def test_companies_new_postings_7d_uses_first_seen_window(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Old", url="https://x/old", location="Remote")
    record_posting(conn, company_id, title="New", url="https://x/new", location="Remote")
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


def test_coverage_default_excludes_hidden_but_hidden_count_reflects_it(seed):
    conn, db_path = seed
    upsert_company(conn, "acme visible")
    hidden_id = upsert_company(conn, "beta hidden")
    set_company_priority(conn, hidden_id, "hidden")
    upsert_contacts(
        conn,
        [
            make_contact(first_name="P1", last_name="X", company="acme visible"),
            make_contact(first_name="P2", last_name="X", company="beta hidden"),
        ],
        source="leon",
    )

    client = client_for(db_path)
    body = client.get("/api/coverage").json()
    assert [c["name"] for c in body["no_ats"]] == ["acme visible"]
    assert body["hidden_count"] == 1


def test_coverage_include_hidden_includes_it_with_priority_field(seed):
    conn, db_path = seed
    hidden_id = upsert_company(conn, "beta hidden")
    set_company_priority(conn, hidden_id, "hidden")
    upsert_contacts(conn, [make_contact(company="beta hidden")], source="leon")

    client = client_for(db_path)
    body = client.get("/api/coverage?include_hidden=1").json()
    entry = next(c for c in body["no_ats"] if c["name"] == "beta hidden")
    assert entry["priority"] == "hidden"
    assert body["hidden_count"] == 1


def test_coverage_hiding_via_priority_endpoint_flows_through(seed):
    """Exercises the "ignore company" flow: POST /api/companies/{id}/priority
    hides a coverage company, which then disappears from default coverage
    (but is counted in hidden_count and reappears with include_hidden=1),
    and reappears in default coverage once un-hidden.
    """
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    upsert_contacts(conn, [make_contact(company="acme widgets")], source="leon")
    client = client_for(db_path)

    body = client.get("/api/coverage").json()
    assert any(c["id"] == company_id for c in body["no_ats"])
    assert body["hidden_count"] == 0

    resp = client.post(f"/api/companies/{company_id}/priority", json={"priority": "hidden"})
    assert resp.status_code == 200

    body = client.get("/api/coverage").json()
    assert all(c["id"] != company_id for c in body["no_ats"])
    assert body["hidden_count"] == 1

    body = client.get("/api/coverage?include_hidden=1").json()
    entry = next(c for c in body["no_ats"] if c["id"] == company_id)
    assert entry["priority"] == "hidden"

    resp = client.post(f"/api/companies/{company_id}/priority", json={"priority": "normal"})
    assert resp.status_code == 200

    body = client.get("/api/coverage").json()
    assert any(c["id"] == company_id for c in body["no_ats"])
    assert body["hidden_count"] == 0


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
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Remote")

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


# --- /api/settings/filters -----------------------------------------------------

def test_get_filters_defaults(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.get("/api/settings/filters")
    assert resp.status_code == 200
    assert resp.json() == {
        "location_enabled": True,
        "include_remote": True,
        "metro": "boston",
        "salary_min": None,
        "include_unknown_salary": True,
    }


def test_put_filters_roundtrip(seed):
    _, db_path = seed
    client = client_for(db_path)
    put_resp = client.put(
        "/api/settings/filters",
        json={
            "location_enabled": False,
            "include_remote": False,
            "metro": "boston",
            "salary_min": 150000,
            "include_unknown_salary": False,
        },
    )
    assert put_resp.status_code == 200
    expected = {
        "location_enabled": False,
        "include_remote": False,
        "metro": "boston",
        "salary_min": 150000,
        "include_unknown_salary": False,
    }
    assert put_resp.json() == {"ok": True, "filters": expected}

    get_resp = client.get("/api/settings/filters")
    assert get_resp.json() == expected


def test_put_filters_unknown_metro_422(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.put(
        "/api/settings/filters",
        json={
            "location_enabled": True,
            "include_remote": True,
            "metro": "atlantis",
            "salary_min": None,
            "include_unknown_salary": True,
        },
    )
    assert resp.status_code == 422


def test_put_filters_invalid_types_422(seed):
    _, db_path = seed
    client = client_for(db_path)
    resp = client.put(
        "/api/settings/filters",
        json={
            "location_enabled": True,
            "include_remote": True,
            "metro": "boston",
            "salary_min": "not-a-number",
            "include_unknown_salary": True,
        },
    )
    assert resp.status_code == 422


def set_filters(conn, **overrides) -> None:
    filters = {
        "location_enabled": True,
        "include_remote": True,
        "metro": "boston",
        "salary_min": None,
        "include_unknown_salary": True,
    }
    filters.update(overrides)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('filters', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(filters),),
    )
    conn.commit()


# --- filter-driven matching on /api/companies -----------------------------------

def test_companies_default_filters_remote_location_matches(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Remote")

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert [c["name"] for c in body["companies"]] == ["acme widgets"]
    assert body["companies"][0]["open_postings"] == 1


def test_companies_default_filters_boston_location_matches(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Boston, MA")

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert [c["name"] for c in body["companies"]] == ["acme widgets"]


def test_companies_default_filters_excludes_non_matching_onsite_nyc(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="New York, NY")

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"] == []
    assert body["companies_excluded_by_filters"] == 1


def test_companies_all_locations_bypasses_location_filter(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="New York, NY")

    client = client_for(db_path)
    body = client.get("/api/companies?all_locations=1").json()
    assert [c["name"] for c in body["companies"]] == ["acme widgets"]
    assert body["companies_excluded_by_filters"] == 0


def test_companies_response_echoes_filters(seed):
    _, db_path = seed
    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["filters"] == {
        "location_enabled": True,
        "include_remote": True,
        "metro": "boston",
        "salary_min": None,
        "include_unknown_salary": True,
    }


def test_companies_salary_min_filter_excludes_below_threshold(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Low", url="https://x/low", location="Remote")
    record_posting(conn, company_id, title="High", url="https://x/high", location="Remote")
    conn.execute("UPDATE postings SET salary_min = 80000, salary_max = 90000 WHERE url = 'https://x/low'")
    conn.execute("UPDATE postings SET salary_min = 160000, salary_max = 180000 WHERE url = 'https://x/high'")
    conn.commit()
    set_filters(conn, salary_min=150000, include_unknown_salary=False)

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"][0]["open_postings"] == 1


def test_companies_salary_min_filter_include_unknown_true_keeps_unknown(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Unknown", url="https://x/unknown", location="Remote")
    set_filters(conn, salary_min=150000, include_unknown_salary=True)

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"][0]["open_postings"] == 1


def test_companies_salary_min_filter_include_unknown_false_drops_unknown(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Unknown", url="https://x/unknown", location="Remote")
    set_filters(conn, salary_min=150000, include_unknown_salary=False)

    client = client_for(db_path)
    body = client.get("/api/companies").json()
    assert body["companies"] == []


# --- filter matches_filters flag on /api/companies/{id} -------------------------

def test_company_detail_matches_filters_flags(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Remote OK", url="https://x/remote", location="Remote")
    record_posting(conn, company_id, title="NYC No", url="https://x/nyc", location="New York, NY")

    client = client_for(db_path)
    body = client.get(f"/api/companies/{company_id}").json()
    by_url = {p["url"]: p["matches_filters"] for p in body["postings"]}
    assert by_url["https://x/remote"] is True
    assert by_url["https://x/nyc"] is False
    # detail endpoint still returns every open posting regardless of match
    assert len(body["postings"]) == 2


def test_company_detail_includes_enrichment_fields(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Remote")
    conn.execute(
        "UPDATE postings SET department = ?, posted_at = ?, salary_min = ?, salary_max = ?, "
        "salary_currency = ?, remote = ? WHERE url = 'https://x/1'",
        ("Product", "2024-01-01T00:00:00+00:00", 150000, 180000, "USD", "remote"),
    )
    conn.commit()

    client = client_for(db_path)
    posting = client.get(f"/api/companies/{company_id}").json()["postings"][0]
    assert posting["department"] == "Product"
    assert posting["posted_at"] == "2024-01-01T00:00:00+00:00"
    assert posting["salary_min"] == 150000
    assert posting["salary_max"] == 180000
    assert posting["salary_currency"] == "USD"
    assert posting["remote"] == "remote"


# --- /api/summary matching_postings ----------------------------------------------

def test_summary_matching_postings_under_default_filters(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Remote OK", url="https://x/remote", location="Remote")
    record_posting(conn, company_id, title="NYC No", url="https://x/nyc", location="New York, NY")

    client = client_for(db_path)
    body = client.get("/api/summary").json()
    assert body["open_postings"] == 2
    assert body["matching_postings"] == 1


def test_summary_matching_postings_with_location_disabled(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Remote OK", url="https://x/remote", location="Remote")
    record_posting(conn, company_id, title="NYC No", url="https://x/nyc", location="New York, NY")
    set_filters(conn, location_enabled=False)

    client = client_for(db_path)
    body = client.get("/api/summary").json()
    assert body["matching_postings"] == 2


# --- GET /api/postings ------------------------------------------------------

def test_postings_default_only_matching_and_none_status(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Matching Open", url="https://x/ok", location="Remote")
    record_posting(conn, company_id, title="Non Matching", url="https://x/nyc", location="New York, NY")
    record_posting(conn, company_id, title="Done Already", url="https://x/done", location="Remote")
    set_posting_user_status(conn, posting_id_of(conn, "https://x/done"), "done")

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    urls = [p["url"] for p in body["postings"]]
    assert urls == ["https://x/ok"]
    assert body["total"] == 3
    assert body["matching"] == 2  # ok + done both match filters; nyc doesn't


def test_postings_all_includes_non_matching_and_done_ignored(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="Matching Open", url="https://x/ok", location="Remote")
    record_posting(conn, company_id, title="Non Matching", url="https://x/nyc", location="New York, NY")
    record_posting(conn, company_id, title="Ignored", url="https://x/ignored", location="Remote")
    set_posting_user_status(conn, posting_id_of(conn, "https://x/ignored"), "ignored")

    client = client_for(db_path)
    body = client.get("/api/postings?all=1").json()
    urls = {p["url"] for p in body["postings"]}
    assert urls == {"https://x/ok", "https://x/nyc", "https://x/ignored"}
    by_url = {p["url"]: p for p in body["postings"]}
    assert by_url["https://x/nyc"]["matches_filters"] is False
    assert by_url["https://x/ignored"]["user_status"] == "ignored"
    assert body["total"] == 3
    assert body["matching"] == 2


def test_postings_hidden_company_excluded_always_including_all(seed):
    """Hidden means the user deliberately ignored that company -- the Jobs
    "show all" reveal (?all=1) is for filtered-out/done/ignored rows, not for
    resurfacing ignored companies. Companies tab's own hidden-reveal is the
    one intentional path back, so /api/postings excludes hidden companies in
    every mode.
    """
    conn, db_path = seed
    visible_id = upsert_company(conn, "visible co")
    record_posting(conn, visible_id, title="Visible PM", url="https://x/visible", location="Remote")

    hidden_id = upsert_company(conn, "hidden co")
    set_company_priority(conn, hidden_id, "hidden")
    record_posting(conn, hidden_id, title="Hidden PM", url="https://x/hidden", location="Remote")

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    assert [p["url"] for p in body["postings"]] == ["https://x/visible"]
    assert body["total"] == 1

    body = client.get("/api/postings?all=1").json()
    urls = [p["url"] for p in body["postings"]]
    assert "https://x/hidden" not in urls
    assert urls == ["https://x/visible"]
    assert body["total"] == 1


def test_postings_deprioritized_company_included_and_flagged(seed):
    conn, db_path = seed
    dep_id = upsert_company(conn, "dep co")
    set_company_priority(conn, dep_id, "deprioritized")
    record_posting(conn, dep_id, title="Dep PM", url="https://x/dep", location="Remote")

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    assert len(body["postings"]) == 1
    assert body["postings"][0]["company_priority"] == "deprioritized"


def test_postings_sort_order(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    now = datetime.now(timezone.utc)

    record_posting(conn, company_id, title="Bravo", url="https://x/bravo", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/bravo"), iso(now))

    record_posting(conn, company_id, title="Delta", url="https://x/delta", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/delta"), iso(now - timedelta(days=1)))

    record_posting(conn, company_id, title="Alpha", url="https://x/alpha", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/alpha"), iso(now))
    set_posting_posted_at(conn, posting_id_of(conn, "https://x/alpha"), iso(now - timedelta(days=3)))

    record_posting(conn, company_id, title="Charlie", url="https://x/charlie", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/charlie"), iso(now - timedelta(days=10)))
    set_posting_posted_at(conn, posting_id_of(conn, "https://x/charlie"), iso(now))

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    titles = [p["title"] for p in body["postings"]]
    # is_new (from first_seen) desc first: Bravo/Delta/Alpha are new, Charlie
    # is not (despite its very recent posted_at -- is_new only looks at
    # first_seen). Within the is_new group, sorted by
    # COALESCE(posted_at, first_seen) desc: Bravo (now) > Delta (now-1d) >
    # Alpha (now-3d, via posted_at).
    assert titles == ["Bravo", "Delta", "Alpha", "Charlie"]


def test_postings_sort_title_tiebreak(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    now = iso(datetime.now(timezone.utc))

    record_posting(conn, company_id, title="Zulu", url="https://x/zulu", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/zulu"), now)

    record_posting(conn, company_id, title="Alpha", url="https://x/alpha", location="Remote")
    set_posting_first_seen(conn, posting_id_of(conn, "https://x/alpha"), now)

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    assert [p["title"] for p in body["postings"]] == ["Alpha", "Zulu"]


def test_postings_contact_join_correctness(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Remote")
    upsert_contacts(
        conn,
        [
            make_contact(first_name="A", last_name="Strong", company="acme widgets"),
            make_contact(first_name="B", last_name="Ok", company="acme widgets"),
        ],
        source="leon",
    )
    set_contact_rating(conn, contact_id_of(conn, "A", "Strong"), "strong")
    set_contact_rating(conn, contact_id_of(conn, "B", "Ok"), "ok")

    client = client_for(db_path)
    body = client.get("/api/postings").json()
    posting = body["postings"][0]
    assert posting["contact_count"] == 2
    assert posting["contact_points"] == 3  # strong(2) + ok(1)
    assert posting["company_id"] == company_id
    assert posting["company_name"] == "acme widgets"


def test_postings_response_shape_has_all_documented_fields(seed):
    conn, db_path = seed
    company_id = upsert_company(conn, "acme widgets")
    record_posting(conn, company_id, title="PM", url="https://x/1", location="Remote")

    client = client_for(db_path)
    posting = client.get("/api/postings").json()["postings"][0]
    assert set(posting.keys()) == {
        "id", "title", "url", "location", "department", "posted_at",
        "salary_min", "salary_max", "salary_currency", "remote",
        "first_seen", "user_status", "is_new", "matches_filters",
        "company_id", "company_name", "company_priority",
        "contact_count", "contact_points",
    }

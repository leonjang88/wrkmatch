"""Interface contract for three NOT-YET-IMPLEMENTED additions to wrkmatch/ats_clients.py.

wrkmatch/ats_clients.py currently defines 5 clients (greenhouse_jobs, lever_jobs,
ashby_jobs, workable_jobs, recruitee_jobs), each following the same shape:

    def {platform}_jobs(slug: str) -> List[Job]

fetching from a public ATS API and returning `Job` dataclass instances (company,
source, title, location, department, url, posted_at). Network errors, non-200
responses, and malformed/missing payload shapes all degrade to `[]` rather than
raising. These tests fail on import until the three new functions below exist,
and until they're added to `ATS_FUNCS`. That's expected.

The contract:

    smartrecruiters_jobs(slug: str) -> List[Job]
        GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
        Response: {"content": [{"id": ..., "name": ..., "location": {"city": ...},
        "function": {"label": ...}, "releasedDate": ...}, ...]}
        Field mapping: title <- name, location <- location.city,
        department <- function.label, posted_at <- releasedDate.
        url <- f"https://jobs.smartrecruiters.com/{slug}/{id}"
        source == "smartrecruiters". company falls back to `slug` (SmartRecruiters'
        postings payload carries no separate company display name).
        [] on: HTTP error status, connection error, missing/non-list "content".

    personio_jobs(slug: str) -> List[Job]
        GET https://{slug}.jobs.personio.de/xml?language=en
        Response body is XML (not JSON) — the shared `_get_json` JSON+
        Content-Type helper does not apply; this client fetches raw text/xml
        and parses with xml.etree.ElementTree:
            <workzag-jobs>
              <position>
                <id>123</id>
                <name>Product Manager</name>
                <office>Berlin</office>
                <department>Product</department>
              </position>
            </workzag-jobs>
        Field mapping: title <- name, location <- office, department <- department.
        url <- f"https://{slug}.jobs.personio.de/job/{id}?language=en"
        source == "personio". company falls back to `slug`.
        [] on: HTTP error status, connection error, unparseable/empty XML,
        XML with no <position> elements.

    workday_jobs(slug: str) -> List[Job]
        POST https://{slug}.wd{N}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs
        (JSON body carrying limit/offset/searchText — a search-all query).
        Workday tenants are split across numbered hosts (wd1-wd5) and career-site
        names, neither of which is discoverable in advance, so this client probes
        a bounded, ordered set of candidates and stops at the first one that
        yields postings:
            hosts: wd1, wd2, wd3, wd5 (in that order)
            sites: {slug}, "External", "Careers" (in that order)
            i.e. (wd1, slug), (wd1, External), (wd1, Careers),
                 (wd2, slug), (wd2, External), (wd2, Careers), ...
        A probe "hits" when the response is 200 with a non-empty "jobPostings"
        list; anything else (error status, connection error, missing/empty
        jobPostings) moves on to the next candidate. Once a probe hits, no
        further candidates are probed.
        Response: {"total": N, "jobPostings": [{"title": ..., "locationsText": ...,
        "externalPath": "/job/...", "postedOn": ...}, ...]}
        Field mapping: title <- title, location <- locationsText,
        department <- "" (Workday's search payload carries no department field),
        posted_at <- postedOn.
        url <- f"https://{slug}.wd{{N}}.myworkdayjobs.com/en-US/{{site}}{{externalPath}}"
        using whichever (N, site) probe hit.
        source == "workday". company falls back to `slug`.
        [] when every candidate in the bounded set fails/misses.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
import requests

from wrkmatch.ats_clients import (
    ATS_FUNCS,
    ashby_jobs,
    greenhouse_jobs,
    lever_jobs,
    personio_jobs,
    recruitee_jobs,
    smartrecruiters_jobs,
    workable_jobs,
    workday_jobs,
)

WORKDAY_HOSTS = ["wd1", "wd2", "wd3", "wd5"]
WORKDAY_SITES_TEMPLATE = ["{slug}", "External", "Careers"]


def workday_url(slug: str, host: str, site: str) -> str:
    return f"https://{slug}.{host}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs"


def workday_probe_order(slug: str):
    for host in WORKDAY_HOSTS:
        for site_tpl in WORKDAY_SITES_TEMPLATE:
            site = slug if site_tpl == "{slug}" else site_tpl
            yield host, site


# --- smartrecruiters_jobs ----------------------------------------------------

def test_smartrecruiters_jobs_success(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        json={
            "content": [
                {
                    "id": "abc123",
                    "name": "Product Manager",
                    "location": {"city": "Austin"},
                    "function": {"label": "Product"},
                    "releasedDate": "2024-03-01T00:00:00Z",
                }
            ]
        },
        headers={"Content-Type": "application/json"},
    )

    jobs = smartrecruiters_jobs(slug)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "smartrecruiters"
    assert job.title == "Product Manager"
    assert job.location == "Austin"
    assert job.department == "Product"
    assert job.url == f"https://jobs.smartrecruiters.com/{slug}/abc123"


def test_smartrecruiters_jobs_http_error_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        status_code=404,
    )
    assert smartrecruiters_jobs(slug) == []


def test_smartrecruiters_jobs_connection_error_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        exc=requests.ConnectionError,
    )
    assert smartrecruiters_jobs(slug) == []


def test_smartrecruiters_jobs_malformed_payload_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        json={"unexpected": "shape"},
        headers={"Content-Type": "application/json"},
    )
    assert smartrecruiters_jobs(slug) == []


def test_smartrecruiters_jobs_empty_content_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        json={"content": []},
        headers={"Content-Type": "application/json"},
    )
    assert smartrecruiters_jobs(slug) == []


# --- personio_jobs ------------------------------------------------------------

PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>123</id>
    <name>Product Manager</name>
    <office>Berlin</office>
    <department>Product</department>
  </position>
</workzag-jobs>
"""


def test_personio_jobs_success(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://{slug}.jobs.personio.de/xml?language=en",
        text=PERSONIO_XML,
        headers={"Content-Type": "application/xml"},
    )

    jobs = personio_jobs(slug)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "personio"
    assert job.title == "Product Manager"
    assert job.location == "Berlin"
    assert job.department == "Product"
    assert job.url == f"https://{slug}.jobs.personio.de/job/123?language=en"


def test_personio_jobs_http_error_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://{slug}.jobs.personio.de/xml?language=en",
        status_code=500,
    )
    assert personio_jobs(slug) == []


def test_personio_jobs_connection_error_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://{slug}.jobs.personio.de/xml?language=en",
        exc=requests.ConnectionError,
    )
    assert personio_jobs(slug) == []


def test_personio_jobs_unparseable_xml_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://{slug}.jobs.personio.de/xml?language=en",
        text="<not><valid<xml",
        headers={"Content-Type": "application/xml"},
    )
    assert personio_jobs(slug) == []


def test_personio_jobs_no_positions_returns_empty(requests_mock):
    slug = "acme"
    requests_mock.get(
        f"https://{slug}.jobs.personio.de/xml?language=en",
        text="<workzag-jobs></workzag-jobs>",
        headers={"Content-Type": "application/xml"},
    )
    assert personio_jobs(slug) == []


# --- workday_jobs ---------------------------------------------------------------

def _workday_payload(external_path="/job/product-manager_R-00123", title="Product Manager"):
    return {
        "total": 1,
        "jobPostings": [
            {
                "title": title,
                "locationsText": "Remote - US",
                "externalPath": external_path,
                "postedOn": "Posted 3 Days Ago",
            }
        ],
    }


def test_workday_jobs_success_on_first_probe(requests_mock):
    slug = "acme"
    host, site = next(workday_probe_order(slug))  # (wd1, slug)
    requests_mock.post(workday_url(slug, host, site), json=_workday_payload())

    jobs = workday_jobs(slug)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "workday"
    assert job.title == "Product Manager"
    assert job.location == "Remote - US"
    assert job.url == f"https://{slug}.{host}.myworkdayjobs.com/en-US/{site}/job/product-manager_R-00123"
    # first candidate hit -> no further probes made
    assert len(requests_mock.request_history) == 1


def test_workday_jobs_success_on_later_probe_skips_remaining(requests_mock):
    slug = "acme"
    probes = list(workday_probe_order(slug))
    hit_index = 4  # (wd2, "External") given the documented probe order
    hit_host, hit_site = probes[hit_index]

    for host, site in probes[:hit_index]:
        requests_mock.post(workday_url(slug, host, site), status_code=404)
    requests_mock.post(workday_url(slug, hit_host, hit_site), json=_workday_payload())
    # deliberately no mock registered for probes after hit_index: if the
    # implementation queries them, requests_mock raises and the test fails,
    # proving "remaining probes skipped".

    jobs = workday_jobs(slug)

    assert len(jobs) == 1
    assert jobs[0].url == (
        f"https://{slug}.{hit_host}.myworkdayjobs.com/en-US/{hit_site}/job/product-manager_R-00123"
    )
    assert len(requests_mock.request_history) == hit_index + 1


def test_workday_jobs_all_probes_fail_returns_empty(requests_mock):
    slug = "acme"
    for host, site in workday_probe_order(slug):
        requests_mock.post(workday_url(slug, host, site), status_code=404)

    assert workday_jobs(slug) == []
    assert len(requests_mock.request_history) == len(WORKDAY_HOSTS) * len(WORKDAY_SITES_TEMPLATE)


def test_workday_jobs_all_probes_connection_error_returns_empty(requests_mock):
    slug = "acme"
    for host, site in workday_probe_order(slug):
        requests_mock.post(workday_url(slug, host, site), exc=requests.ConnectionError)

    assert workday_jobs(slug) == []


def test_workday_jobs_malformed_payload_treated_as_miss(requests_mock):
    slug = "acme"
    probes = list(workday_probe_order(slug))

    # First probe returns 200 but without usable jobPostings -> must be
    # treated as a miss, not a crash, and probing must continue.
    requests_mock.post(workday_url(slug, *probes[0]), json={"total": 0, "jobPostings": []})
    for host, site in probes[1:]:
        requests_mock.post(workday_url(slug, host, site), status_code=404)

    assert workday_jobs(slug) == []


# --- ATS_FUNCS registry -------------------------------------------------------

def test_ats_funcs_contains_all_eight_clients():
    # The original 5 must keep their existing relative order; the 3 new ones
    # are appended in any order among themselves.
    assert ATS_FUNCS[:5] == [
        greenhouse_jobs,
        lever_jobs,
        ashby_jobs,
        workable_jobs,
        recruitee_jobs,
    ]
    assert len(ATS_FUNCS) == 8
    assert set(ATS_FUNCS[5:]) == {smartrecruiters_jobs, workday_jobs, personio_jobs}

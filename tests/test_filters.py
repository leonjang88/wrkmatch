"""Interface contract for a NOT-YET-IMPLEMENTED module wrkmatch/filters.py.

wrkmatch/filters.py does not exist yet; these tests fail on import until it's
implemented. That's expected. The contract they define:

    is_pm_title(title: str | None, extra_patterns: list[str] | None = None) -> bool
        Case-insensitive check for whether a job title reads as a Product
        Manager role (as opposed to adjacent-but-different titles like
        "Product Marketing Manager" or "Program Manager"). Whitespace around
        the title is tolerated. `None` and `""` both return False rather than
        raising.

        `extra_patterns`, when given, is a list of additional regex or
        substring patterns checked in addition to (not instead of) the
        built-in default patterns — any match, default or extra, returns True.

    filter_pm_jobs(jobs: list[Job]) -> list[Job]
        Filters a list of Job instances (see wrkmatch/ats_clients.py) down to
        those whose `.title` satisfies is_pm_title(), preserving input order.
        Empty input -> empty output.
"""
from __future__ import annotations

import pytest

from wrkmatch.ats_clients import Job
from wrkmatch.filters import filter_pm_jobs, is_pm_title


def make_job(title: str, **overrides) -> Job:
    fields = dict(
        company="Acme Widgets",
        source="greenhouse",
        title=title,
        location="Remote",
        department="Product",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    fields.update(overrides)
    return Job(**fields)


PM_TITLES = [
    "Product Manager",
    "Senior Product Manager",
    "Principal Product Manager",
    "Group Product Manager",
    "Staff Product Manager",
    "Associate Product Manager",
    "Product Manager, Growth",
    "Technical Product Manager",
    "Director of Product Management",
    "Head of Product",
    "VP of Product",
    "Director of Product",
    "Director, Product Management",
    "Product Management Lead",
    "Product Owner",
    "Product Manager, Marketing",
]

NON_PM_TITLES = [
    "Product Marketing Manager",
    "Production Manager",
    "Product Designer",
    "Product Analyst",
    "Program Manager",
    "Project Manager",
    "Software Engineer, Product",
    "Product Support Specialist",
    "Manager, Production Engineering",
    "Director of Product Marketing",
    "Associate Director of Product Marketing, ICHRA",
    "VP of Product Design",
    "Head of Product Marketing",
]


# --- is_pm_title: positive matches ------------------------------------------

@pytest.mark.parametrize("title", PM_TITLES)
def test_is_pm_title_matches_pm_titles(title):
    assert is_pm_title(title) is True


@pytest.mark.parametrize("title", PM_TITLES)
def test_is_pm_title_matches_are_case_insensitive(title):
    assert is_pm_title(title.upper()) is True
    assert is_pm_title(title.lower()) is True


# --- is_pm_title: negative matches ------------------------------------------

@pytest.mark.parametrize("title", NON_PM_TITLES)
def test_is_pm_title_rejects_non_pm_titles(title):
    assert is_pm_title(title) is False


@pytest.mark.parametrize("title", NON_PM_TITLES)
def test_is_pm_title_rejections_are_case_insensitive(title):
    assert is_pm_title(title.upper()) is False
    assert is_pm_title(title.lower()) is False


# --- is_pm_title: the tricky pair -------------------------------------------

def test_is_pm_title_product_marketing_manager_excluded():
    assert is_pm_title("Product Marketing Manager") is False


def test_is_pm_title_product_manager_comma_marketing_included():
    assert is_pm_title("Product Manager, Marketing") is True


# --- is_pm_title: edge cases -------------------------------------------------

def test_is_pm_title_empty_string_is_false():
    assert is_pm_title("") is False


def test_is_pm_title_none_is_false():
    assert is_pm_title(None) is False


def test_is_pm_title_tolerates_surrounding_whitespace():
    assert is_pm_title("   Product Manager   ") is True


def test_is_pm_title_tolerates_internal_extra_whitespace():
    assert is_pm_title("Product   Manager") is True


# --- is_pm_title: extra_patterns override ------------------------------------

def test_is_pm_title_extra_patterns_broadens_match():
    assert is_pm_title("Product Lead, Payments", extra_patterns=["product lead"]) is True


def test_is_pm_title_extra_patterns_does_not_replace_defaults():
    # "Product Owner" already matches via defaults even with an unrelated extra pattern.
    assert is_pm_title("Product Owner", extra_patterns=["product lead"]) is True
    # A non-PM title still isn't matched just because extra_patterns was passed.
    assert is_pm_title("Production Manager", extra_patterns=["product lead"]) is False


# --- filter_pm_jobs -----------------------------------------------------------

def test_filter_pm_jobs_keeps_only_pm_titles():
    jobs = [make_job("Product Manager"), make_job("Product Marketing Manager")]
    result = filter_pm_jobs(jobs)
    assert [j.title for j in result] == ["Product Manager"]


def test_filter_pm_jobs_preserves_order():
    titles = ["Senior Product Manager", "Program Manager", "Product Owner", "Product Designer", "Head of Product"]
    jobs = [make_job(t) for t in titles]
    result = filter_pm_jobs(jobs)
    assert [j.title for j in result] == ["Senior Product Manager", "Product Owner", "Head of Product"]


def test_filter_pm_jobs_empty_list_returns_empty_list():
    assert filter_pm_jobs([]) == []


def test_filter_pm_jobs_returns_same_job_objects():
    pm_job = make_job("Product Manager")
    result = filter_pm_jobs([pm_job, make_job("Program Manager")])
    assert result == [pm_job]

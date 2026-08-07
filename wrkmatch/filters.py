from __future__ import annotations
import re
from typing import Optional, List

from .ats_clients import Job

# Compiled patterns for matching PM titles
_PM_PATTERNS = [
    re.compile(r'\bproduct\s+manager\b', re.IGNORECASE),
    re.compile(r'\bproduct\s+owner\b', re.IGNORECASE),
    re.compile(r'\bproduct\s+management\s+lead\b', re.IGNORECASE),
    re.compile(r'\bhead\s+of\s+product(?!\s+(marketing|design))\b', re.IGNORECASE),
    re.compile(r'\b(director|vp)\s+of\s+product(?!\s+(marketing|design))\b', re.IGNORECASE),
    re.compile(r'\b(director|vp)\s*,\s*product\s+management\b', re.IGNORECASE),
]


def _normalize_title(title: Optional[str]) -> str:
    """Normalize title for matching: strip, lowercase, collapse internal whitespace."""
    if not title:
        return ""
    return re.sub(r'\s+', ' ', title.strip())


def is_pm_title(title: Optional[str], extra_patterns: Optional[List[str]] = None) -> bool:
    """
    Case-insensitive check for whether a job title reads as a Product Manager role.

    Returns False for None and empty strings. Handles internal and surrounding whitespace.
    extra_patterns (if provided) adds additional substring/regex patterns to check
    in addition to the built-in defaults.

    Args:
        title: Job title to check
        extra_patterns: Optional list of additional regex patterns to match

    Returns:
        True if title matches any default or extra pattern, False otherwise
    """
    if not title:
        return False

    normalized = _normalize_title(title)

    # Check built-in patterns
    for pattern in _PM_PATTERNS:
        if pattern.search(normalized):
            return True

    # Check extra patterns if provided
    if extra_patterns:
        for pattern_str in extra_patterns:
            try:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                if pattern.search(normalized):
                    return True
            except re.error:
                # Invalid regex, skip this pattern
                continue

    return False


def filter_pm_jobs(jobs: List[Job]) -> List[Job]:
    """
    Filter a list of Job instances to only those with PM titles.

    Preserves input order and returns the same Job objects.

    Args:
        jobs: List of Job instances to filter

    Returns:
        Filtered list containing only jobs matching is_pm_title()
    """
    return [job for job in jobs if is_pm_title(job.title)]

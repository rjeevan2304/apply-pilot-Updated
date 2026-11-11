"""Shared job filters used by every discovery source."""

from __future__ import annotations

import re


_INDIA_PLACES = ("india", "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai",
                 "delhi", "new delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
                 "kochi", "jaipur")
_UAE_PLACES = ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah")
_REMOTE_FILLER = re.compile(
    r"\b(remote|work from home|wfh|home[- ]based|distributed|hybrid|on[- ]?site|or|and|the|in|"
    r"from|based|location|locations|position|role|worldwide|global|anywhere|apac|asia|pacific)\b")


def open_to(location: str | None, places: tuple[str, ...], extra_open: tuple[str, ...] = ()) -> bool:
    """Can someone living in `places` take this job?

    True for the places themselves, for "N Locations" (unknown, kept), for remote
    roles with no country/region named (or marked worldwide/global/anywhere and
    any `extra_open` words), and False for anything tied to another country or
    region, e.g. "Remote, United States" or "Toronto, Canada".
    """
    if not location or str(location).strip().lower() in ("", "nan", "none"):
        return True
    low = str(location).lower()
    if any(re.search(rf"\b{re.escape(p)}\b", low) for p in places):
        return True
    if re.search(r"\b\d+\s+locations?\b", low):
        return True
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in ("worldwide", "global", "anywhere") + extra_open):
        return True
    if re.search(r"\bremote\b|work from home|\bwfh\b", low):
        leftover = re.sub(r"[^a-z]+", " ", _REMOTE_FILLER.sub(" ", low)).strip()
        return leftover == ""
    return False


def india_open(location: str | None) -> bool:
    """India roles, plus remote roles open to India (worldwide / APAC / unrestricted)."""
    return open_to(location, _INDIA_PLACES, extra_open=("apac", "asia"))


def uae_open(location: str | None) -> bool:
    """UAE roles, plus remote roles open to the UAE (worldwide / unrestricted)."""
    return open_to(location, _UAE_PLACES)


def title_ok(title: str | None) -> bool:
    """Exclude unrelated functions, internships and senior roles.

    Business Analyst and Program/Project Manager are allowed; interns,
    trainees, sales, recruiting, HR, marketing and data entry are dropped.
    """
    if title is None or str(title).strip().lower() in {'', 'nan', 'none'}: return False
    value = re.sub(r'\s+', ' ', str(title)).strip().lower()
    excluded = (r'\b(sales|sdr|bdr|account executive|business development|recruiter|recruitment|talent acquisition|human resources|hr|marketing|data entry|intern|interns|internship|trainee)\b',)
    if any(re.search(term, value) for term in excluded): return False
    senior = (r'\b(senior|snr|sr\.?|staff|lead|leader|leaders|leadership|principal|director|head|vp|avp|svp|evp|vice president|chief|distinguished|fellow)\b' r'|\b(level|l)\s*(ii|iii|iv|2|3|4)\b' r'|\b(manager|engineer|analyst)\s+(ii|iii|iv)\b')
    return re.search(senior, value) is None

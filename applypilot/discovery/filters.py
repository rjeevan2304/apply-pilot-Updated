"""Shared job filters used by every discovery source."""

from __future__ import annotations

import re


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

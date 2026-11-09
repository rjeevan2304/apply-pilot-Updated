"""Shared job filters used by every discovery source."""

from __future__ import annotations

import re


def title_ok(title: str | None) -> bool:
    """Exclude unrelated functions and senior roles; preserve Product Analyst."""
    if title is None or str(title).strip().lower() in {'', 'nan', 'none'}: return False
    value = re.sub(r'\s+', ' ', str(title)).strip().lower()
    excluded = ('business analyst','strategy analyst','strategy consultant','management consultant','business operations analyst','business operations','program analyst','program manager','project manager','operations analyst','financial analyst','management analyst','account executive','recruiter','human resources','marketing ')
    if any(term in value for term in excluded): return False
    senior = (r'\b(senior|sr\.?|staff|lead|principal|director|head|vp|vice president|chief)\b' r'|\b(level|l)\s*(ii|iii|iv|2|3|4)\b' r'|\b(manager|engineer|analyst)\s+(ii|iii|iv)\b')
    return re.search(senior, value) is None

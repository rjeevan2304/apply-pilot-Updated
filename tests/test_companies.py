"""Tests for target-company discovery filters (real location strings from the boards)."""

import pytest

from applypilot.discovery.companies import Posting, _html_to_text, classify_location, title_matches


def P(title, locs, remote=None):
    return Posting(company="X", title=title, url="u", apply_url="u", locations=locs, remote=remote)


@pytest.mark.parametrize("title,locs,remote,expected", [
    # Canonical
    ("Python Engineer", ["Home based - Worldwide"], None, "Remote - worldwide"),
    ("Go Engineer", ["Home Based - APAC", "Home based - EMEA"], None, "Remote - APAC"),
    ("Kernel Engineer", ["Home based - EMEA"], None, ""),
    ("IoT Architect (Americas only)", ["Home based - Worldwide"], None, ""),
    ("Graduate SDR - Canada (Remote)", ["Home Based - Asia Pacific"], None, ""),
    # GitLab / Grafana / Elastic
    ("Backend Engineer", ["Bangalore, India"], None, "India"),
    ("Backend Engineer", ["Remote, United States"], None, ""),
    ("SRE", ["India (Remote)"], None, "India"),
    ("SRE", ["Spain (Remote)"], None, ""),
    ("UX Engineer", ["India"], None, "India"),
    # Supabase / PostHog (Ashby)
    ("Software Engineer - Auth", ["Remote, Global"], True, "Remote - worldwide"),
    ("Support Engineer (EMEA)", ["Remote, EMEA"], None, ""),
    ("Product Engineer", ["Remote"], True, "Remote - region not stated"),
    ("SRE (US - Pacific time)", ["Remote (US)"], True, ""),
    ("Customer Solution Architect (AU, NZ, SG)", ["Remote, APAC"], True, ""),
    ("Platform Security Engineer (AMER/APAC)", ["Remote, Global"], True, "Remote - worldwide"),
    # Atlassian: bare "Remote - Remote" next to a US city means US-remote
    ("Software Engineer", ["Seattle - United States - Seattle, Washington United States", "Remote - Remote"], None, ""),
    ("Data Platform Engineer", ["Bengaluru - India - Bengaluru, 560071 India", "Remote - Remote"], None, "India"),
    ("Software Engineer", ["Remote - Remote"], None, "Remote - region not stated"),
    # Onsite elsewhere
    ("ML Engineer", ["San Francisco"], False, ""),
])
def test_classify_location(title, locs, remote, expected):
    assert classify_location(P(title, locs, remote)) == expected


def test_title_matches():
    kw = ["engineer", "product"]
    assert title_matches("Junior Ubuntu Software Engineer", kw)
    assert title_matches("Product Manager - Auth", kw)
    assert not title_matches("Cloud Engineering Manager", kw + ["engineering"])
    assert not title_matches("Graduate Sales Development Representative", kw, ["sales"])
    assert not title_matches("Billing Support Specialist", kw)
    assert title_matches("Anything", [])


def test_html_to_text_handles_greenhouse_double_escaping():
    raw = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    assert _html_to_text(raw) == "Hello & welcome\n- Python"

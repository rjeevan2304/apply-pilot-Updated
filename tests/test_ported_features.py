"""Tests for features ported from the kkmkmkmk variant: title filter, years rule,
UAE region, kie.ai provider fallback, jobspy rate-limit watcher."""

import json
import logging

import httpx
import pytest

from applypilot import llm
from applypilot.discovery.companies import Posting, classify_location
from applypilot.discovery.filters import title_ok
from applypilot.scoring.scorer import _apply_years_rule, min_years_required


# -- title filter ---------------------------------------------------------------

@pytest.mark.parametrize("title,ok", [
    ("Business Analyst", True),
    ("Program Manager", True),
    ("Project Manager", True),
    ("Associate Product Manager", True),
    ("Product Analyst", True),
    ("Software Engineering Intern", False),
    ("Data Analyst Internship", False),
    ("Graduate Trainee", False),
    ("Sales Development Representative", False),
    ("HR Business Partner", False),
    ("Senior Business Analyst", False),
    ("Snr Solutions Architect", False),
    ("Product Manager II", False),
])
def test_title_ok(title, ok):
    assert title_ok(title) is ok


# -- years-of-experience rule ----------------------------------------------------

def test_min_years_required():
    assert min_years_required("Analyst", None, "We need 5+ years of experience") == 5
    assert min_years_required("Analyst", None, "3-5 years experience; 2+ years SQL") == 2
    assert min_years_required("Analyst", None, "Minimum of 7 years in product") == 7
    assert min_years_required(
        "Product Analyst",
        "https://www.naukri.com/job-listings-product-analyst-acme-hyderabad-9-to-14-years-091126",
        "") == 9
    assert min_years_required("Analyst", None, "Great team, fun culture") is None


def test_years_rule_caps_score():
    job = {"title": "Product Analyst", "url": "", "full_description": "Requires 6+ years of experience."}
    r = _apply_years_rule({"score": 8, "keywords": "", "reasoning": "fits"}, job)
    assert r["score"] == 3 and "Capped from 8" in r["reasoning"]
    job["full_description"] = "0-2 years of experience."
    r = _apply_years_rule({"score": 8, "keywords": "", "reasoning": "fits"}, job)
    assert r["score"] == 8


# -- UAE region on company boards -----------------------------------------------

@pytest.mark.parametrize("title,locs,expected", [
    ("Backend Engineer", ["Dubai, United Arab Emirates"], "UAE"),
    ("Data Analyst", ["Remote - UAE"], "UAE"),
    ("Product Analyst (UAE)", ["Abu Dhabi"], "UAE"),
    ("Engineer", ["Riyadh, Saudi Arabia"], ""),
    ("Engineer", ["Bangalore, India", "Dubai"], "India"),
])
def test_uae_classification(title, locs, expected):
    p = Posting(company="X", title=title, url="u", apply_url="u", locations=locs)
    assert classify_location(p) == expected


# -- kie.ai provider --------------------------------------------------------------

def _kie(monkeypatch, handler, model="claude-haiku-5-5", fallback=""):
    monkeypatch.setattr(llm, "_kie_backoff", lambda *a, **k: 0)
    monkeypatch.setattr(llm, "_fallback_clients", {})
    if fallback:
        monkeypatch.setenv("LLM_FALLBACK_MODEL", fallback)
    else:
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    real_init = llm.KieClaudeClient.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._client = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(llm.KieClaudeClient, "__init__", init)
    return llm.KieClaudeClient(model, "key")


def test_kie_messages_ok(monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "SCORE: 8"}]})

    c = _kie(monkeypatch, handler)
    assert c.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]) == "SCORE: 8"
    assert seen["body"]["system"] == "sys" and seen["body"]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("status,body", [
    (402, {"msg": "Insufficient credits"}),
    (400, {"error": {"message": "prompt is too long: 250000 tokens > 200000 maximum"}}),
    (200, {"code": 429, "msg": "You exceeded your current quota"}),
])
def test_kie_limit_switches_to_fallback_immediately(monkeypatch, status, body):
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        if model == "claude-haiku-5-5":
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"output_text": "from fallback"})

    c = _kie(monkeypatch, handler, fallback="deepseek-v4")
    assert c.chat([{"role": "user", "content": "hi"}]) == "from fallback"
    assert calls == ["claude-haiku-5-5", "deepseek-v4"]  # no pointless retries of the main model


def test_kie_limit_without_fallback_raises(monkeypatch):
    c = _kie(monkeypatch, lambda req: httpx.Response(402, json={"msg": "Insufficient credits"}))
    with pytest.raises(llm.KieLimitError):
        c.chat([{"role": "user", "content": "hi"}])


def test_kie_output_truncated_uses_fallback(monkeypatch):
    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4":
            return httpx.Response(200, json={"status": "incomplete",
                                             "incomplete_details": {"reason": "max_output_tokens"},
                                             "output": [{"type": "reasoning", "content": [{"text": "thinking..."}]}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}]})

    c = _kie(monkeypatch, handler, model="deepseek-v4", fallback="claude-haiku-5-5")
    assert c.chat([{"role": "user", "content": "hi"}]) == "done"


def test_kie_rate_limit_retries_then_fallback(monkeypatch):
    monkeypatch.setattr(llm, "_KIE_RETRIES", 3)
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        if model == "claude-haiku-5-5":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    c = _kie(monkeypatch, handler, fallback="claude-sonnet-5-5")
    assert c.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert calls == ["claude-haiku-5-5"] * 3 + ["claude-sonnet-5-5"]


def test_get_client_kie(monkeypatch):
    monkeypatch.setattr(llm, "_instance", None)
    monkeypatch.setattr(llm, "_model_clients", {})
    monkeypatch.setenv("LLM_PROVIDER", "kie")
    monkeypatch.setenv("KIE_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("LLM_SCORE_MODEL", "deepseek-v4")
    assert isinstance(llm.get_client(), llm.KieClaudeClient)
    assert llm.get_client_for("LLM_SCORE_MODEL").model == "deepseek-v4"
    assert llm.get_client_for("LLM_ENRICH_MODEL") is llm.get_client()
    monkeypatch.setattr(llm, "_instance", None)


# -- jobspy rate-limit watcher ------------------------------------------------------

def test_rate_watch_counts_blocks():
    pytest.importorskip("jobspy")
    from applypilot.discovery.jobspy import _RateWatch
    w = _RateWatch()
    log = logging.getLogger("jobspy.test")
    log.addHandler(w)
    try:
        log.error("LinkedIn response status code 429")
        log.error("Indeed: recaptcha required")
        log.error("something else")
    finally:
        log.removeHandler(w)
    assert w.hits == 2

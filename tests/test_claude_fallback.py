"""Gemini free tier first, Claude API as fallback (mocked HTTP for both)."""

import json

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402  (the Anthropic SDK's HTTP library)

from applypilot import llm  # noqa: E402


def claude_client(monkeypatch, handler, model="claude-opus-5-5"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    c = llm.AnthropicClient(model=model)
    c._client = anthropic.Anthropic(
        api_key="k", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return c


def claude_ok(text="from claude", seen=None):
    def handler(req):
        if seen is not None:
            seen.append((str(req.url), dict(req.headers), json.loads(req.content)))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
    return handler


def gemini_client(monkeypatch, responses):
    """LLMClient on the Gemini compat URL returning the given (status, body) in order."""
    calls = iter(responses)

    def handler(req):
        status, body = next(calls)
        return httpx.Response(status, json=body)

    c = llm.LLMClient(llm._GEMINI_COMPAT_BASE, "gemini-test", "key")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    c._request_interval = 0
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return c


GEMINI_OK = (200, {"choices": [{"message": {"content": "from gemini"}}]})
RATE_LIMIT = (429, {"error": {"message": "Resource exhausted: requests per minute"}})
DAILY_QUOTA = (429, {"error": {"message": "Quota exceeded for metric: requests per day (PerDay)"}})


# -- Claude client ---------------------------------------------------------------

def test_claude_request_shape(monkeypatch):
    seen = []
    c = claude_client(monkeypatch, claude_ok(seen=seen))
    out = c.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                 temperature=0.2, max_tokens=512)
    assert out == "from claude"
    url, headers, body = seen[0]
    assert url.endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["system"] == "sys"
    assert body["max_tokens"] >= 16000            # thinking needs room; 512 would truncate
    assert "temperature" not in body              # rejected by current models
    assert body["fallbacks"] == "default"         # server-side refusal fallback
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")


def test_claude_haiku_has_no_server_fallback(monkeypatch):
    seen = []
    c = claude_client(monkeypatch, claude_ok(seen=seen), model="claude-haiku-5-5")
    c.chat([{"role": "user", "content": "hi"}])
    assert "fallbacks" not in seen[0][2]


def test_claude_credit_exhausted(monkeypatch):
    def handler(req):
        return httpx2.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API."}})
    c = claude_client(monkeypatch, handler)
    with pytest.raises(llm.ClaudeCreditExhausted):
        c.chat([{"role": "user", "content": "hi"}])


def test_claude_refusal_raises(monkeypatch):
    def handler(req):
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": "refusal", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0}})
    c = claude_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="declined"):
        c.chat([{"role": "user", "content": "hi"}])


# -- Gemini first, Claude fallback ------------------------------------------------

def make_fallback(monkeypatch, gemini_responses):
    g = gemini_client(monkeypatch, gemini_responses)
    g.fail_fast_on_rate_limit = True
    calls = []
    c = claude_client(monkeypatch, claude_ok(seen=calls))
    return llm.FallbackClient(g, c, "Gemini", "Claude"), calls


def test_uses_gemini_when_it_works(monkeypatch):
    fb, claude_calls = make_fallback(monkeypatch, [GEMINI_OK])
    assert fb.ask("hi") == "from gemini"
    assert claude_calls == []


def test_rate_limit_falls_back_for_that_request_only(monkeypatch):
    fb, claude_calls = make_fallback(monkeypatch, [RATE_LIMIT, RATE_LIMIT, GEMINI_OK])
    assert fb.ask("one") == "from claude"   # one retry on Gemini, then Claude
    assert fb.ask("two") == "from gemini"   # next request tries Gemini again
    assert len(claude_calls) == 1


def test_daily_quota_switches_for_rest_of_run(monkeypatch):
    fb, claude_calls = make_fallback(monkeypatch, [DAILY_QUOTA])
    assert fb.ask("one") == "from claude"
    assert fb.ask("two") == "from claude"   # Gemini not called again (iterator would be exhausted)
    assert len(claude_calls) == 2


def test_both_exhausted_stops_batch(monkeypatch):
    g = gemini_client(monkeypatch, [DAILY_QUOTA])

    def broke(req):
        return httpx2.Response(402, json={"type": "error", "error": {
            "type": "billing_error", "message": "Payment required"}})
    fb = llm.FallbackClient(g, claude_client(monkeypatch, broke), "Gemini", "Claude")
    with pytest.raises(llm.LLMQuotaExhausted):   # scorer catches this and stops the batch
        fb.ask("hi")


# -- provider selection ------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "ANTHROPIC_API_KEY",
              "LLM_FALLBACK_PROVIDER", "ANTHROPIC_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_instance", None)
    yield monkeypatch
    llm._instance = None


def test_gemini_plus_claude_key_gives_fallback(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "g")
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    c = llm.get_client()
    assert isinstance(c, llm.FallbackClient)
    assert c.primary._is_gemini and c.primary.fail_fast_on_rate_limit
    assert c.fallback.model == "claude-haiku-5-5"
    assert c.primary.model == "gemini-3.5-flash"


def test_gemini_only_has_no_fallback(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "g")
    c = llm.get_client()
    assert isinstance(c, llm.LLMClient) and not c.fail_fast_on_rate_limit


def test_claude_only(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    clean_env.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    c = llm.get_client()
    assert isinstance(c, llm.AnthropicClient) and c.model == "claude-sonnet-5-5"


def test_fallback_can_be_disabled(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "g")
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    clean_env.setenv("LLM_FALLBACK_PROVIDER", "none")
    assert isinstance(llm.get_client(), llm.LLMClient)

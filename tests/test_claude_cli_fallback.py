"""Claude subscription via the Claude Code CLI as the fallback (fake `claude` binary)."""

import json
import os
import stat

import pytest

from applypilot import llm


def fake_claude(tmp_path, monkeypatch, *, stdout, code=0):
    """Create an executable `claude` that prints `stdout` and exits with `code`."""
    exe = tmp_path / "claude"
    exe.write_text(f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{stdout}'\nexit {code}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BIN", str(exe))
    return exe


@pytest.fixture
def env(monkeypatch):
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "ANTHROPIC_API_KEY",
              "LLM_FALLBACK_PROVIDER", "CLAUDE_MODEL", "CLAUDE_BIN", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_instance", None)
    yield monkeypatch
    llm._instance = None


def test_cli_ok(tmp_path, env):
    fake_claude(tmp_path, env, stdout=json.dumps({"is_error": False, "result": "OK"}))
    assert llm.ClaudeCLIClient("haiku").ask("hi") == "OK"


def test_cli_usage_limit_is_quota(tmp_path, env):
    fake_claude(tmp_path, env, stdout=json.dumps(
        {"is_error": True, "result": "Claude AI usage limit reached. Your limit will reset at 5pm."}), code=1)
    with pytest.raises(llm.LLMQuotaExhausted):
        llm.ClaudeCLIClient("haiku").ask("hi")


def test_cli_not_logged_in_message(tmp_path, env):
    fake_claude(tmp_path, env, stdout=json.dumps(
        {"is_error": True, "result": "Not logged in. Please run /login"}), code=1)
    with pytest.raises(llm.ClaudeCLIError, match="not logged in"):
        llm.ClaudeCLIClient("haiku").ask("hi")


def test_cli_other_error_is_not_quota(tmp_path, env):
    fake_claude(tmp_path, env, stdout=json.dumps({"is_error": True, "result": "API Error: 529 overloaded"}), code=1)
    with pytest.raises(llm.ClaudeCLIError) as ei:
        llm.ClaudeCLIClient("haiku").ask("hi")
    assert not isinstance(ei.value, llm.LLMQuotaExhausted)


def test_gemini_with_cli_fallback(tmp_path, env):
    fake_claude(tmp_path, env, stdout=json.dumps({"is_error": False, "result": "OK"}))
    env.setenv("GEMINI_API_KEY", "g")
    env.setenv("ANTHROPIC_API_KEY", "a")          # ignored: CLI chosen explicitly
    env.setenv("LLM_FALLBACK_PROVIDER", "claude_cli")
    c = llm.get_client()
    assert isinstance(c, llm.FallbackClient)
    assert isinstance(c.fallback, llm.ClaudeCLIClient) and c.fallback.model == "haiku"
    assert c.primary.fail_fast_on_rate_limit


def test_cli_fallback_missing_binary_runs_without_fallback(env, monkeypatch):
    monkeypatch.setattr(llm, "claude_cli_path", lambda: None)
    env.setenv("GEMINI_API_KEY", "g")
    env.setenv("LLM_FALLBACK_PROVIDER", "claude_cli")
    assert isinstance(llm.get_client(), llm.LLMClient)


def test_claude_path_finds_local_bin(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / ".local/bin").mkdir(parents=True)
    exe = tmp_path / ".local/bin/claude"
    exe.write_text("#!/bin/sh\n")
    assert llm.claude_cli_path() == str(exe)

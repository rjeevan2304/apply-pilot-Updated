"""Empty tailoring output must not become an empty/broken PDF; Gemini gets token room."""

import json

import httpx
import pytest

from applypilot import llm


def test_gemini_gets_minimum_output_tokens(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    c = llm.LLMClient(llm._GEMINI_COMPAT_BASE, "gemini-3.5-flash", "k")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    c._request_interval = 0
    c.chat([{"role": "user", "content": "x"}], max_tokens=2048)
    assert seen["max_tokens"] >= 8192


def test_non_gemini_keeps_requested_tokens(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = llm.LLMClient("http://local/v1", "local-model", "")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    c._request_interval = 0
    c.chat([{"role": "user", "content": "x"}], max_tokens=2048)
    assert seen["max_tokens"] == 2048


def test_truncated_answer_is_logged(caplog):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": '}, "finish_reason": "length"}]})

    c = llm.LLMClient("http://local/v1", "m", "")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    c._request_interval = 0
    assert c.chat([{"role": "user", "content": "x"}]) == '{"a": '
    assert "cut off" in caplog.text


def test_no_resume_written_when_tailoring_produces_nothing(tmp_path, monkeypatch):
    from applypilot.scoring import tailor

    resume = tmp_path / "resume.txt"
    resume.write_text("my resume")
    monkeypatch.setattr(tailor, "RESUME_PATH", resume)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "out")
    monkeypatch.setattr(tailor, "load_profile", lambda: {})
    job = {"url": "u1", "title": "Analyst", "site": "x", "fit_score": 8, "full_description": "d"}
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **k: [job])

    class Conn:
        def execute(self, *a):
            return self
        def commit(self):
            pass
    monkeypatch.setattr(tailor, "get_connection", lambda: Conn())
    monkeypatch.setattr(tailor, "tailor_resume", lambda *a, **k: (
        "", {"status": "exhausted_retries", "attempts": 4, "last_error": "invalid JSON from LLM"}))

    r = tailor.run_tailoring(limit=1)
    assert r["approved"] == 0 and r["failed"] == 1
    assert r["statuses"] == {"no_output": 1}
    assert not list((tmp_path / "out").glob("*.txt"))


def test_invalid_pdf_is_removed_and_empty_txt_skipped(tmp_path, monkeypatch):
    from applypilot.scoring import pdf

    txt = tmp_path / "r.txt"
    txt.write_text("NAME\nTitle\n" + "line\n" * 60)
    monkeypatch.setattr(pdf, "render_pdf", lambda html, out: open(out, "wb").write(b"tiny"))
    with pytest.raises(RuntimeError, match="invalid or empty"):
        pdf.convert_to_pdf(txt)
    assert not txt.with_suffix(".pdf").exists()

    monkeypatch.setattr(pdf, "TAILORED_DIR", tmp_path)
    (tmp_path / "empty.txt").write_text("")
    calls = []
    monkeypatch.setattr(pdf, "convert_to_pdf", lambda f: calls.append(f.name))
    pdf.batch_convert()
    assert calls == ["r.txt"]

"""Work is saved as it happens, Workday resumes, and the cycle runs Workday last."""

import sqlite3

import pytest

from applypilot.scoring import scorer, tailor


class Stop(BaseException):
    """Simulates the run being killed mid-way (not caught by `except Exception`)."""


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, site TEXT, location TEXT, "
               "full_description TEXT, fit_score INTEGER, score_reasoning TEXT, scored_at TEXT, "
               "tailored_resume_path TEXT, tailored_at TEXT, tailor_attempts INTEGER)")
    return db


def test_scores_saved_one_by_one_even_if_run_stops(tmp_path, monkeypatch):
    db = _db()
    for i in range(4):
        db.execute("INSERT INTO jobs (url, title, site, full_description) VALUES (?,?,?,?)",
                   (f"u{i}", "Analyst", "x", "0-2 years"))
    (tmp_path / "r.txt").write_text("resume")
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "r.txt")
    monkeypatch.setattr(scorer, "get_connection", lambda: db)
    monkeypatch.setattr(scorer, "get_jobs_by_stage", lambda **k: [
        dict(r) for r in db.execute("SELECT * FROM jobs WHERE fit_score IS NULL ORDER BY url")])
    calls = []

    def fake(resume, job):
        calls.append(job["url"])
        if len(calls) == 3:
            raise Stop()
        return {"score": 8, "keywords": "k", "reasoning": "r"}

    monkeypatch.setattr(scorer, "score_job", fake)
    with pytest.raises(Stop):
        scorer.run_scoring()
    scored = [r[0] for r in db.execute("SELECT url FROM jobs WHERE fit_score IS NOT NULL ORDER BY url")]
    assert scored == ["u0", "u1"]


def test_tailored_resume_saved_before_next_job(tmp_path, monkeypatch):
    db = _db()
    for i in range(2):
        db.execute("INSERT INTO jobs (url, title, site, fit_score, full_description) VALUES (?,?,?,?,?)",
                   (f"u{i}", f"Role {i}", "x", 8, "desc"))
    (tmp_path / "r.txt").write_text("resume")
    monkeypatch.setattr(tailor, "RESUME_PATH", tmp_path / "r.txt")
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "out")
    monkeypatch.setattr(tailor, "load_profile", lambda: {})
    monkeypatch.setattr(tailor, "get_connection", lambda: db)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **k: [
        dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY url")])
    n = []

    def fake(resume, job, profile, validation_mode="normal"):
        n.append(1)
        if len(n) == 2:
            raise Stop()
        return "NAME\nresume text " * 20, {"status": "approved", "attempts": 1}

    monkeypatch.setattr(tailor, "tailor_resume", fake)
    import applypilot.scoring.pdf as pdf
    monkeypatch.setattr(pdf, "convert_to_pdf", lambda p: (tmp_path / "x.pdf").write_bytes(b"%PDF") and tmp_path / "x.pdf")
    with pytest.raises(Stop):
        tailor.run_tailoring(limit=5)
    row = db.execute("SELECT tailored_resume_path FROM jobs WHERE url='u0'").fetchone()
    assert row[0] and row[0].endswith(".txt")


def test_workday_resumes_where_it_stopped(tmp_path, monkeypatch):
    from applypilot.discovery import workday

    monkeypatch.setattr(workday.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(workday, "init_db", lambda: None)
    monkeypatch.setattr(workday, "load_employers", lambda: {k: {"name": k} for k in ("A", "B", "C")})
    monkeypatch.setattr(workday.config, "load_search_config", lambda: {
        "queries": [{"query": "q1", "tier": 1}, {"query": "q2", "tier": 1}]})
    seen = []

    def fake_one(key, employers, query, *a):
        seen.append((query, key))
        if len(seen) == 4:      # stop during q2 / employer A
            raise Stop()
        return {"new": 0, "existing": 0, "found": 0}

    monkeypatch.setattr(workday, "_process_one", fake_one)
    with pytest.raises(Stop):
        workday.run_workday_discovery()
    assert seen == [("q1", "A"), ("q1", "B"), ("q1", "C"), ("q2", "A")]

    seen.clear()
    monkeypatch.setattr(workday, "_process_one", lambda key, e, query, *a: seen.append((query, key))
                        or {"new": 0, "existing": 0, "found": 0})
    workday.run_workday_discovery()
    assert seen == [("q2", "A"), ("q2", "B"), ("q2", "C")]     # resumed, not restarted

    seen.clear()
    workday.run_workday_discovery()
    assert seen[0] == ("q1", "A")                              # full pass done -> from the top


def test_cycle_publishes_before_workday(monkeypatch):
    from applypilot import pipeline

    order = []
    monkeypatch.setattr(pipeline, "load_env", lambda: None)
    monkeypatch.setattr(pipeline, "ensure_dirs", lambda: None)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "_discover_fast", lambda w: order.append("india+uae+companies") or {})
    monkeypatch.setattr(pipeline, "_discover_slow", lambda w: order.append("other sites + workday") or {})
    monkeypatch.setattr(pipeline, "_run_enrich", lambda *a: order.append("enrich"))
    monkeypatch.setattr(pipeline, "_run_pdf", lambda: order.append("pdf"))
    monkeypatch.setattr(pipeline, "get_stats", lambda: {"untailored_eligible": 0, "tailored": 0})
    batches = iter([{"scored": 50}, {"scored": 3}, {"scored": 0}, {"scored": 0}])
    monkeypatch.setattr("applypilot.scoring.scorer.run_scoring",
                        lambda limit=0: order.append("score") or next(batches))
    monkeypatch.setattr("applypilot.scoring.tailor.run_tailoring",
                        lambda **k: order.append("tailor") or {"approved": 1})
    pipeline.run_cycle(publish=lambda: order.append("PUBLISH"))

    workday_at = order.index("other sites + workday")
    assert order[0] == "india+uae+companies"
    assert "PUBLISH" in order[:workday_at]                      # results visible before Workday
    assert order.count("PUBLISH") >= 4                         # after every scoring batch
    assert order[-1] == "PUBLISH"


def test_cycle_stops_scoring_when_quota_is_used_up(monkeypatch):
    from applypilot import pipeline

    calls = []
    monkeypatch.setattr(pipeline, "_run_enrich", lambda *a: None)
    monkeypatch.setattr(pipeline, "_run_pdf", lambda: None)
    monkeypatch.setattr(pipeline, "get_stats", lambda: {"untailored_eligible": 0, "tailored": 0})
    monkeypatch.setattr("applypilot.scoring.scorer.run_scoring",
                        lambda limit=0: calls.append(1) or {"scored": 10, "quota_exhausted": True})
    monkeypatch.setattr("applypilot.scoring.tailor.run_tailoring", lambda **k: {"approved": 0})
    res = pipeline._process(7, "normal", False, 50, lambda: None, "test")
    assert len(calls) == 1 and "quota" in res["stopped_reason"]

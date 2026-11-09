"""Senior/lead titles are dropped at search time and never sent to the LLM."""

import sqlite3

from applypilot.discovery.filters import title_ok


def test_new_senior_words():
    for t in ("Leader, Software Engineering", "Distinguished Engineer", "AVP Technology",
              "SVP Product", "Fellow, AI", "Engineering Leadership Program"):
        assert not title_ok(t), t
    for t in ("Associate Product Manager", "Program Manager", "Forward Deployed Engineer"):
        assert title_ok(t), t


def test_scorer_skips_senior_titles_without_llm(tmp_path, monkeypatch):
    from applypilot.scoring import scorer

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, site TEXT, location TEXT, "
               "full_description TEXT, fit_score INTEGER, score_reasoning TEXT, scored_at TEXT)")
    db.executemany("INSERT INTO jobs (url, title, site, full_description) VALUES (?,?,?,?)", [
        ("a", "Senior Compiler Engineer", "NVIDIA", "d"),
        ("b", "ASIC DFT DV Technical Lead", "Cisco", "d"),
        ("c", "Associate Product Manager", "Swiggy", "d"),
    ])
    (tmp_path / "r.txt").write_text("resume")
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "r.txt")
    monkeypatch.setattr(scorer, "get_connection", lambda: db)
    monkeypatch.setattr(scorer, "get_jobs_by_stage", lambda **k: [
        dict(r) for r in db.execute("SELECT * FROM jobs WHERE fit_score IS NULL")])
    calls = []
    monkeypatch.setattr(scorer, "score_job", lambda resume, job: calls.append(job["url"])
                        or {"score": 8, "keywords": "k", "reasoning": "r"})
    res = scorer.run_scoring()
    assert calls == ["c"] and res["skipped"] == 2
    rows = dict(db.execute("SELECT url, fit_score FROM jobs").fetchall())
    assert rows == {"a": 1, "b": 1, "c": 8}


def test_scoring_loop_continues_past_a_batch_of_only_skipped_titles(monkeypatch):
    from applypilot import pipeline

    batches = iter([{"scored": 0, "skipped": 50}, {"scored": 7}, {"scored": 0}])
    calls = []
    monkeypatch.setattr(pipeline, "_run_enrich", lambda *a: None)
    monkeypatch.setattr(pipeline, "_run_pdf", lambda: None)
    monkeypatch.setattr(pipeline, "get_stats", lambda: {"untailored_eligible": 0, "tailored": 0})
    monkeypatch.setattr("applypilot.scoring.scorer.run_scoring",
                        lambda limit=0: calls.append(1) or next(batches))
    monkeypatch.setattr("applypilot.scoring.tailor.run_tailoring", lambda **k: {"approved": 0})
    pipeline._process(7, "normal", False, 50, lambda: None, "t")
    assert len(calls) == 3


def test_workday_and_other_sites_drop_senior_titles(monkeypatch, tmp_path):
    from applypilot import database
    from applypilot.discovery import workday

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, salary TEXT, description TEXT, "
               "location TEXT, site TEXT, strategy TEXT, discovered_at TEXT)")
    new, _ = database.store_jobs(db, [{"url": "1", "title": "Sr Engineer"}, {"url": "2", "title": "Data Analyst"}],
                                 site="X", strategy="s")
    assert new == 1 and [r[0] for r in db.execute("SELECT title FROM jobs")] == ["Data Analyst"]

    fetched = []
    monkeypatch.setattr(workday, "search_employer", lambda *a, **k: [
        {"title": "Senior System Software Engineer"}, {"title": "Software Engineer I"}])
    monkeypatch.setattr(workday, "fetch_details", lambda emp, jobs: fetched.extend(jobs) or jobs)
    monkeypatch.setattr(workday, "get_connection", lambda: None)
    monkeypatch.setattr(workday, "store_results", lambda conn, jobs, emps: (len(jobs), 0))
    r = workday._process_one("nv", {"nv": {"name": "NVIDIA"}}, "q", True, [], [])
    assert r["new"] == 1 and [j["title"] for j in fetched] == ["Software Engineer I"]

"""Postings asking for 5+ years are scored 3 without calling the LLM."""

import sqlite3

from applypilot.scoring import scorer


def test_precheck_skips_llm_for_senior_postings(tmp_path, monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, site TEXT, location TEXT, "
               "full_description TEXT, fit_score INTEGER, score_reasoning TEXT, scored_at TEXT)")
    db.executemany("INSERT INTO jobs (url, title, site, location, full_description) VALUES (?,?,?,?,?)", [
        ("u1", "Product Analyst", "x", "Hyderabad", "We need 6+ years of experience in analytics."),
        ("u2", "Product Analyst", "x", "Hyderabad", "0-2 years experience. Python, SQL."),
        ("https://www.naukri.com/job-listings-analyst-acme-hyderabad-8-to-12-years-1", "Analyst", "naukri",
         "Hyderabad", "Great role."),
    ])
    resume = tmp_path / "resume.txt"
    resume.write_text("resume")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "get_connection", lambda: db)
    monkeypatch.setattr(scorer, "get_jobs_by_stage", lambda **k: [
        dict(r) for r in db.execute("SELECT * FROM jobs WHERE fit_score IS NULL")])
    llm_calls = []

    def fake_score(resume_text, job):
        llm_calls.append(job["url"])
        return {"score": 8, "keywords": "k", "reasoning": "fits"}

    monkeypatch.setattr(scorer, "score_job", fake_score)
    scorer.run_scoring()
    assert llm_calls == ["u2"]
    scores = dict(db.execute("SELECT url, fit_score FROM jobs").fetchall())
    assert scores["u1"] == 3 and scores["u2"] == 8
    assert scores["https://www.naukri.com/job-listings-analyst-acme-hyderabad-8-to-12-years-1"] == 3

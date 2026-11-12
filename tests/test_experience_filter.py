"""Postings asking for more than MAX_YEARS_REQUIRED years are left out of the shortlist."""

from applypilot import export
from applypilot.scoring.scorer import min_years_required


def test_years_read_from_postings():
    assert min_years_required("Analyst", None, "3-4 years experience") == 3
    assert min_years_required("Analyst", None, "Minimum 3 years of experience") == 3
    assert min_years_required("Analyst", None, "0-2 years experience") == 2
    assert min_years_required("Analyst", None, "Great team") is None


def test_threshold_is_two_years():
    assert export.MAX_YEARS_REQUIRED == 2


def test_shortlist_leaves_out_jobs_asking_for_more(tmp_path, monkeypatch):
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE jobs (url TEXT, title TEXT, company TEXT, site TEXT, location TEXT, region TEXT, "
               "fit_score INTEGER, tailored_resume_path TEXT, tailored_at TEXT, full_description TEXT, "
               "application_url TEXT, discovered_at TEXT, salary TEXT, score_reasoning TEXT, "
               "cover_letter_path TEXT)")
    rows = [
        ("a", "Analyst", "Acme", "Hyderabad, India", "3-4 years of experience required"),
        ("b", "Analyst", "Beta", "Bengaluru, India", "1 year of experience, Python"),
        ("c", "Analyst", "Gamma", "Remote (Remote)", "At least 2 years experience"),
    ]
    for u, t, c, loc, desc in rows:
        db.execute("INSERT INTO jobs (url,title,company,site,location,region,fit_score,tailored_resume_path,"
                   "full_description) VALUES (?,?,?,?,?,NULL,8,'/x.txt',?)", (u, t, c, "x", loc, desc))
    monkeypatch.setattr(export, "get_connection", lambda: db)
    monkeypatch.setattr(export, "default_export_dir", lambda: tmp_path)
    monkeypatch.setattr(export, "_pdf_or_txt", lambda p: (None, None))
    res = export.export_shortlist(output_dir=tmp_path / "out", copy_files=False, write_status=False)
    assert sorted(r["company"] for r in res["records"]) == ["Beta", "Gamma"]

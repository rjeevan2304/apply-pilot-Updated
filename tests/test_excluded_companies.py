"""Excluded companies are left out of the search results and the shortlist."""

from applypilot.discovery import filters


def test_matching_is_case_insensitive_and_whole_word():
    assert filters.company_excluded("Scoutit")
    assert filters.company_excluded("SCOUTIT Technologies Pvt Ltd")
    assert filters.company_excluded("Joblet-AI")
    assert filters.company_excluded("TalentRabbit")
    assert not filters.company_excluded("Scoutitude Labs")
    assert not filters.company_excluded("Microsoft")


def test_export_drops_excluded(tmp_path, monkeypatch):
    import sqlite3
    from applypilot import export
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE jobs (url TEXT, title TEXT, company TEXT, site TEXT, location TEXT, region TEXT, "
               "fit_score INTEGER, tailored_resume_path TEXT, tailored_at TEXT, full_description TEXT, "
               "application_url TEXT, discovered_at TEXT, salary TEXT, score_reasoning TEXT, cover_letter_path TEXT)")
    for u, c in [("a", "Scoutit"), ("b", "Acme")]:
        db.execute("INSERT INTO jobs (url,title,company,site,location,region,fit_score,tailored_resume_path,"
                   "full_description) VALUES (?,?,?,?,?,NULL,8,'/x.txt','1 year experience')",
                   (u, "Analyst", c, "x", "Hyderabad, India"))
    monkeypatch.setattr(export, "get_connection", lambda: db)
    monkeypatch.setattr(export, "_pdf_or_txt", lambda p: (None, None))
    res = export.export_shortlist(output_dir=tmp_path / "out", copy_files=False, write_status=False)
    assert [r["company"] for r in res["records"]] == ["Acme"]

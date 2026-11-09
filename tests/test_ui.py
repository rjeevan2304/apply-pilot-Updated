"""Local dashboard: API, colour labels, safe file serving, status.json."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from applypilot.ui import server as ui


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "UI_HOME", tmp_path / "ui-home")
    short = tmp_path / "shortlist"
    folder = short / "jobs" / "Acme - Analyst [abc123]"
    folder.mkdir(parents=True)
    (folder / "Resume.pdf").write_bytes(b"%PDF-1.4 test")
    (folder / "job.txt").write_text("Company: Acme\n\nWhy it fits:\ngood\n\n" + "-" * 60 + "\nDesc")
    (short / "shortlist.csv").write_text(
        "company,title,region,location,fit_score,apply_url,job_url,folder,resume_pdf\n"
        "Acme,Analyst,India,Hyderabad,8,https://a/apply,https://a/job,jobs/Acme - Analyst [abc123],x.pdf\n")
    (short / "status.json").write_text(json.dumps({"running": False, "stats": {"total": 5}}))
    (short / "secret.txt").write_text("nope")
    app = ui.App(ui.LocalSource(short))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, app, tmp_path
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def post(url, obj):
    req = urllib.request.Request(url, json.dumps(obj).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_status_and_jobs(site):
    base, _, _ = site
    st = json.loads(get(base + "/api/status")[2])
    assert st["status"]["stats"]["total"] == 5 and st["job_count"] == 1
    jobs = json.loads(get(base + "/api/jobs")[2])
    assert jobs[0]["company"] == "Acme" and jobs[0]["index"] == 1 and jobs[0]["decision"] is None
    assert b"ApplyPilot" in get(base + "/")[2]


def test_files_served_only_from_jobs(site):
    base, _, _ = site
    code, ctype, body = get(base + "/files/jobs/Acme%20-%20Analyst%20%5Babc123%5D/Resume.pdf")
    assert code == 200 and ctype == "application/pdf" and body.startswith(b"%PDF")
    for bad in ("/files/secret.txt", "/files/shortlist.csv", "/files/jobs/%2E%2E/secret.txt"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + bad)
        assert e.value.code in (400, 403, 404)


def test_colour_labels_saved_and_validated(site):
    base, app, tmp = site
    e = post(base + "/api/decision", {"url": "https://a/job", "decision": "lowpay", "note": "₹ too low"})
    assert e["decision"] == "lowpay" and e["note"] == "₹ too low"
    assert json.loads(get(base + "/api/jobs")[2])[0]["decision"] == "lowpay"
    saved = json.loads((tmp / "ui-home" / "decisions.json").read_text())
    assert saved["https://a/job"]["decision"] == "lowpay"
    post(base + "/api/decision", {"url": "https://a/job", "decision": ""})
    assert json.loads(get(base + "/api/jobs")[2])[0]["decision"] is None
    with pytest.raises(urllib.error.HTTPError) as err:
        post(base + "/api/decision", {"url": "https://a/job", "decision": "purple"})
    assert err.value.code == 400


def test_newer_remote_label_wins_on_merge(site):
    _, app, _ = site
    app.decisions = {"u": {"decision": "retry", "updated": "2026-01-01T00:00:00+00:00"}}
    app._merge({"u": {"decision": "apply", "updated": "2026-02-01T00:00:00+00:00"},
                "v": {"decision": "no", "updated": "2026-01-01T00:00:00+00:00"}})
    assert app.decisions["u"]["decision"] == "apply" and app.decisions["v"]["decision"] == "no"
    app._merge({"u": {"decision": "retry", "updated": "2025-12-01T00:00:00+00:00"}})
    assert app.decisions["u"]["decision"] == "apply"


def test_status_file_written_by_export(tmp_path, monkeypatch):
    import sqlite3
    from applypilot import statusfile

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE jobs (url TEXT, region TEXT, fit_score INT, tailored_resume_path TEXT)")
    db.executemany("INSERT INTO jobs VALUES (?,?,?,?)", [("a", None, 8, "p"), ("b", "UAE", 3, None)])
    monkeypatch.setattr("applypilot.database.get_connection", lambda *a, **k: db)
    monkeypatch.setattr("applypilot.database.get_stats", lambda conn=None: {"total": 2, "by_site": [("x", 2)]})
    p = statusfile.write_status(tmp_path, running=False)
    d = json.loads(p.read_text())
    assert d["running"] is False and d["stats"]["total"] == 2 and d["last_run_finished_at"]
    assert {r["region"]: r["strong"] for r in d["by_region"]} == {"India": 1, "UAE": 0}

"""Gmail application sync: classification, titles, dedupe and output. No real Gmail access."""

import json

import pytest

from applypilot import gmail_sync as gs


@pytest.mark.parametrize("subject,snippet,want", [
    ("Application Rejection for the position -0472- Front End Developer", "We regret to inform you", "rejected"),
    ("Thank You for Applying to Brain Co.", "", "applied"),
    ("We’ve received your Target job application", "", "applied"),
    ("Your IBM Application: Next Steps", "", "next_steps"),
    ("SDE-I Online Assessment Invitation", "", "assessment"),
    ("Please complete the coding assessment", "", "assessment"),
    ("We'd like to schedule an interview", "", "interview"),
    ("Apply for assessment for contract CN112540448", "Kindly apply for assessment.", "assessment"),  # gov.in notices are dropped in build_records
    ("Congratulations, we are delighted to offer you the role", "", "offer"),
    ("Creation of your account for Genpact application", "Your account has been created", None),
    ("Customer, your ₹750 voucher offer ends 31 Oct", "", None),
])
def test_classify(subject, snippet, want):
    assert gs.classify(subject, snippet) == want


def test_titles_only_when_they_look_like_roles():
    assert gs.title_from_subject("Indeed Application: AI Engineer 4A") == "AI Engineer 4A"
    assert gs.title_from_subject("Thank You for Applying to Brain Co.") == ""
    assert gs.title_from_subject("Thank you for applying for the SDET - II role at CertifyOS").startswith("SDET")


def test_company_from_sender():
    assert gs.company_from_sender("Brain Co. Hiring Team", "no-reply@ashbyhq.com") == "Brain Co"
    assert gs.company_from_sender("", "target@myworkday.com") == "Target"


def test_latest_status_wins_per_role_and_unknown_roles_kept_separately():
    msgs = [
        {"threadId": "t1", "subject": "Indeed Application: AI Engineer", "snippet": "", "from": "x <a@indeed.com>", "date": "2026-11-01"},
        {"threadId": "t2", "subject": "Application Rejection for the position AI Engineer", "snippet": "",
         "from": "Acme Careers <a@acme.com>", "date": "2026-11-05"},
        {"threadId": "t3", "subject": "Thank you for your application!", "snippet": "", "from": "Foo <a@foo.com>", "date": "2026-11-02"},
        {"threadId": "t4", "subject": "Thank you for your application!", "snippet": "", "from": "Foo <a@foo.com>", "date": "2026-11-03"},
    ]
    recs = gs.build_records(msgs)
    acme = [r for r in recs if r["company"] == "Acme"][0]
    assert acme["status"] == "rejected" and acme["date"] == "2026-11-05"
    assert len([r for r in recs if r["company"] == "Foo"]) == 2     # no title: kept one per email


class FakeService:
    def __init__(self, threads):
        self._threads = threads

    def users(self):
        return self

    def threads(self):
        return self

    def list(self, **kw):
        class R:
            def execute(s):
                return {"threads": [{"id": t["id"]} for t in self._threads]}
        return R()

    def get(self, id, **kw):
        t = next(x for x in self._threads if x["id"] == id)
        class R:
            def execute(s):
                return {"messages": [{"snippet": t["snippet"], "payload": {"headers": [
                    {"name": "Subject", "value": t["subject"]}, {"name": "From", "value": t["from"]},
                    {"name": "Date", "value": "Thu, 05 Nov 2026 12:00:00 +0000"}]}}]}
        return R()


def test_fetch_and_write_output(tmp_path, monkeypatch):
    threads = [{"id": "t1", "subject": "Thanks for applying at CertifyOS!", "snippet": "",
                "from": "CertifyOS Hiring Team <no-reply@ashbyhq.com>"}]
    monkeypatch.setattr(gs, "_credentials", lambda: object())
    import googleapiclient.discovery as d
    monkeypatch.setattr(d, "build", lambda *a, **k: FakeService(threads))
    res = gs.sync(out_dir=tmp_path)
    data = json.loads((tmp_path / "applications.json").read_text())
    assert res["count"] == 1 and data["applications"][0]["company"] == "CertifyOS"
    assert data["applications"][0]["date"] == "2026-11-05" and data["applications"][0]["status"] == "applied"
    assert set(data["applications"][0]) == {"company", "title", "date", "status", "note"}


def test_not_connected_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="gmail-login"):
        gs.sync(out_dir=tmp_path)


def test_government_notices_ignored():
    msgs = [{"threadId": "g1", "subject": "Apply for assessment for contract", "snippet": "",
             "from": "Apprenticeship India <no-reply@apprenticeshipindia.gov.in>", "date": "2026-11-03"}]
    assert gs.build_records(msgs) == []


def test_before_october_is_dropped():
    msgs = [
        {"threadId": "a", "subject": "Thank you for applying to Acme", "snippet": "", "from": "Acme <a@acme.com>", "date": "2026-09-20"},
        {"threadId": "b", "subject": "Thank you for applying to Beta", "snippet": "", "from": "Beta <a@beta.com>", "date": "2026-10-03"},
    ]
    assert [r["company"] for r in gs.build_records(msgs)] == ["Beta"]

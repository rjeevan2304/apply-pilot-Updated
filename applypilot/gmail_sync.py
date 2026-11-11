"""Read application emails from your Gmail (read-only) and publish your application status.

One-time setup on the VM (see README "Gmail access"):
  1. Save your OAuth client file as ~/.applypilot/gmail_client.json
  2. applypilot gmail-login      (approve access once in your browser, paste the address back)

After that, every `applypilot cycle` run refreshes applications.json in your bucket,
which the dashboard's Applications tab shows. Only company, role, date, status and a
short note are kept; email text is never stored.

Read-only: the only permission requested is gmail.readonly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from applypilot import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SINCE = "2026-10-01"  # applications from this date onwards (no end date)
CLIENT_FILE = config.APP_DIR / "gmail_client.json"
TOKEN_FILE = config.APP_DIR / "gmail_token.json"
REDIRECT = "http://localhost:8085/"

QUERY = (
    '(subject:(applying OR application OR applied OR "received your" OR interview OR '
    'unfortunately OR regret OR "not moving forward" OR offer OR "next steps" OR assessment OR '
    'test OR "status of your application" OR "update on your application" OR "thank you for your interest")) '
    f"after:{SINCE.replace('-', '/')} -in:spam -in:trash"
)

REJECT_WORDS = ("rejection", "rejected", "unfortunately", "not moving forward", "not be considered", "regret to inform", "not able to proceed",
                "decided not to", "not selected", "will not be moving", "not be progressing",
                "unsuccessful", "not successful", "other candidates", "not proceed")
OFFER_WORDS = ("offer letter", "pleased to offer", "job offer", "delighted to offer",
               "offer of employment", "we would like to offer")
INTERVIEW_WORDS = ("interview", "schedule a call", "screening call", "call with the team", "meet the team")
ASSESSMENT_WORDS = ("assessment", "online test", "coding test", "coding challenge", "hackerrank",
                    "codility", "take-home", "take home", "online exam")
NEXT_STEPS_WORDS = ("next steps", "next step", "we'd like to move forward", "moving you forward")
APPLIED_WORDS = ("thank you for applying", "thanks for applying", "application received",
                 "received your application", "we've received", "we have received",
                 "application confirmation", "thank you for your application", "your application",
                 "thank you for your interest", "applied", "received your", "indeed application",
                 "job application", "application for")


def classify(subject: str, snippet: str) -> str | None:
    """applied / interview / offer / rejected, or None if the email isn't about an application."""
    text = f"{subject} {snippet}".lower()
    if any(w in text for w in REJECT_WORDS):
        return "rejected"
    if any(w in text for w in OFFER_WORDS):
        return "offer"
    if any(w in text for w in INTERVIEW_WORDS):
        return "interview"
    if any(w in text for w in ASSESSMENT_WORDS):
        return "assessment"
    if any(w in text for w in NEXT_STEPS_WORDS):
        return "next_steps"
    if any(w in text for w in APPLIED_WORDS):
        return "applied"
    return None


_ATS_HOSTS = {"myworkday", "greenhouse-mail", "ashbyhq", "workablemail", "teamtailor-mail",
              "jobalerts", "successfactors", "icims", "lever", "smartrecruiters"}
_NOISE = re.compile(r"\b(hiring team|talent acquisition|talent|recruiting|recruitment|careers?|team|"
                    r"people operations|people|hr|notifications?|jobs?|no[- ]?reply|noreply)\b", re.I)


def company_from_sender(display: str, address: str) -> str:
    name = _NOISE.sub(" ", display or "").strip(" -–|,.")
    if not name or "@" in name:
        local, _, domain = (address or "").partition("@")
        host = domain.split(".")
        name = host[-2] if len(host) >= 2 else (host[0] if host else "")
        if name in _ATS_HOSTS:
            # Job platforms send from e.g. target@myworkday.com: the company is in the address.
            name = local.split(".")[0].split("+")[0]
        name = name.replace("-", " ").title()
    return re.sub(r"\s+", " ", name).strip()[:200] or "Unknown"


ROLE_WORDS = re.compile(r"\b(engineer|engineering|developer|analyst|manager|designer|scientist|specialist|"
                       r"architect|intern|associate|consultant|tester|sdet|lead|administrator|"
                       r"technician|coordinator|advocate|researcher|product|solutions?|support)\b", re.I)
_TITLE_PATTERNS = [
    re.compile(r"\bfor (?:the )?(.+?)(?: role| position| job| at | - | – |!|\.|$)", re.I),
    re.compile(r"(?:position|role|job)[:\s]+(.+?)(?: at | - | – |!|\.|$)", re.I),
    re.compile(r"application:\s*(.+?)(?: at | - | – |!|\.|$)", re.I),
]


def title_from_subject(subject: str) -> str:
    for pat in _TITLE_PATTERNS:
        m = pat.search(subject or "")
        if m:
            t = re.sub(r"^(?:applying|application) for (?:the )?", "", m.group(1).strip(" -–:|"), flags=re.I)
            if 3 <= len(t) <= 120 and not re.search(r"\b(your|our|we|thank)\b", t, re.I) \
                    and ROLE_WORDS.search(t):
                return t
    return ""


def build_records(messages: list[dict]) -> list[dict]:
    """messages: [{'threadId', 'subject', 'snippet', 'from', 'date'}] -> application records.

    Newest status wins per (company, title). Emails with no recognisable title are kept one by one.
    """
    recs: dict = {}
    for m in sorted(messages, key=lambda x: x.get("date") or ""):
        if (m.get("date") or "") and m["date"] < SINCE:
            continue  # before the start date
        display, address = parseaddr(m.get("from", ""))
        if address.lower().endswith(".gov.in"):
            continue  # government notices (e.g. apprenticeship attendance) aren't job applications
        status = classify(m.get("subject", ""), m.get("snippet", ""))
        if not status:
            continue
        company = company_from_sender(display, address)
        title = title_from_subject(m.get("subject", ""))
        date = (m.get("date") or "")[:10]
        key = (company.lower(), title.lower()) if title else ("thread", m.get("threadId"))
        recs[key] = {"company": company, "title": title, "date": date, "status": status,
                     "note": (m.get("subject") or "")[:200]}
    return sorted(recs.values(), key=lambda r: r["date"], reverse=True)


# ---------------------------------------------------------------------------
# Gmail access
# ---------------------------------------------------------------------------

def login() -> None:
    """Headless one-time sign-in: prints a link, you approve it, then paste the address back."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_FILE.exists():
        raise SystemExit(f"Save your OAuth client file as {CLIENT_FILE} first (see the README).")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, redirect_uri=REDIRECT)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    print("\n1) Open this link in your browser and approve read-only Gmail access:\n")
    print(url)
    print("\n2) Your browser then shows an error page (that's expected). Copy the whole web address")
    print("   from its address bar (it starts with http://localhost:8085/?code=...) and paste it here:\n")
    response = input("Paste the address: ").strip()
    # The redirect points at localhost on this machine, which the library treats as
    # non-HTTPS. The code only goes from Google to this machine, so allow it here.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow.fetch_token(authorization_response=response)
    creds = flow.credentials
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    print(f"\nDone. Gmail access saved to {TOKEN_FILE} (only you on this VM can read it).")


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def fetch_messages(service, max_threads: int = 300) -> list[dict]:
    out: list[dict] = []
    page = None
    seen = 0
    while seen < max_threads:
        resp = service.users().threads().list(userId="me", q=QUERY, maxResults=100, pageToken=page).execute()
        for t in resp.get("threads", []):
            seen += 1
            full = service.users().threads().get(
                userId="me", id=t["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]
            ).execute()
            for msg in full.get("messages", [])[:1]:   # the first message is the application email
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                date = ""
                try:
                    date = parsedate_to_datetime(headers.get("date", "")).astimezone(timezone.utc).date().isoformat()
                except (TypeError, ValueError):
                    pass
                out.append({"threadId": t["id"], "subject": headers.get("subject", ""),
                            "snippet": msg.get("snippet", ""), "from": headers.get("from", ""), "date": date})
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def sync(gcs_uri: str | None = None, out_dir: Path | None = None) -> dict:
    """Refresh applications.json locally and in the bucket. Returns a small summary."""
    from googleapiclient.discovery import build

    creds = _credentials()
    if creds is None:
        raise RuntimeError("Gmail is not connected yet. Run: applypilot gmail-login")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    records = build_records(fetch_messages(service))
    out_dir = Path(out_dir) if out_dir else config.APP_DIR / "shortlist"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "applications.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"updated": datetime.now().astimezone().isoformat(timespec="seconds"),
                               "applications": records}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    if gcs_uri:
        from applypilot.export import upload_file_to_gcs
        upload_file_to_gcs(path, gcs_uri, "applications.json")
    counts: dict = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info("Gmail sync: %d application(s) %s", len(records), counts)
    return {"count": len(records), "counts": counts, "path": path}

"""Direct discovery from target companies' own job boards.

Reads ``companies.yaml`` (``~/.applypilot/companies.yaml``, else the packaged
default), pulls every opening from each company's public applicant-tracking
API (Greenhouse, Lever, Ashby, Workable, Atlassian), keeps roles a candidate
in India can take (India-based, UAE-based, or remote worldwide/APAC/unrestricted), and
stores them with full descriptions so they go straight to scoring.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.filters import title_ok

log = logging.getLogger(__name__)

_TIMEOUT = 30
_UA = "applypilot/0.3 (+job discovery; public job-board API)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Posting:
    company: str
    title: str
    url: str
    apply_url: str
    locations: list[str] = field(default_factory=list)
    remote: bool | None = None
    description: str = ""
    posted_at: str | None = None
    eligibility: str = ""  # filled by classify_location()

    @property
    def location(self) -> str:
        return "; ".join(dict.fromkeys(loc for loc in self.locations if loc)) or ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def companies_config_path() -> Path:
    user = config.APP_DIR / "companies.yaml"
    return user if user.exists() else config.CONFIG_DIR / "companies.yaml"


def load_companies_config() -> dict:
    path = companies_config_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("companies", [])
    data.setdefault("title_keywords", [])
    data.setdefault("exclude_title_words", [])
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    if "&lt;" in raw or "<" in text:  # Greenhouse double-escapes its HTML
        text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", text)
        text = re.sub(r"(?i)<li[^>]*>", "- ", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# ATS fetchers: each returns a list of Postings, or None if the board doesn't exist
# ---------------------------------------------------------------------------

def _fetch_greenhouse(client: httpx.Client, company: str, slug: str) -> list[Posting] | None:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        locs = [p.strip() for p in loc.split(";") if p.strip()]
        locs += [o.get("name") for o in j.get("offices") or [] if o.get("name")]
        out.append(Posting(
            company=company, title=(j.get("title") or "").strip(),
            url=j.get("absolute_url") or "", apply_url=j.get("absolute_url") or "",
            locations=locs, description=_html_to_text(j.get("content")),
            posted_at=j.get("first_published") or j.get("updated_at"),
        ))
    return out


def _fetch_lever(client: httpx.Client, company: str, slug: str) -> list[Posting] | None:
    r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = list(cats.get("allLocations") or []) or [cats.get("location") or ""]
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{_html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        created = j.get("createdAt")
        out.append(Posting(
            company=company, title=(j.get("text") or "").strip(),
            url=j.get("hostedUrl") or "", apply_url=j.get("applyUrl") or j.get("hostedUrl") or "",
            locations=locs,
            remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
            description="\n\n".join(p for p in parts if p.strip()).strip(),
            posted_at=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else None,
        ))
    return out


def _fetch_ashby(client: httpx.Client, company: str, slug: str) -> list[Posting] | None:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        locs += [s.get("location") for s in j.get("secondaryLocations") or [] if s.get("location")]
        wp = (j.get("workplaceType") or "").lower()
        remote = j.get("isRemote")
        if wp:
            remote = wp == "remote"
        out.append(Posting(
            company=company, title=(j.get("title") or "").strip(),
            url=j.get("jobUrl") or "", apply_url=j.get("applyUrl") or j.get("jobUrl") or "",
            locations=locs, remote=remote,
            description=j.get("descriptionPlain") or _html_to_text(j.get("descriptionHtml")),
            posted_at=j.get("publishedAt"),
        ))
    return out


def _fetch_workable(client: httpx.Client, company: str, slug: str) -> list[Posting] | None:
    r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = []
        for loc in j.get("locations") or []:
            locs.append(", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x))
        if not locs:
            locs.append(", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x))
        url = j.get("url") or j.get("shortlink") or ""
        out.append(Posting(
            company=company, title=(j.get("title") or "").strip(),
            url=url, apply_url=j.get("application_url") or url,
            locations=locs, remote=j.get("telecommuting"),
            description=_html_to_text(j.get("description")),
            posted_at=j.get("published_on") or j.get("created_at"),
        ))
    return out


def _fetch_atlassian(client: httpx.Client, company: str, slug: str | None = None) -> list[Posting] | None:
    r = client.get("https://www.atlassian.com/endpoint/careers/listings")
    r.raise_for_status()
    out = []
    for j in r.json():
        portal = j.get("portalJobPost") or {}
        url = portal.get("portalUrl") or j.get("applyUrl") or ""
        desc = "\n\n".join(_html_to_text(j.get(k)) for k in ("overview", "responsibilities", "qualifications"))
        out.append(Posting(
            company=company, title=(j.get("title") or "").strip(),
            url=url, apply_url=j.get("applyUrl") or url,
            locations=list(j.get("locations") or []),
            description=desc.strip(), posted_at=portal.get("updatedDate"),
        ))
    return out


FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "workable": _fetch_workable,
    "atlassian": _fetch_atlassian,
}


def fetch_company(client: httpx.Client, entry: dict) -> tuple[list[Posting], str]:
    """Fetch all postings for one company entry. Returns (postings, source label)."""
    name = entry["name"]
    ats = (entry.get("ats") or "").lower() or None
    slugs = entry.get("slugs") or ([entry["slug"]] if entry.get("slug") else [])
    if ats == "atlassian":
        return _fetch_atlassian(client, name) or [], "atlassian"
    if ats and ats not in FETCHERS:
        raise ValueError(f"{name}: unknown ats '{ats}' (use one of {', '.join(FETCHERS)})")
    if not slugs:
        slugs = [re.sub(r"[^a-z0-9]", "", name.lower())]

    order = [ats] if ats else ["greenhouse", "ashby", "lever", "workable"]
    for kind in order:
        for slug in slugs:
            try:
                postings = FETCHERS[kind](client, name, slug)
            except httpx.HTTPError as exc:
                log.debug("%s: %s/%s failed: %s", name, kind, slug, exc)
                if ats:
                    raise
                continue
            if postings is not None:
                return postings, f"{kind}:{slug}"
    raise LookupError(f"{name}: no job board found (tried {', '.join(order)} with slugs {slugs})")


# ---------------------------------------------------------------------------
# Eligibility (can someone based in India take this role?)
# ---------------------------------------------------------------------------

_INDIA = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|new delhi|"
    r"gurgaon|gurugram|noida|kolkata|ahmedabad|kochi|jaipur)\b|\bIN\b"
)
_OPEN_REGION = re.compile(r"\b(worldwide|global|globally|anywhere|international)\b")
_APAC = re.compile(r"\b(apac|asia|asia[- ]pacific|apj)\b")
_REMOTE = re.compile(r"\b(remote|home[- ]based|work from home|wfh|distributed|telecommute)\b")
_TITLE_REGION = re.compile(
    r"(?i)\b(americas?|amer|namer|north america|latam|u\.?s\.?a?|united states|canada|"
    r"uk|u\.k\.|uki|emea|europe|eu|dach|germany|france|spain|nordics|taipei|taiwan|"
    r"singapore|japan|japanese|mandarin|korea|korean|australia|anz|brazil|mexico|philippines|"
    r"china|chinese|ksa|au|nz|sg|anz|new zealand|latin america)\b"
)
_TITLE_OPEN = re.compile(r"(?i)\b(india|apac|asia|global|worldwide|uae|dubai|abu dhabi)\b")
# Second target region: UAE roles (onsite or remote) are eligible; candidate needs visa sponsorship.
_UAE = re.compile(r"(?i)\b(uae|u\.a\.e\.?|united arab emirates|dubai|abu dhabi|sharjah)\b")
_FILLER = re.compile(r"\b(remote|home|based|work|from|or|and|hq|office|hybrid|wfh|distributed|telecommute|location|flexible)\b")


def classify_location(p: Posting) -> str:
    """Return an eligibility label, or "" if the role isn't open to India."""
    locs = [loc for loc in p.locations if loc and loc.strip()]
    labels: set[str] = set()
    restricted_elsewhere = False  # e.g. "Seattle, US" or "Remote - Americas"
    for loc in locs or ["Remote" if p.remote else ""]:
        # Country codes like "IN" are case-sensitive; everything else isn't.
        if _INDIA.search(loc) or _INDIA.search(loc.lower()):
            labels.add("India")
            continue
        if _UAE.search(loc):
            labels.add("UAE")
            continue
        low = loc.lower()
        is_remote = bool(_REMOTE.search(low)) or (p.remote is True)
        leftover = _FILLER.sub(" ", re.sub(r"[^a-z ]", " ", low)).strip()
        if _OPEN_REGION.search(low) and is_remote:
            labels.add("Remote - worldwide")
        elif _APAC.search(low) and is_remote:
            labels.add("Remote - APAC")
        elif is_remote and not leftover:
            labels.add("Remote - region not stated")
        elif leftover:
            restricted_elsewhere = True

    # A role whose title pins it to another region isn't open to India,
    # even when the board also tags it "worldwide".
    if not labels.intersection({"India", "UAE"}) and _TITLE_REGION.search(p.title) \
            and not _TITLE_OPEN.search(p.title):
        return ""
    if labels == {"Remote - region not stated"} and restricted_elsewhere:
        return ""
    for label in ("India", "Remote - worldwide", "Remote - APAC", "UAE", "Remote - region not stated"):
        if label in labels:
            return label
    return ""


def title_matches(title: str, keywords: list[str], exclude: list[str] | None = None) -> bool:
    low = title.lower()
    for word in exclude or []:
        if re.search(rf"\b{re.escape(word.lower())}\b", low):
            return False
    # Managers are senior unless it's a product/program/project-manager track.
    if re.search(r"\bmanager\b", low) and not re.search(r"\b(product|program|project) manager\b", low):
        return False
    if not keywords:
        return True
    return any(re.search(rf"\b{re.escape(k.lower())}\b", low) for k in keywords)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def collect(entries: list[dict] | None = None, keywords: list[str] | None = None,
            all_levels: bool = False) -> tuple[list[Posting], list[dict]]:
    """Fetch and filter postings. Returns (kept postings, per-company report)."""
    cfg = load_companies_config()
    entries = cfg["companies"] if entries is None else entries
    keywords = cfg["title_keywords"] if keywords is None else keywords
    exclude = cfg["exclude_title_words"]

    kept: list[Posting] = []
    report: list[dict] = []
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True) as client:
        for entry in entries:
            row = {"company": entry["name"], "source": "", "total": 0, "eligible": 0, "kept": 0, "error": ""}
            try:
                postings, row["source"] = fetch_company(client, entry)
            except Exception as exc:  # one bad board must not stop the rest
                row["error"] = str(exc)[:200]
                log.warning("Company board failed: %s", row["error"])
                report.append(row)
                continue
            row["total"] = len(postings)
            for p in postings:
                if not p.url or not p.title:
                    continue
                p.eligibility = classify_location(p)
                if not p.eligibility:
                    continue
                row["eligible"] += 1
                if not title_matches(p.title, keywords, exclude):
                    continue
                if not all_levels and not title_ok(p.title):
                    continue
                kept.append(p)
                row["kept"] += 1
            report.append(row)
            log.info("%s (%s): %d openings, %d open to India, %d kept",
                     row["company"], row["source"], row["total"], row["eligible"], row["kept"])
    return kept, report


def store_postings(conn: sqlite3.Connection, postings: list[Posting]) -> tuple[int, int]:
    """Insert postings (full description included, so enrichment is skipped)."""
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0
    for p in postings:
        location = f"{p.location} [{p.eligibility}]" if p.eligibility else p.location
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, description, location, site, company, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at, region) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p.url, p.title, p.description[:500], location, p.company, p.company,
                 "company_board", now, p.description or None, p.apply_url, now,
                 "UAE" if p.eligibility == "UAE" else None),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()
    return new, existing


def run_company_discovery() -> dict:
    """Pipeline stage hook: fetch target companies and store eligible roles."""
    init_db()
    postings, report = collect()
    new, existing = store_postings(get_connection(), postings)
    log.info("Company boards: %d kept, %d new, %d already known", len(postings), new, existing)
    return {"kept": len(postings), "new": new, "existing": existing, "report": report}

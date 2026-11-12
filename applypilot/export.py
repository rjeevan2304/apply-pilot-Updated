"""Export a shortlist of companies with their tailored resumes.

For manual applying: writes a CSV + Markdown list of every job that has a
tailored resume (company, title, location, score, apply link, file paths) and
copies each job's resume / cover letter / job description into its own folder.

The output folder is stable (default ``~/.applypilot/shortlist``) and updated
in place on every run, so a scheduled job on a server keeps one up-to-date
``shortlist.csv``. Optionally the folder is mirrored to a Google Cloud Storage
bucket (``--gcs gs://bucket/prefix`` or ``APPLYPILOT_GCS_URI``).
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from applypilot.config import APP_DIR
from applypilot.database import get_connection, job_company

log = logging.getLogger(__name__)

CSV_FIELDS = (
    "company", "title", "region", "location", "fit_score", "salary", "apply_url",
    "job_url", "discovered_at", "tailored_at", "resume_pdf", "resume_txt",
    "cover_letter", "folder",
)


def default_export_dir() -> Path:
    env = os.environ.get("APPLYPILOT_EXPORT_DIR")
    return Path(env).expanduser() if env else APP_DIR / "shortlist"


def _existing(path: Path | None) -> Path | None:
    return path if path is not None and path.exists() else None


def _pdf_or_txt(txt_path: str | None) -> tuple[Path | None, Path | None]:
    """Return (pdf, txt) for a stored .txt path, each only if it exists."""
    if not txt_path:
        return None, None
    txt = Path(txt_path)
    return _existing(txt.with_suffix(".pdf")), _existing(txt)


def _safe(name: str, max_len: int = 60) -> str:
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    return re.sub(r"\s+", " ", name)[:max_len].strip() or "Unknown"


def _folder_name(company: str, title: str, url: str) -> str:
    """Stable per-job folder name (same job -> same folder on every run)."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{_safe(f'{company} - {title}')} [{digest}]"


def _copy_if_changed(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size == src.stat().st_size \
            and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    shutil.copy2(src, dst)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def export_shortlist(
    min_score: int = 7,
    output_dir: Path | None = None,
    copy_files: bool = True,
    write_status: bool = True,
) -> dict:
    """Write the shortlist and return {"count", "dir", "csv", "markdown", "records"}."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND COALESCE(fit_score, 0) >= ? "
        "ORDER BY fit_score DESC, tailored_at DESC, COALESCE(company, site), title",
        (min_score,),
    ).fetchall()
    jobs = [dict(r) for r in rows]
    # Only jobs you can take: India (or remote open to India) for the India search,
    # remote open to the UAE for the UAE search. Other countries and hybrid/onsite
    # roles abroad are left out, including ones saved before this rule existed.
    from applypilot.discovery.filters import india_open, uae_open
    jobs = [j for j in jobs
            if (uae_open(j.get("location")) if (j.get("region") == "UAE") else india_open(j.get("location")))]

    out = Path(output_dir) if output_dir else default_export_dir()
    out.mkdir(parents=True, exist_ok=True)
    jobs_dir = out / "jobs"

    records: list[dict] = []
    for job in jobs:
        company = job_company(job)
        title = job.get("title") or "Untitled"
        resume_pdf, resume_txt = _pdf_or_txt(job.get("tailored_resume_path"))
        cl_pdf, cl_txt = _pdf_or_txt(job.get("cover_letter_path"))
        cover = cl_pdf or cl_txt
        apply_url = job.get("application_url") or job["url"]

        folder = None
        if copy_files:
            folder = jobs_dir / _folder_name(company, title, job["url"])
            folder.mkdir(parents=True, exist_ok=True)
            resume_file = resume_pdf or resume_txt
            if resume_file:
                _copy_if_changed(resume_file, folder / f"Resume{resume_file.suffix}")
            if cover:
                _copy_if_changed(cover, folder / f"Cover_Letter{cover.suffix}")
            job_txt = (
                f"Company:   {company}\n"
                f"Title:     {title}\n"
                f"Region:    {job.get('region') or 'India'}\n"
                f"Location:  {job.get('location') or ''}\n"
                f"Score:     {job.get('fit_score')}/10\n"
                f"Apply at:  {apply_url}\n"
                f"Listing:   {job['url']}\n\n"
                f"Why it fits:\n{job.get('score_reasoning') or ''}\n\n"
                f"{'-' * 60}\n{job.get('full_description') or ''}\n"
            )
            job_file = folder / "job.txt"
            if not job_file.exists() or job_file.read_text(encoding="utf-8") != job_txt:
                job_file.write_text(job_txt, encoding="utf-8")

        records.append({
            "company": company,
            "title": title,
            "region": job.get("region") or "India",
            "location": job.get("location") or "",
            "fit_score": job.get("fit_score"),
            "salary": job.get("salary") or "",
            "apply_url": apply_url,
            "job_url": job["url"],
            "discovered_at": (job.get("discovered_at") or "")[:19],
            "tailored_at": (job.get("tailored_at") or "")[:19],
            "resume_pdf": str(resume_pdf or ""),
            "resume_txt": str(resume_txt or ""),
            "cover_letter": str(cover or ""),
            "folder": str(folder.relative_to(out)) if folder else "",
        })

    # CSV (written atomically so a reader never sees a half-written file)
    csv_path = out / "shortlist.csv"
    tmp_csv = csv_path.with_name("shortlist.csv.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    os.replace(tmp_csv, csv_path)

    # Markdown
    def cell(text) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# Job shortlist ({len(records)} jobs, score >= {min_score})",
        "",
        f"Updated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| # | Company | Role | Region | Location | Score | Apply | Folder |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(records, 1):
        lines.append(
            f"| {i} | {cell(r['company'])} | {cell(r['title'])} | {cell(r['region'])} | {cell(r['location'])} "
            f"| {r['fit_score']} | [link]({r['apply_url']}) | {cell(r['folder'] or r['resume_pdf'] or r['resume_txt'])} |"
        )
    md_path = out / "shortlist.md"
    _atomic_write(md_path, "\n".join(lines) + "\n")

    # Pipeline snapshot for the dashboard (`applypilot ui`). Mid-run publishes
    # skip this: the cycle's heartbeat keeps status.json at "running".
    if write_status:
        try:
            from applypilot.statusfile import write_status as _write_status
            _write_status(out, running=False)
        except Exception:
            log.warning("Could not write status.json", exc_info=True)

    return {"count": len(records), "dir": out, "csv": csv_path, "markdown": md_path,
            "records": records}


# ---------------------------------------------------------------------------
# Google Cloud Storage sync
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"GCS location must look like gs://bucket[/prefix], got: {uri}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"Missing bucket name in {uri}")
    return bucket, prefix.strip("/")


def upload_file_to_gcs(local_path: Path, gcs_uri: str, rel: str) -> None:
    """Upload one file to gs://bucket/prefix/<rel> (used for the status heartbeat)."""
    from google.cloud import storage

    bucket_name, prefix = _parse_gcs_uri(gcs_uri)
    blob = storage.Client().bucket(bucket_name).blob(f"{prefix}/{rel}" if prefix else rel)
    blob.cache_control = "no-cache"
    blob.upload_from_filename(str(local_path))


def sync_to_gcs(local_dir: Path, gcs_uri: str) -> int:
    """Mirror ``local_dir`` to ``gs://bucket/prefix``. Returns files uploaded.

    The CSV/Markdown are always uploaded; other files only when new or changed
    (by MD5). Uses Application Default Credentials, i.e. the VM's service
    account on Compute Engine, so no key file is needed there.
    """
    try:
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "google-cloud-storage is not installed. Run: pip install -e '.[gcp]'"
        ) from exc

    from google.api_core import exceptions as gexc

    bucket_name, prefix = _parse_gcs_uri(gcs_uri)
    try:
        return _sync(storage.Client(), local_dir, gcs_uri, bucket_name, prefix)
    except gexc.NotFound as exc:
        raise RuntimeError(
            f"Bucket '{bucket_name}' not found. Check the name in APPLYPILOT_GCS_URI "
            f"(Cloud Console > Cloud Storage > Buckets)."
        ) from exc
    except (gexc.Forbidden, gexc.Unauthorized) as exc:
        detail = str(exc).lower()
        if "scope" in detail:
            hint = ("the VM's access scopes don't allow writing to Cloud Storage. Stop the VM, "
                    "Edit > Access scopes > 'Set access for each API' > Storage: Read Write, then start it.")
        else:
            hint = ("the VM's service account can't write to this bucket. In the bucket's Permissions tab, "
                    "grant the VM's service account the 'Storage Object Admin' role.")
        raise RuntimeError(f"Cloud Storage refused access to gs://{bucket_name}: {hint}") from exc


def _sync(client, local_dir: Path, gcs_uri: str, bucket_name: str, prefix: str) -> int:
    import base64

    bucket = client.bucket(bucket_name)

    remote_md5 = {
        b.name: b.md5_hash
        for b in client.list_blobs(bucket_name, prefix=f"{prefix}/" if prefix else None)
    }

    uploaded = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(local_dir).as_posix()
        name = f"{prefix}/{rel}" if prefix else rel
        always = rel in ("shortlist.csv", "shortlist.md", "status.json")
        if not always and name in remote_md5:
            local_md5 = base64.b64encode(hashlib.md5(path.read_bytes()).digest()).decode()
            if local_md5 == remote_md5[name]:
                continue
        blob = bucket.blob(name)
        if rel == "shortlist.csv":
            blob.cache_control = "no-cache"
        blob.upload_from_filename(str(path))
        uploaded += 1

    log.info("Synced %d file(s) to %s", uploaded, gcs_uri)
    return uploaded

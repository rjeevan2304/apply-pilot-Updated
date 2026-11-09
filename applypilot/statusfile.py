"""status.json: a snapshot of the pipeline for the local dashboard (`applypilot ui`).

Written next to shortlist.csv (and uploaded to the bucket with it):
- every few minutes while `applypilot cycle` runs (running=true, current stage), and
- at the end of every export (running=false).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

STATUS_FILE = "status.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_status(running: bool, run_started_at: str | None = None,
                 previous: dict | None = None) -> dict:
    """Collect pipeline counts from the database plus run state."""
    from applypilot.database import get_connection, get_stats

    conn = get_connection()
    stats = get_stats(conn)
    by_region = conn.execute(
        "SELECT COALESCE(region, 'India') AS r, COUNT(*), "
        "SUM(fit_score IS NOT NULL), SUM(fit_score >= 7), SUM(tailored_resume_path IS NOT NULL) "
        "FROM jobs GROUP BY r ORDER BY r"
    ).fetchall()

    progress: dict = {}
    if running:
        try:
            from applypilot.pipeline import PROGRESS
            progress = dict(PROGRESS)
        except Exception:
            progress = {}

    previous = previous or {}
    return {
        "generated_at": _now(),
        "running": running,
        "stage": progress.get("stage") if running else None,
        "detail": progress.get("detail") if running else None,
        "stage_started_at": progress.get("stage_started_at") if running else None,
        "run_started_at": run_started_at if running else previous.get("run_started_at"),
        "last_run_finished_at": previous.get("last_run_finished_at") if running else _now(),
        "stats": {
            k: (list(map(list, v)) if isinstance(v, (list, tuple)) else v)
            for k, v in stats.items()
        },
        "by_region": [
            {"region": r[0], "total": r[1], "scored": r[2] or 0, "strong": r[3] or 0, "tailored": r[4] or 0}
            for r in by_region
        ],
    }


def read_status(directory: Path) -> dict:
    try:
        return json.loads((Path(directory) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(directory: Path, running: bool, run_started_at: str | None = None) -> Path:
    """Write status.json atomically into `directory` and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = build_status(running, run_started_at, previous=read_status(directory))
    path = directory / STATUS_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path

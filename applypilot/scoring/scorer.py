"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import SavingList, get_connection, get_jobs_by_stage, job_company
from applypilot.llm import LLMQuotaExhausted, get_client_for

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)
- UAE JOBS (location in Dubai, Abu Dhabi or elsewhere in the UAE, onsite or remote, are acceptable): the candidate needs employer visa sponsorship, like almost every applicant. Do not penalize for visa, residence or NOC requirements at all. Judge role field and seniority exactly as for India.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job_company(job)}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client_for("LLM_SCORE_MODEL")
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except LLMQuotaExhausted:
        raise
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


_YEARS_PATTERNS = (
    re.compile(r"(?<![\d.])(\d{1,2})\s*\+?\s*(?:to|-|\u2013|\u2014)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?<![\d.])(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?:minimum|min\.?|at\s*least)\s*(?:of\s*)?(?<![\d.])(\d{1,2})\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?<![\d.])(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s*)?(?:relevant\s*|professional\s*|hands-on\s*|total\s*)?(?:experience|exp\b)", re.I),
)


def min_years_required(title: str | None, url: str | None, description: str | None) -> int | None:
    """Smallest years-of-experience figure stated in the posting (None if none found).

    Using the smallest figure keeps this conservative: a posting that also says "2+ years" is not flagged.
    Naukri URLs carry the range too ("...-9-to-14-years-...").
    """
    text = f"{title or ''} {(url or '').replace('-', ' ')} {description or ''}"
    found = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if 0 < n <= 30:
                found.append(n)
    return min(found) if found else None


def _apply_years_rule(result: dict, job: dict) -> dict:
    """Hard seniority check: postings whose smallest stated experience is 5+ years score at most 3."""
    yrs = min_years_required(job.get("title"), job.get("url"), job.get("full_description"))
    if yrs is not None and yrs >= 5 and result["score"] > 3:
        result["reasoning"] = f"(Capped from {result['score']}: posting asks for {yrs}+ years.) " + result["reasoning"]
        result["score"] = 3
    return result


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    # Titles the search filters would have dropped (senior/lead/director/intern/...)
    # are marked skipped without an LLM call. They never reach the 7+ shortlist.
    # Catches jobs found before these rules existed (e.g. from Workday).
    from applypilot.discovery.filters import title_ok
    skipped = [j for j in jobs if not title_ok(j.get("title"))]
    if skipped:
        conn.executemany(
            "UPDATE jobs SET fit_score = 1, score_reasoning = ? WHERE url = ?",
            [("\n(Skipped without an LLM call: senior/lead or excluded title.)", j["url"]) for j in skipped],
        )
        conn.commit()
        log.info("Skipped %d job(s) by title without an LLM call", len(skipped))
        jobs = [j for j in jobs if title_ok(j.get("title"))]
    if not jobs:
        return {"scored": 0, "skipped": len(skipped), "errors": 0, "elapsed": 0.0, "distribution": []}

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    quota_exhausted = False
    def _save_score(r: dict) -> None:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}",
             datetime.now(timezone.utc).isoformat(), r["url"]),
        )
        conn.commit()

    results: list[dict] = SavingList(_save_score)  # each score is saved as soon as it's made

    for job in jobs:
        try:
            result = score_job(resume_text, job)
        except LLMQuotaExhausted as exc:
            completed += 1
            errors += 1
            quota_exhausted = True
            log.error(
                "LLM quota/credit exhausted (no fallback left) at job %d/%d. "
                "Stopping this batch; remaining jobs stay pending. Details: %s",
                completed, len(jobs), exc,
            )
            break

        completed += 1
        if result["score"] != 0:
            result = _apply_years_rule(result, job)

        if result["score"] == 0:
            errors += 1
            log.error(
                "[%d/%d] Scoring failed; leaving job pending: %s",
                completed, len(jobs), job.get("title", "?")[:60],
            )
            continue

        result["url"] = job["url"]
        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # (Scores were saved one by one as they were made.)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "skipped": len(skipped),
        "scored": len(results),
        "errors": errors,
        "quota_exhausted": quota_exhausted,
        "elapsed": elapsed,
        "distribution": distribution,
    }

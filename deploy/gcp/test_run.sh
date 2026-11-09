#!/usr/bin/env bash
# Small end-to-end test of the pipeline, in a SEPARATE folder (~/.applypilot-test).
# Your real database, resumes and shortlist in ~/.applypilot are not touched.
#
#   bash deploy/gcp/test_run.sh                     # test, results stay on the VM
#   bash deploy/gcp/test_run.sh gs://BUCKET/test-run # also upload the test shortlist
#
# Uses your real profile.json, resume.txt and .env (API keys), but only:
#   2 search queries x 1 location x 5 results per job board, 2 target companies,
#   at most 6 jobs scored and 2 resumes tailored. Takes about 5-15 minutes.
# Optional: TEST_MIN_SCORE=5 bash deploy/gcp/test_run.sh  -> tailor jobs scoring 5+
# in THIS TEST ONLY (handy if the small sample has no 7+ job). Real runs keep 7.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REAL="$HOME/.applypilot"
TEST="$HOME/.applypilot-test"
GCS_URI="${1:-}"

for f in profile.json resume.txt .env searches.yaml; do
  if [ ! -f "$REAL/$f" ]; then
    echo "Missing $REAL/$f - finish the setup first (see README)." >&2
    exit 1
  fi
done

mkdir -p "$TEST"
# Share your real profile, resume and keys with the test folder (links, not copies).
for f in profile.json resume.txt resume.pdf .env companies.yaml; do
  [ -e "$REAL/$f" ] && ln -sf "$REAL/$f" "$TEST/$f"
done

export APPLYPILOT_DIR="$TEST"
cd "$REPO"
exec "$REPO/.venv/bin/python" - "$GCS_URI" "$REAL/searches.yaml" <<'PY'
import logging, os, sys, time
from pathlib import Path

import yaml

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
gcs_uri, searches_path = sys.argv[1], sys.argv[2]
min_score = int(os.environ.get("TEST_MIN_SCORE", "7"))
results = []


def step(title):
    print(f"\n=== {title} ===", flush=True)


def done(name, ok, note=""):
    results.append((name, ok, note))
    print(("  OK   " if ok else "  FAIL ") + name + (f" - {note}" if note else ""), flush=True)


from applypilot.config import APP_DIR, ensure_dirs, load_env
load_env()
ensure_dirs()
from applypilot.database import get_connection, init_db, job_company
init_db()
conn = get_connection()
print(f"Test folder: {APP_DIR}")

# 1. LLM keys -------------------------------------------------------------------
step("1/7  LLM keys")
from applypilot import llm
client = llm.get_client()
primary = getattr(client, "primary", client)
try:
    reply = primary.ask("Reply with just: OK", max_tokens=400)
    done("Main LLM (" + str(getattr(primary, "model", "?")) + ")", True, f"replied {reply.strip()[:20]!r}")
except Exception as e:
    done("Main LLM", False, str(e)[:300])
if isinstance(client, llm.FallbackClient):
    try:
        reply = client.fallback.ask("Reply with just: OK", max_tokens=400)
        done(f"Fallback LLM ({client.fallback.model})", True, f"replied {reply.strip()[:20]!r}")
    except Exception as e:
        done("Fallback LLM", False, str(e)[:300])
if not any(ok for name, ok, _ in results if "LLM" in name):
    print("\nNo LLM works, so scoring/tailoring can't run. Fix the key(s) in ~/.applypilot/.env and retry.")
    sys.exit(1)

# 2. Job boards (small) ------------------------------------------------------------
step("2/7  Job boards: 2 searches x 1 location x 5 results")
cfg = yaml.safe_load(Path(searches_path).read_text(encoding="utf-8")) or {}
cfg["queries"] = (cfg.get("queries") or [])[:2]
cfg["locations"] = (cfg.get("locations") or [])[:1]
cfg.setdefault("defaults", {})["results_per_site"] = 5
cfg.update(batch_size=100, search_pause_seconds=2, batch_pause_seconds=0,
           max_total_cooldown_seconds=0)
print("  searches:", [q["query"] for q in cfg["queries"]], "in", [l["location"] for l in cfg["locations"]],
      "| boards:", cfg.get("sites"))
try:
    from applypilot.discovery.jobspy import run_discovery
    r = run_discovery(cfg)
    done("Job boards", r.get("new", 0) > 0 or not r.get("errors"),
         f"{r.get('new', 0)} new jobs, {r.get('errors', 0)} errors"
         + ("" if r.get("new") else " (0 can just mean every result was filtered out by title/location)"))
except Exception as e:
    done("Job boards", False, str(e)[:300])

# 3. Target companies (2) ------------------------------------------------------------
step("3/7  Target company job boards (2 companies)")
try:
    from applypilot.discovery.companies import collect, load_companies_config, store_postings
    entries = [c for c in load_companies_config()["companies"] if c["name"] in ("Supabase", "SigNoz")][:2]
    postings, report = collect(entries=entries)
    new, _ = store_postings(conn, postings[:4])
    done("Company boards", bool(postings), f"{len(postings)} matching roles at "
         + ", ".join(r["company"] for r in report) + f"; saved {new} for the test")
except Exception as e:
    done("Company boards", False, str(e)[:300])

rows = conn.execute("SELECT site, COUNT(*), SUM(full_description IS NOT NULL) FROM jobs GROUP BY site").fetchall()
print("  in test database:", ", ".join(f"{r[0]}: {r[1]} ({r[2]} with description)" for r in rows) or "nothing")

# 4. Scoring ----------------------------------------------------------------------------
step("4/7  Scoring up to 6 jobs")
try:
    from applypilot.scoring.scorer import run_scoring
    r = run_scoring(limit=6)
    done("Scoring", r.get("scored", 0) > 0, f"{r.get('scored', 0)} scored, {r.get('errors', 0)} errors")
    for row in conn.execute("SELECT fit_score, title, company, site, url FROM jobs "
                            "WHERE fit_score IS NOT NULL ORDER BY fit_score DESC"):
        print(f"    {row[0]:>2}/10  {row[1][:55]}  @ {row[2] or row[3]}")
except Exception as e:
    done("Scoring", False, str(e)[:300])

# 5. Tailoring --------------------------------------------------------------------------
step(f"5/7  Tailoring up to 2 resumes (score >= {min_score})")
eligible = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= ?", (min_score,)).fetchone()[0]
if not eligible:
    done("Tailoring", True, f"skipped: no job in this small sample scored {min_score}+. "
         "That's fine; to test tailoring anyway run: TEST_MIN_SCORE=5 bash deploy/gcp/test_run.sh")
else:
    try:
        from applypilot.scoring.tailor import run_tailoring
        r = run_tailoring(min_score=min_score, limit=2)
        made = conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0]
        done("Tailoring", made > 0, f"{made} tailored resume(s); result {r}")
    except Exception as e:
        done("Tailoring", False, str(e)[:300])

# 6. PDFs -------------------------------------------------------------------------------
step("6/7  PDF resumes")
try:
    from applypilot.scoring.pdf import batch_convert
    n = batch_convert()
    pdfs = list((APP_DIR / "tailored_resumes").glob("*.pdf"))
    done("PDFs", bool(pdfs) or not eligible, f"{len(pdfs)} PDF(s)")
except Exception as e:
    done("PDFs", False, str(e)[:300])

# 7. Shortlist + bucket -----------------------------------------------------------------
step("7/7  Shortlist CSV" + (f" + upload to {gcs_uri}" if gcs_uri else ""))
try:
    from applypilot.export import export_shortlist, sync_to_gcs
    res = export_shortlist(min_score=min_score)
    done("Shortlist", True, f"{res['count']} job(s) in {res['csv']}")
    for r in res["records"]:
        print(f"    {r['fit_score']}/10  {r['company']} - {r['title']}\n           apply: {r['apply_url']}\n           files: {APP_DIR / 'shortlist' / r['folder']}")
    if gcs_uri:
        n = sync_to_gcs(res["dir"], gcs_uri)
        done("Bucket upload", True, f"{n} file(s) to {gcs_uri}")
except Exception as e:
    done("Shortlist/upload", False, str(e)[:300])

print("\n=== Summary ===")
for name, ok, note in results:
    print(("  OK   " if ok else "  FAIL ") + name)
failed = [r for r in results if not r[1]]
print("\nAll good - you can start the real run." if not failed else
      f"\n{len(failed)} step(s) failed - paste this output (without keys) to get help.")
print(f"Test files: {APP_DIR}  (delete with: rm -rf {APP_DIR})")
PY

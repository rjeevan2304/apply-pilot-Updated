# apply-pilot-Updated

A customized version of [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) v0.3.0, an AI-powered end-to-end job application pipeline (discover → enrich → score → tailor → apply).

## Changes from upstream

Modified modules:

- `applypilot/llm.py`
- `applypilot/apply/launcher.py`
- `applypilot/apply/prompt.py`
- `applypilot/discovery/jobspy.py`
- `applypilot/scoring/pdf.py`
- `applypilot/scoring/scorer.py`
- `applypilot/scoring/tailor.py`

## Setup

```bash
git clone https://github.com/rjeevan2304/apply-pilot-Updated.git
cd apply-pilot-Updated
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install 'python-jobspy>=1.4.1'   # job discovery backend (Indeed, LinkedIn, Naukri)
playwright install chromium
```

## Configuration

ApplyPilot reads its config from `~/.applypilot/`:

```bash
mkdir -p ~/.applypilot
cp config/profile.example.json  ~/.applypilot/profile.json
cp config/searches.example.yaml ~/.applypilot/searches.yaml
cp config/.env.example          ~/.applypilot/.env
```

Then fill in `profile.json` (personal details, skills, resume facts), edit `searches.yaml` (queries, locations, sites), add your API key to `.env`, and place your resume at `~/.applypilot/resume.pdf` / `resume.txt`.

Personal data (profile, resume, database, logs, tailored resumes) is never committed — see `.gitignore`.

## Usage: shortlist + tailored resumes (apply manually)

This fork is set up to **find jobs and tailor resumes only**; you apply yourself.

```bash
applypilot cycle                 # discover -> enrich -> score -> tailor -> pdf -> export
applypilot export                # just refresh the shortlist from what's already done
applypilot export --min-score 6  # include weaker matches
```

Output (updated in place on every run):

```
~/.applypilot/shortlist/
  shortlist.csv     company, title, location, score, apply_url, ... (open in Sheets/Excel)
  shortlist.md      same list as a table
  jobs/<Company - Title [id]>/
    Resume.pdf      tailored resume for this job
    Cover_Letter.*  only with `cycle --cover`
    job.txt         apply link, why it fits, full job description
```

### Target companies (remote roles open to India)

Besides job-board searches, discovery pulls openings straight from a list of companies'
own job boards (Greenhouse, Lever, Ashby, Workable, Atlassian), configured in
[`applypilot/config/companies.yaml`](applypilot/config/companies.yaml). Copy it to
`~/.applypilot/companies.yaml` to edit the list.

Only roles someone in India can take are kept: India locations, or remote roles marked
worldwide / global / APAC, or plain "Remote" with no region. Roles limited to other regions
(US, EMEA, "Americas only", ...), senior titles, managers (except Product Manager) and
non-engineering/product functions are dropped. These roles come with full descriptions, so
they go straight to scoring and tailoring.

```bash
applypilot companies                 # show matching roles per company (nothing saved)
applypilot companies --csv roles.csv # also write them to a CSV
applypilot companies --save          # add them to the database now (cycle does this too)
```

### UAE as a second region

Copy `config/searches_uae.example.yaml` to `~/.applypilot/searches_uae.yaml` to also search
Dubai / Abu Dhabi (delete the file to stop). Those jobs get `region = UAE` in the database and
the shortlist CSV, and scoring doesn't penalise the visa sponsorship they need. UAE roles on
the target-company boards are kept too.

### Scoring and filtering rules

- Titles: Business Analyst and Program/Project Manager are allowed; interns, trainees, sales,
  recruiting, HR, marketing, data entry and senior titles (Senior/Snr/Lead/Staff/Principal/…, II+)
  are dropped before scoring.
- Years rule: if the smallest experience figure in a posting (text or Naukri URL, e.g.
  `...-9-to-14-years-...`) is 5+ years, the score is capped at 3.

### Rate limits (job boards)

Searches run in batches with pauses, cool off after a 429/captcha and retry once. Tune in
`searches.yaml` / `searches_uae.yaml`:

```yaml
batch_size: 5                     # searches per batch
search_pause_seconds: 10          # pause between searches
batch_pause_seconds: 120          # pause between batches
rate_limit_cooldown_seconds: 300  # cool-off after a 429 / captcha
max_total_cooldown_seconds: 3600  # cap on total cool-off per run
time_budget_minutes: 240          # optional: stop searching after this long
```

LinkedIn detail pages are fetched at most 15 per pass with random delays; the pass stops at the
first block or login wall, and a failed page is retried at most twice on later runs.

### LLM: Gemini free tier first, Claude API as fallback

In `~/.applypilot/.env`:

```
GEMINI_API_KEY=...                 # main LLM (free tier), model gemini-3.5-flash
ANTHROPIC_API_KEY=...              # fallback LLM (Claude API, paid per use)
# ANTHROPIC_MODEL=claude-haiku-5-5 # default; claude-sonnet-5-5 / claude-opus-5-5 are stronger, cost more
# ANTHROPIC_EFFORT=medium          # low | medium | high | xhigh | max
```

With both keys set, every request goes to Gemini first. Claude is used:

- for the rest of the run once Gemini's daily/monthly quota is used up;
- for a single request when Gemini is still rate limited after one retry, times out, or
  returns 404 (usually a retired `LLM_MODEL`).

If Claude is also out of credit, scoring stops and the remaining jobs stay pending for the
next run.

**Using a Claude Pro/Max subscription instead of API credit:** install Claude Code on the VM
(`curl -fsSL https://claude.ai/install.sh | bash`), run `claude` once and log in with your
Claude account, then set `LLM_FALLBACK_PROVIDER=claude_cli` in `.env` (optionally
`CLAUDE_MODEL=haiku|sonnet|opus`, default `haiku`). Requests then count against your
subscription's usage limits; when a limit is hit, scoring stops until the next run. Set `LLM_FALLBACK_PROVIDER=none` to turn the fallback off, or leave out
`GEMINI_API_KEY` to use Claude only. `applypilot doctor` shows which is active.

### LLM: kie.ai with a fallback model

In `~/.applypilot/.env`:

```
LLM_PROVIDER=kie
KIE_API_KEY=...
LLM_MODEL=claude-haiku-5-5        # main model
LLM_FALLBACK_MODEL=deepseek-v4    # used when the main model hits a limit
# optional per-stage models: LLM_SCORE_MODEL=..., LLM_ENRICH_MODEL=...
```

The fallback is used immediately when kie.ai reports a token, context-length, credit or quota
limit (or the answer is cut off at the output-token limit), and after retries are exhausted on
rate limits or server errors. At most 2 requests are in flight at once (`KIE_MAX_CONCURRENCY`).

`applypilot cycle` never submits applications. The `apply` command (Claude Code + Chrome
auto-apply) is still there but isn't needed.

## Running on a Google Cloud VM

Everything runs headless; no display or local machine needed.

1. Create a Compute Engine VM (Debian 12 or Ubuntu 24.04; e2-small is enough). If you want
   the CSV in Cloud Storage too, give the VM's service account **Storage Object Admin** on
   your bucket and set the VM's access scope for Storage to **Read Write**.
2. On the VM:

   ```bash
   git clone https://github.com/rjeevan2304/apply-pilot-Updated.git
   cd apply-pilot-Updated
   bash deploy/gcp/setup.sh gs://YOUR_BUCKET/applypilot   # bucket is optional
   ```

   This installs dependencies and headless Chromium, creates `~/.applypilot/`, and enables a
   systemd timer that runs `applypilot cycle` every 6 hours (missed runs catch up after a reboot).
3. Fill in `~/.applypilot/profile.json`, `resume.txt`, `searches.yaml` and `.env`
   (`GEMINI_API_KEY=...`). Optionally do a quick test first: `bash deploy/gcp/test_run.sh`
   (2 searches, 2 companies, up to 6 jobs scored and 2 resumes tailored, in a separate
   `~/.applypilot-test` folder; add `gs://BUCKET/test-run` to test the upload too).
   Then start a first run:

   ```bash
   sudo systemctl start applypilot-cycle.service
   journalctl -fu applypilot-cycle
   ```

Where the results are:

- On the VM: `~/.applypilot/shortlist/shortlist.csv`. Fetch it with
  `gcloud compute scp VM_NAME:~/.applypilot/shortlist/shortlist.csv .`
- In Cloud Storage (if configured): `gs://YOUR_BUCKET/applypilot/shortlist.csv` plus each job's
  folder. Browse it in the Cloud Console, or `gcloud storage cp -r gs://YOUR_BUCKET/applypilot .`

Settings: change the bucket or add flags (`--min-score 6`, `--cover`, `--workers 2`) in
`/etc/default/applypilot`; change the schedule in
`/etc/systemd/system/applypilot-cycle.timer` and then run `sudo systemctl daemon-reload`.
`shortlist.csv` is regenerated each run, so keep your own "applied" notes in a copy
(e.g. a Google Sheet that imports it), not in the file itself.

Without systemd (e.g. inside `tmux`): `applypilot cycle --loop-hours 6 --gcs gs://YOUR_BUCKET/applypilot`.

## License

AGPL-3.0, same as upstream. See [LICENSE](LICENSE).

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
   (`GEMINI_API_KEY=...`), then start a first run:

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

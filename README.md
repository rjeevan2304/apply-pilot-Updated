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
pip install python-jobspy   # job discovery backend
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

## Usage

```bash
applypilot --help
```

## License

AGPL-3.0, same as upstream. See [LICENSE](LICENSE).

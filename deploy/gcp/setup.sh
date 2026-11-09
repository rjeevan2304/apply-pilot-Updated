#!/usr/bin/env bash
# One-time setup of ApplyPilot on a Google Cloud (Compute Engine) Debian/Ubuntu VM.
#
# Run from the repo root as your normal user (not root):
#     bash deploy/gcp/setup.sh [gs://BUCKET/PREFIX]
#
# Installs system packages, a virtualenv with all deps, headless Chromium for
# PDF rendering/scraping, and a systemd timer that runs `applypilot cycle`
# every 6 hours. Re-running is safe.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_USER="$(id -un)"
RUN_HOME="$HOME"
GCS_URI="${1:-}"

if [ "$RUN_USER" = "root" ]; then
  echo "Run this as your normal user (it uses sudo where needed)." >&2
  exit 1
fi

echo "==> System packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  if command -v python3.11 >/dev/null || sudo apt-get install -y python3.11 python3.11-venv; then
    PY=python3.11
  else
    echo "Python 3.11+ is required. Use a Debian 12 / Ubuntu 24.04 image." >&2
    exit 1
  fi
fi

echo "==> Virtualenv + Python deps"
cd "$REPO"
$PY -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[gcp,jobspy]'

echo "==> Headless Chromium for Playwright (PDFs + scraping)"
sudo "$REPO/.venv/bin/playwright" install-deps chromium
.venv/bin/playwright install chromium

echo "==> Config folder ~/.applypilot"
mkdir -p "$RUN_HOME/.applypilot"
[ -f "$RUN_HOME/.applypilot/profile.json" ]  || cp config/profile.example.json  "$RUN_HOME/.applypilot/profile.json"
[ -f "$RUN_HOME/.applypilot/searches.yaml" ] || cp config/searches.example.yaml "$RUN_HOME/.applypilot/searches.yaml"
[ -f "$RUN_HOME/.applypilot/.env" ]          || cp config/.env.example          "$RUN_HOME/.applypilot/.env"
chmod 600 "$RUN_HOME/.applypilot/.env"

echo "==> systemd timer"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$RUN_HOME|g" \
  deploy/gcp/applypilot-cycle.service | sudo tee /etc/systemd/system/applypilot-cycle.service >/dev/null
sudo cp deploy/gcp/applypilot-cycle.timer /etc/systemd/system/applypilot-cycle.timer

if [ ! -f /etc/default/applypilot ] || [ -n "$GCS_URI" ]; then
  # An empty assignment here would mask a value set in ~/.applypilot/.env, so comment it out.
  if [ -n "$GCS_URI" ]; then GCS_LINE="APPLYPILOT_GCS_URI=$GCS_URI"; else GCS_LINE="#APPLYPILOT_GCS_URI=gs://your-bucket/applypilot"; fi
  sudo tee /etc/default/applypilot >/dev/null <<EOF
# Read by applypilot-cycle.service at every run (no reload needed after editing).
# Cloud Storage destination for shortlist.csv + resumes (commented out = keep on the VM only):
$GCS_LINE
# Extra flags for 'applypilot cycle', e.g. --min-score 6 --cover --workers 2
APPLYPILOT_CYCLE_ARGS=
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable --now applypilot-cycle.timer

cat <<EOF

Done. Before the first run, fill in on this VM:
  ~/.applypilot/profile.json     your details / resume facts
  ~/.applypilot/resume.txt       your base resume (plain text)
  ~/.applypilot/searches.yaml    queries + locations
  ~/.applypilot/.env             GEMINI_API_KEY=... (or another LLM provider)

Run once now and watch it:   sudo systemctl start applypilot-cycle.service && journalctl -fu applypilot-cycle
Next scheduled runs:         systemctl list-timers applypilot-cycle.timer
Shortlist on the VM:         ~/.applypilot/shortlist/shortlist.csv
EOF

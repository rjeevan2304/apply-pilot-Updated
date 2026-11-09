#!/usr/bin/env bash
# Deploy the ApplyPilot dashboard to Google Cloud Run, password protected.
# Only the dashboard (applypilot/ui) is uploaded, not the rest of the repository.
#
# Run in Google Cloud Shell (the >_ button in the Cloud Console), from the repo root:
#     bash deploy/cloudrun/deploy.sh gs://YOUR_BUCKET/applypilot
# Re-run the same command to update it (press Enter at the password prompt to keep it).
set -euo pipefail

GCS_URI="${1:-${APPLYPILOT_GCS_URI:-}}"
case "$GCS_URI" in
  gs://?*) ;;
  *) echo "Usage: bash deploy/cloudrun/deploy.sh gs://YOUR_BUCKET/applypilot" >&2; exit 1 ;;
esac
BUCKET="${GCS_URI#gs://}"; BUCKET="${BUCKET%%/*}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-applypilot-ui}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "No Google Cloud project selected. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi
echo "Project: $PROJECT   Region: $REGION   Data: $GCS_URI"

EXISTS=0
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 && EXISTS=1

PW=""
while :; do
  if [ "$EXISTS" = 1 ]; then
    read -r -s -p "Dashboard password (press Enter to keep the current one): " PW; echo
    break
  fi
  read -r -s -p "Choose a dashboard password (8+ characters): " PW; echo
  [ "${#PW}" -ge 8 ] || { echo "Too short, try again."; continue; }
  read -r -s -p "Type it again: " PW2; echo
  [ "$PW" = "$PW2" ] && break
  echo "Passwords didn't match, try again."
done
if [ -n "$PW" ] && [ "${#PW}" -lt 8 ]; then echo "Password must be 8+ characters." >&2; exit 1; fi
case "$PW" in *'##'*) echo "Password can't contain '##'." >&2; exit 1 ;; esac

echo "==> Enabling Cloud Run, Cloud Build and Artifact Registry (first time takes a minute)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT" --quiet

echo "==> Service account that can read/write only your bucket"
SA_NAME="applypilot-ui"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "ApplyPilot dashboard"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" --role roles/storage.objectAdmin --quiet >/dev/null

echo "==> Packaging the dashboard only"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/applypilot"
cp "$REPO/applypilot/__init__.py" "$STAGE/applypilot/"
cp -r "$REPO/applypilot/ui" "$STAGE/applypilot/ui"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO/deploy/cloudrun/Dockerfile" "$STAGE/Dockerfile"
echo "    files: $(cd "$STAGE" && find . -type f | sort | tr '\n' ' ')"

ENVS="APPLYPILOT_GCS_URI=$GCS_URI"
[ -n "$PW" ] && ENVS="$ENVS##APPLYPILOT_UI_PASSWORD=$PW"

echo "==> Building and deploying (a few minutes)"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source "$STAGE" \
  --service-account "$SA" --allow-unauthenticated --max-instances 1 --memory 512Mi \
  --update-env-vars "^##^$ENVS" --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
cat <<MSG

Done. Your dashboard: $URL
Bookmark it (also on your phone) and log in with your password.
To update it later: git pull, then run this same command again.
MSG

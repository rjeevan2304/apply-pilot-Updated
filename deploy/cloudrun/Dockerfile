# ApplyPilot dashboard only (applypilot/ui + package marker). Built by deploy.sh.
FROM python:3.12-slim
RUN pip install --no-cache-dir "google-cloud-storage>=2.10"
WORKDIR /app
COPY applypilot /app/applypilot
ENV PYTHONUNBUFFERED=1 APPLYPILOT_UI_DIR=/tmp/applypilot-ui
# Cloud Run sets PORT; the server then listens on 0.0.0.0 and requires APPLYPILOT_UI_PASSWORD.
CMD ["sh", "-c", "exec python -m applypilot.ui --gcs \"$APPLYPILOT_GCS_URI\" --no-open"]

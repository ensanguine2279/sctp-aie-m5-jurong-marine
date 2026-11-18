# The agent API (scripts/agent_api.py) as a container: Cloud Run service `jme-agent-api`,
# private (only the page's service account may call it). Deploy from the repo root:
#   gcloud run deploy jme-agent-api --source . --no-allow-unauthenticated ...
# Since 16 Nov 2026 (phase 4) this root Dockerfile is the API's; the page has its own, in
# frontend/. scripts/guard_names.py must be in scripts/, and common/ (db_config, sop_search) next to it: without the staff name list the API
# refuses to start (fail closed).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY scripts/ scripts/
COPY common/ common/

# Cloud Run sets $PORT (8080). AGENT_LOG_FILE=off: the JSON lines go to stderr (Cloud Logging)
# only; the container's disk is temporary.
ENV AGENT_LOG_FILE=off
CMD uvicorn agent_api:create_app --factory --app-dir scripts \
    --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 75
# The demo page (scripts/agent_ui.py) as a container, e.g. for Google Cloud Run.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements-app.txt .
RUN pip install -r requirements-app.txt

COPY scripts/ scripts/

# Cloud Run sets $PORT (8080). AGENT_LOG_FILE=off: log to stderr (Cloud Logging), not the disk.
ENV AGENT_LOG_FILE=off
CMD streamlit run scripts/agent_ui.py \
    --server.port=${PORT:-8080} --server.address=0.0.0.0 \
    --server.headless=true --browser.gatherUsageStats=false
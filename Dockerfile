
# The demo page (scripts/agent_ui.py) as a container, e.g. for Google Cloud Run.
# Since phase 3 (15 Nov 2026) the page only calls the agent API, so the image holds the page
# and its two helpers and nothing else: no agent code, no database driver, and no staff
# name list (scripts/guard_names.py stays with the API). It needs AGENT_API_URL and
# AGENT_API_KEY (and APP_PASSWORD) at run time.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements-app.txt .
RUN pip install -r requirements-app.txt

COPY scripts/agent_ui.py scripts/api_client.py scripts/answer_view.py scripts/

# Cloud Run sets $PORT (8080).
CMD streamlit run scripts/agent_ui.py \
    --server.port=${PORT:-8080} --server.address=0.0.0.0 \
    --server.headless=true --browser.gatherUsageStats=false
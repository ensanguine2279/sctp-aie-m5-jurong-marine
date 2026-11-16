# The demo page as a container (Cloud Run service `jme-agent`), built from this folder alone:
#   gcloud run deploy jme-agent --source frontend ...
# It holds the page and its client and nothing else: no agent code, no database driver, no
# staff name list. At run time it needs AGENT_API_URL, AGENT_API_KEY, AGENT_API_AUTH=google
# (the API is private) and APP_PASSWORD.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agent_ui.py api_client.py ./

# Cloud Run sets $PORT (8080).
CMD streamlit run agent_ui.py \
    --server.port=${PORT:-8080} --server.address=0.0.0.0 \
    --server.headless=true --browser.gatherUsageStats=false
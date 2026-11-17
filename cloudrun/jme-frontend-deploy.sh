#!/bin/bash
# Deploy the page (Cloud Run service jme-frontend, public behind APP_PASSWORD) from
# ~/jme-frontend-deploy.zip, and let it call the private API.
# Run:  bash ~/jme-frontend-deploy.sh      (settings come from ~/jme-env.sh)
set -euo pipefail

source ~/jme-env.sh
: "${PROJECT:?not set in jme-env.sh}" "${REGION:?not set}" "${PAGE_SA:?not set}"
gcloud config set project "$PROJECT" --quiet

# The API's address, looked up now (never a stale copy): the identity token is issued for it.
API_URL=$(gcloud run services describe jme-agent-api --region "$REGION" --format='value(status.url)')
: "${API_URL:?jme-agent-api not found: deploy the API first}"
echo "API: $API_URL"

# Let the page's service account call the private API (harmless if it already can).
gcloud run services add-iam-policy-binding jme-agent-api --region "$REGION" \
  --member="serviceAccount:$PAGE_SA" --role=roles/run.invoker --quiet >/dev/null
echo "invoker granted to $PAGE_SA"

rm -rf ~/jme-frontend && mkdir ~/jme-frontend && cd ~/jme-frontend
unzip -q ~/jme-frontend-deploy.zip
grep -q "^CMD streamlit run agent_ui.py" frontend/Dockerfile \
  || { echo "frontend/Dockerfile isn't the page's"; exit 1; }
ls -A frontend/
test ! -e scripts || { echo "scripts/ in the page zip: leave it out"; exit 1; }

gcloud run deploy jme-frontend --source frontend --region "$REGION" \
  --allow-unauthenticated --session-affinity --service-account "$PAGE_SA" \
  --min-instances 0 --max-instances 1 --memory 512Mi --timeout 3600 \
  --set-env-vars "AGENT_API_URL=$API_URL,AGENT_API_AUTH=google" \
  --set-secrets "AGENT_API_KEY=agent-api-key:latest,APP_PASSWORD=app-password:latest"

echo "page: $(gcloud run services describe jme-frontend --region "$REGION" --format='value(status.url)')"
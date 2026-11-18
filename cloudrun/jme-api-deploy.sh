#!/bin/bash
# Deploy the agent API (Cloud Run service jme-agent-api, private) from ~/jme-api-deploy.zip.
# Run:  bash ~/jme-api-deploy.sh      (settings come from ~/jme-env.sh)
set -euo pipefail                      # stop at the first error, or an unset variable

source ~/jme-env.sh
: "${PROJECT:?not set in jme-env.sh}" "${REGION:?not set}" "${API_SA:?not set}"
gcloud config set project "$PROJECT" --quiet

# A fresh folder, so no old file can get into the build.
rm -rf ~/jme-api && mkdir ~/jme-api && cd ~/jme-api
unzip -q ~/jme-api-deploy.zip
grep -q "^CMD uvicorn agent_api:create_app" Dockerfile \
  || { echo "Dockerfile isn't the API's (expected CMD uvicorn ...)"; exit 1; }
test -f scripts/guard_names.py || { echo "scripts/guard_names.py missing: the API won't start"; exit 1; }
if [ -e scripts/agent_ui.py ] || [ -e frontend ]; then
  echo "page files in the API zip"; exit 1
fi
# Since 17 Nov 2026 the agent imports db_config and the SOP search from common/
for f in common/db_config.py common/sop_search.py; do
  test -f "$f" || { echo "$f missing: zip common/ with the API"; exit 1; }
done
grep -q "^COPY common/ common/" Dockerfile || { echo "Dockerfile doesn't copy common/"; exit 1; }
if [ -e ingest ] || [ -e scripts/ingest_sops.py ]; then
  echo "loader files in the API zip (ingest/ stays on your computer)"; exit 1
fi

gcloud run deploy jme-agent-api --source . --region "$REGION" \
  --no-allow-unauthenticated --service-account "$API_SA" \
  --min-instances 0 --max-instances 1 --memory 1Gi --timeout 600 --concurrency 20 \
  --set-env-vars "CMMS_DB_HOST=jme-cmms-sctp-aie-m5.d.aivencloud.com,CMMS_DB_PORT=26293,CMMS_DB_NAME=jme_cmms,CMMS_DB_AGENT_USERNAME=agent_ro,OPENAI_BASE_URL=https://35-240-177-226.sslip.io,AGENT_LOG_FILE=off" \
  --set-secrets "CMMS_DB_AGENT_PASSWORD=agent-db-password:latest,OPENAI_API_KEY=openai-api-key:latest,CONV_DB_URL=conv-db-url:latest,AGENT_API_KEY=agent-api-key:latest"

API_URL=$(gcloud run services describe jme-agent-api --region "$REGION" --format='value(status.url)')
echo "API: $API_URL"
echo "health with your token:"
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$API_URL/health"; echo
echo -n "health without a token (expect 403): "
curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/health"
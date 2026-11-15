#!/bin/bash
#
# Deploy the jme-agent application (streamlit) to Google Cloud Run
# This script removes any existing jme-agent directory, unzips the latest package, and deploys it to Cloud Run.
# Assumes the jme-agent package is available as jme-agent.zip in the current directory.
#
rm -rf jme-agent && mkdir jme-agent && unzip -o jme-agent.zip -d jme-agent && cd jme-agent

gcloud run deploy jme-agent --source . --region asia-southeast1 \
  --allow-unauthenticated --session-affinity \
  --min-instances 0 --max-instances 1 --memory 1Gi --timeout 600 \
  --set-env-vars "CMMS_DB_HOST=jme-cmms-sctp-aie-m5.d.aivencloud.com,CMMS_DB_PORT=26293,CMMS_DB_NAME=jme_cmms,CMMS_DB_AGENT_USERNAME=agent_ro,CMMS_DB_SESSION_USERNAME=agent_sessions,OPENAI_BASE_URL=https://35-240-177-226.sslip.io,AGENT_LOG_FILE=off" \
  --set-secrets "CMMS_DB_AGENT_PASSWORD=agent-db-password:latest,CMMS_DB_SESSION_PASSWORD=agent-session-db-password:latest,OPENAI_API_KEY=openai-api-key:latest"
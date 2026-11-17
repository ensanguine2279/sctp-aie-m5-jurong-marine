#!/bin/bash
# Check the health of the jme-agent-api service on Google Cloud Run. Requires $API_URL to be set.
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" $API_URL/health
curl -s -o /dev/null -w '%{http_code}\n' $API_URL/health    # without a token: 403
PROJECT=$(gcloud config get-value project)
REGION=asia-southeast1
API_SA=jme-agent-api@$PROJECT.iam.gserviceaccount.com
PAGE_SA=jme-agent-page@$PROJECT.iam.gserviceaccount.com
API_URL=https://jme-agent-api-407651890257.asia-southeast1.run.app
echo "$PROJECT | $API_SA | $PAGE_SA | $API_URL"
gcloud iam service-accounts list --format='value(email)' | grep jme-agent   # both should be listed
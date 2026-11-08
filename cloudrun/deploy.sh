#!/bin/bash
#
# Deploy the jme-agent application (streamlit) to Google Cloud Run
# This script removes any existing jme-agent directory, unzips the latest package, and deploys it to Cloud Run.
# Assumes the jme-agent package is available as jme-agent.zip in the current directory.
#
rm -rf jme-agent && mkdir jme-agent && unzip -o jme-agent.zip -d jme-agent && cd jme-agent
gcloud run deploy jme-agent --source . --region asia-southeast1
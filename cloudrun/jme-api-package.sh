#!/bin/bash
# Package the jme-api application for deployment to Google Cloud Run
# This script creates a zip file containing the necessary files for deployment.
# Excludes Python cache files.
rm -f jme-api-deploy.zip
zip -r jme-api-deploy.zip Dockerfile .dockerignore .gcloudignore requirements-api.txt scripts \
    -x '*/__pycache__/*' '*.pyc'
unzip -l jme-api-deploy.zip | grep -E ' (Dockerfile|\.dockerignore|\.gcloudignore|requirements-api\.txt|scripts/(agent_api|guard_names)\.py)$'
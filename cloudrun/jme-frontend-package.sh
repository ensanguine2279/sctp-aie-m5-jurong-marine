#!/bin/bash
# Package the jme-frontend application for deployment to Google Cloud Run
# This script creates a zip file containing the necessary files for deployment.
# Excludes Python cache files and frontend environment files.
rm -f jme-frontend-deploy.zip
zip -r jme-frontend-deploy.zip frontend -x '*/__pycache__/*' '*.pyc' 'frontend/.env*'
unzip -l jme-frontend-deploy.zip
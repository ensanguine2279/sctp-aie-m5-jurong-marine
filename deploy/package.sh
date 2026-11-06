#!/bin/bash
#
# Package the jme-agent application for deployment to Google Cloud Run
# This script creates a zip file containing the necessary files for deployment.
# Excludes Python cache files.
#
zip -r jme-agent.zip Dockerfile .dockerignore requirements-app.txt scripts -x "scripts/__pycache__/*"
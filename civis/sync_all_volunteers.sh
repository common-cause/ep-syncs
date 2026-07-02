#!/bin/bash
# Civis entrypoint for the all-volunteers sync (PTV users_csv -> BigQuery).
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/sync_all_volunteers.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, etc.).
# BQ-only phase: the airtable extra isn't needed yet.
# Pinned to a ccef-connections release tag — bump deliberately when upgrading.
pip install "ccef-connections[bigquery] @ git+https://github.com/common-cause/ccef_connections.git@v0.2.0"
python app/sync_all_volunteers.py

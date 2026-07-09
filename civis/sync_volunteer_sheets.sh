#!/bin/bash
# Civis entrypoint for the volunteer sheets sync (BigQuery -> Google Sheets
# exports for states and coalition partners).
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/sync_volunteer_sheets.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, etc.). Requires BIGQUERY_CREDENTIALS_PASSWORD and
# GOOGLE_SHEETS_CREDENTIALS_PASSWORD on the job.
# Pinned to a ccef-connections release tag — bump deliberately when upgrading.
pip install "ccef-connections[bigquery,sheets] @ git+https://github.com/common-cause/ccef_connections.git@v0.2.0"
python app/sync_volunteer_sheets.py

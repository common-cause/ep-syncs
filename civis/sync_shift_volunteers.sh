#!/bin/bash
# Civis entrypoint for the shift volunteers sync.
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/sync_shift_volunteers.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, etc.).
pip install "ccef-connections[airtable,bigquery] @ git+https://github.com/common-cause/ccef_connections.git"
python app/sync_shift_volunteers.py

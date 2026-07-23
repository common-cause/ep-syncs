#!/bin/bash
# Civis entrypoint for the Airtable bases capture (Airtable -> BigQuery).
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/sync_airtable_bases.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, etc.).
# Needs airtable (records + metadata API via get_base_schema, added in
# v0.5.0), bigquery, and pandas extras. The Civis datascience-python image
# already ships pandas/pyarrow; the extra keeps the dependency declared.
# Pinned to a ccef-connections release tag — bump deliberately when upgrading.
pip install "ccef-connections[airtable,bigquery,pandas] @ git+https://github.com/common-cause/ccef_connections.git@v0.5.0"
python app/sync_airtable_bases.py

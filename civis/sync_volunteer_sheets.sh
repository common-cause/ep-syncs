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
# v0.2.0 -> v0.12.0 on 2026-08-28: a target with zero volunteers writes a
# header-only _data tab, and format_header_row could not freeze row 1 of a
# 1-row grid ("You can't freeze all visible rows"). The 15 partner targets
# registered 2026-08-24 all had zero volunteers, so all 15 failed to provision
# and took the job red 08-25..08-28 while the other 158 targets kept updating.
# Fixed in v0.12.0. Verified before bumping: every method this job calls
# (get_or_create_spreadsheet, write_worksheet, format_header_row,
# delete_worksheet_if_exists, BigQueryConnector.query) is signature-identical
# across the ten releases in between.
pip install "ccef-connections[bigquery,sheets] @ git+https://github.com/common-cause/ccef_connections.git@v0.12.0"
python app/sync_volunteer_sheets.py

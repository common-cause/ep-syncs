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
# Verified before bumping: every method this job calls
# (get_or_create_spreadsheet, write_worksheet, format_header_row,
# delete_worksheet_if_exists, BigQueryConnector.query) is signature-identical
# across the ten releases in between.
#
# Takes v0.12.1, not v0.12.0. v0.12.0 fixed only the freeze half: it grows a
# header-only tab to two rows so format_header_row can freeze row 1, but
# write_worksheet still resized back down to len(data) == 1 on the NEXT run,
# and Sheets will not shrink a tab to a single frozen row. That converted a
# first-provisioning failure into a permanent one, and all 15 targets failed
# again on the 2026-08-28 verification run. v0.12.1 stops write_worksheet
# shrinking a tab to or below its frozen row count.
#
# v0.12.1 -> v0.13.0 (2026-09-03): THE FIX THIS JOB ACTUALLY NEEDED. With the
# provisioning bug gone it was still failing 5 of 8 nights, and never on quota
# --- always `APIError: [503]: The service is currently unavailable.`
# retry_google_operation matched 429 only, and gspread has no retry of its own,
# so a 503 got zero connector-level retries; it fell through to this script's
# 2-attempt loop, which waits a fixed 65s tuned for a quota window and then
# gives up. At ~700 Sheets/Drive calls per run over 173 targets, a
# fraction-of-a-percent 503 rate reliably kills one or two targets a night.
# v0.13.0 retries transient 5xx for gspread specifically (500/502/503/504, not
# 501), leaving the api_core path alone because BigQuery DML shares that
# decorator. This script's 65s loop is now a genuine last resort.
pip install "ccef-connections[bigquery,sheets] @ git+https://github.com/common-cause/ccef_connections.git@v0.13.0"
python app/sync_volunteer_sheets.py

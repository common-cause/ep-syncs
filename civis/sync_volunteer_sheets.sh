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
pip install "ccef-connections[bigquery,sheets] @ git+https://github.com/common-cause/ccef_connections.git@v0.12.1"
python app/sync_volunteer_sheets.py

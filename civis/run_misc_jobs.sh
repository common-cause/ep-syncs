#!/bin/bash
# Civis entrypoint for the miscellaneous EP sync jobs runner.
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/run_misc_jobs.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, schedule). Requires BIGQUERY_CREDENTIALS_PASSWORD,
# GOOGLE_SHEETS_CREDENTIALS_PASSWORD, ASANA_API_KEY_PASSWORD and
# AIRTABLE_API_KEY_PASSWORD on the job.
#
# ONE nightly Civis job (~3 AM ET) runs this with no arguments; the runner
# self-selects the tasks scheduled for tonight's ET weekday from
# misc_jobs_schedule.yaml. Change which tasks run which nights by editing that
# YAML and pushing -- no Civis-side change.
#
# Pinned to a ccef-connections release tag -- bump deliberately when upgrading.
# The pin must cover every connector any registered task imports AND every
# method it calls on one. Both halves have bitten:
#   - asana_ep_kanban needs AsanaConnector (base install since v0.3.0) -- the
#     v0.2.0 pin broke every nightly run 2026-07-30..08-17.
#   - hub_host_tracker calls SheetsWriterConnector.open_spreadsheet (added in
#     v0.8.0) -- the v0.7.1 pin imported fine and then failed at call time with
#     AttributeError, breaking every nightly run 2026-08-25..08-28. The task ran
#     locally throughout, because the editable install is always newer.
# A task verified locally proves nothing about the pin; check the tag.
# pyyaml (schedule file) and tzdata (America/New_York weekday) are pinned
# explicitly so the container always has them regardless of base image.
#
# Extras: bigquery + sheets (SheetsConnector) + pandas (infrastructure_sheet
# loads via BigQueryConnector.load_dataframe, and pandas is its OWN extra -- the
# bigquery extra does not pull it) + airtable (airtable_base_visibility calls
# AirtableConnector.list_bases, added in v0.5.0, so the PIN is fine and it was
# the EXTRA that was missing -- this job had never installed pyairtable).
# The base image ships pandas, but naming it here means the job doesn't depend
# on that staying true.
pip install "ccef-connections[bigquery,sheets,airtable,pandas] @ git+https://github.com/common-cause/ccef_connections.git@v0.12.1"
pip install pyyaml tzdata
python app/run_misc_jobs.py

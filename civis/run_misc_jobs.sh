#!/bin/bash
# Civis entrypoint for the miscellaneous EP sync jobs runner.
# GitHub-backed job: Civis clones this repo into app/, so set the job body to:
#     bash app/civis/run_misc_jobs.sh
# Edit this file (not the Civis UI) to change setup/run steps. See
# civis/SCHEDULED_SCRIPTS.md for the full job setup spec (docker image,
# credentials, schedule). Requires BIGQUERY_CREDENTIALS_PASSWORD and
# GOOGLE_SHEETS_CREDENTIALS_PASSWORD on the job.
#
# ONE nightly Civis job (~3 AM ET) runs this with no arguments; the runner
# self-selects the tasks scheduled for tonight's ET weekday from
# misc_jobs_schedule.yaml. Change which tasks run which nights by editing that
# YAML and pushing -- no Civis-side change.
#
# Pinned to a ccef-connections release tag -- bump deliberately when upgrading.
# pyyaml (schedule file) and tzdata (America/New_York weekday) are pinned
# explicitly so the container always has them regardless of base image.
pip install "ccef-connections[bigquery,sheets] @ git+https://github.com/common-cause/ccef_connections.git@v0.2.0"
pip install pyyaml tzdata
python app/run_misc_jobs.py

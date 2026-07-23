# EP Syncs

Sync scripts connecting Protect the Vote (PTV) shift scheduling and Airtable to BigQuery for election protection volunteer data.

## Project Type
bigquery

## Connections & External APIs

**All external API connections use `ccef-connections`.** Do not write your own BigQuery,
Airtable, or PTV clients directly in this project.

The shared library lives at:
```
C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections
```
Install it with (heavy deps are behind extras since 0.2.0):
```bash
pip install -e "C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections[airtable,bigquery]"
```
Civis jobs install it pinned to a release tag from GitHub instead — see the
`civis/*.sh` entrypoints.

**If a PTV API wrapper or Airtable client doesn't exist in `ccef-connections` yet:**
Spec it out and build it *in `ccef-connections`*, then import it here.
Do not duplicate connection logic in individual projects.

## Credential Pattern
All credentials follow `{CREDENTIAL_NAME}_PASSWORD` in `.env` (Civis-compatible).
JSON credentials are stored as unquoted JSON strings. Never commit `.env`.

Active credentials in `.env` (all seeded):
- `BIGQUERY_CREDENTIALS_PASSWORD` — GCP: `proj-tmc-mem-com`, SA: `com-dbt@`
- `AIRTABLE_API_KEY_PASSWORD` — Airtable PAT (Rob's "sync operations" token; PATs are scoped per-base)
- `PTV_API_KEY_PASSWORD` — PTV API key (username `colab` is the PTVConnector default; only the key is read)
- `GOOGLE_SHEETS_CREDENTIALS_PASSWORD` — GCP SA JSON: `sheets-controllers@sheets-controllers` (member of the "2026 EP Volunteer Exports" shared drive)

## PII / Data Handling

Row-level PII (names, emails, phones, street addresses, gift amounts) **never gets
committed to git** — repos here are org-visible via shared corpora and export pipelines.
Any directory that will receive raw dumps or query results gets gitignored BEFORE the
first file lands (allowlist known-clean file types; never enumerate known-bad files).
Committed derivatives must be masked or aggregated; fabricate example rows in docs.
Row-level people-data lives in access-controlled systems (BigQuery, ROI, Action Network,
shared Sheets) — point at it, don't copy it. Full policy: knowledge library entry
`pii-handling-policy` (`kl_get`).

## BigQuery MCP

The global `bigquery` MCP is active and pre-approved for this project. Use `bq_query(sql)` and `bq_list_tables(dataset)` to query data or inspect tables without leaving the conversation. Connects to `proj-tmc-mem-com` using the shared service account.

```
bq_query("SELECT * FROM ep.some_table LIMIT 5")
bq_list_tables("ep")
```

## Schema MCP (bq-schema-docs)

The global `schema` MCP provides field-level documentation for all 63 datasets in `proj-tmc-mem-com`. Use it to understand table structure before writing queries.

```
schema_list_datasets()                                                           # master index of all datasets
schema_get_dataset("ep")                                                         # README + data model overview
schema_list_tables("ep")                                                         # all table names in a dataset
schema_get_table("ep", "some_table")                                             # all fields + types
schema_search("volunteer", dataset="ep")                                         # find tables by keyword
```

All tools are pre-approved — no confirmation needed. Docs are auto-generated from INFORMATION_SCHEMA.

## Key Files
- `sync_shift_volunteers.py` — PTV `shift_volunteers_csv` (all 50 states + DC) → `ptv_raw_2026.shift_volunteers`; then Airtable upsert for each enabled `ep.shift_volunteer_sync_targets` row (the registry drives ONLY the Airtable leg — the BQ landing is national)
- `sync_all_volunteers.py` — PTV `users_csv` (all registered volunteers) → `ptv_raw_2026.users`; BQ-only, no Airtable leg yet
- `sync_airtable_bases.py` — registered Airtable bases → `ep_2026_raw` (typed per-(base,table) tables rebuilt each run + JSON history), driven by the `ep.airtable_sync_sources` registry; READ-ONLY toward Airtable. Design: `docs/airtable_bases_sync_spec.md`
- `sync_volunteer_sheets.py` — BQ roster → Google Sheets exports (one sheet per state, one per partner source code) in the "2026 EP Volunteer Exports" shared drive; partner-edit-safe (hidden `_data` tab + formula mirror), driven by the `ep.volunteer_sheet_targets` registry
- `run_misc_jobs.py` — shared runner for small, periodic exports that don't each warrant their own Civis job; one nightly Civis job (~3 AM ET) runs the tasks scheduled for tonight's ET weekday. Task identity lives in the `JOBS` registry; task timing lives in `misc_jobs_schedule.yaml`. Per-task failures isolated. Add a task = new `misc_jobs/` module with `run()` + a `JOBS` row + a YAML entry
- `misc_jobs/` — task modules for `run_misc_jobs.py`; today `event_975203_signups.py` (Mobilize event 975203 FL-training signups → Google Sheet)
- `misc_jobs_schedule.yaml` — per-task night-of-week schedule for `run_misc_jobs.py` (edit + push to re-time a task; no Civis change)
- `bq/ep_2026_cleaned/` — committed SQL for the `ep_2026_cleaned` interface layer (views + UDFs other projects consume; normalized email/phone contract). Applied via `apply_bq_views.py` (`--check` = drift check)
- `apply_bq_views.py` — apply/drift-check the `bq/ep_2026_cleaned/*.sql` DDL in filename (dependency) order; `--render-generated` rewrites the 3x union-view snapshots
- `airtable_views.py` — generator for the ep_2026_cleaned Airtable union views (per-entity canonical contracts + heuristics + record-link resolution + registry `canonical_overrides`); re-run automatically at the end of every `sync_airtable_bases.py` run
- `docs/all_volunteers_sync_spec.md` — all-volunteers sync design + the deferred Airtable-leg notes
- `docs/volunteer_sheets_spec.md` — volunteer sheets sync design (row-stability contract, registry seeding, go-live checklist)
- `bq/shift_volunteer_sync_targets.sql` — DDL + registration contract for the sync-targets registry
- `bq/airtable_sync_sources.sql` — DDL + registration contract + seeds for the Airtable base registry (insert an enabled row = start capturing a base)
- `bq/airtable_records_history.sql` — DDL for the append-only JSON history of every captured Airtable record (incl. the ROW_NUMBER dedupe recipe — JSON cols can't SELECT DISTINCT)
- `bq/volunteer_sheet_targets.sql` — DDL + seeds for the sheet-targets registry (insert an enabled row = provision a sheet)
- `civis/SCHEDULED_SCRIPTS.md` — source-of-truth for the Civis jobs (schedules, credentials, failure modes); the `civis/*.sh` files are the real job bodies
- `count_2025_volunteers.py` — one-off counting script (not scheduled)
- `ptv_sync.py`, `parsons test.py` — legacy pre-ccef-connections reference only; do not copy patterns from them

## How to Run
```bash
python sync_shift_volunteers.py                    # shift sync (all states → BQ, registry targets → Airtable)
python sync_shift_volunteers.py --states NE,PA     # exact pull-set override (ops/testing)
python sync_shift_volunteers.py --bq-only          # skip the Airtable leg
python sync_all_volunteers.py                      # all-volunteers sync (all 50 states + DC)
python sync_all_volunteers.py --states NE,PA       # subset override for ops/testing
python sync_airtable_bases.py                      # Airtable capture (all enabled registry bases)
python sync_airtable_bases.py --bases ne_field_report,ut_quiz  # subset (ops/testing)
python sync_airtable_bases.py --list               # show discovered tables, write nothing
python sync_airtable_bases.py --check-access       # PAT coverage incl. disabled rows
python sync_volunteer_sheets.py                    # volunteer sheets sync (all enabled registry targets)
python sync_volunteer_sheets.py --targets NE,aclum # subset override for ops/testing
python run_misc_jobs.py                            # misc jobs scheduled for tonight (ET weekday)
python run_misc_jobs.py --as-of mon                # dry-run a specific night (still executes tasks)
python run_misc_jobs.py --only event_975203_signups # single misc task, ignore schedule (ops/testing)
python run_misc_jobs.py --list                     # list registered misc tasks + schedule, run nothing
```
All read credentials from `.env` locally; in Civis they run as scheduled
GitHub-backed container jobs (shift sync daily 6:00 AM ET, all-volunteers
daily 6:30 AM ET, Airtable capture not yet scheduled — planned 6:45 AM ET,
volunteer sheets not yet scheduled — planned 7:00 AM ET,
misc jobs nightly 3:00 AM ET, self-selecting tasks per
`misc_jobs_schedule.yaml`)
— see `civis/SCHEDULED_SCRIPTS.md` before touching schedules.

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

**PTV admin GUI (no API) uses the `ptv-tools` package, not ccef-connections.**
PTV has no admin API, so training signups and other admin-only data ride an
authenticated Playwright browser session. `ptv-tools` (installed editable, like
ccef-connections; owns login/session/verified state-scoping) is the session
layer; the operational scrapers (e.g. `sync_ptv_trainings.py`) live here. The
read-only CSV *API* (`users_csv`, `shift_volunteers_csv`) still goes through
ccef-connections' `PTVConnector`. Requires `PTV_ADMIN_EMAIL` in `.env` and a
saved session at `~/.ptv-tools/storage_state.json` (per-machine).

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
- `sync_shift_volunteers.py` — PTV `shift_volunteers_csv` (all 50 states + DC) → `ptv_raw_2026.shift_volunteers`; then Airtable upsert for each enabled `ep.shift_volunteer_sync_targets` row (the registry drives ONLY the Airtable leg — the BQ landing is national). Appends per-run telemetry to `ep.shift_sync_log` — the Airtable leg's duplicate-key skips and per-state "stopped reporting" both exit 0, so the log is the only place they're visible. Airtable matches the upsert key **case-sensitively** (verified 2026-08-19); the duplicate guard gates on exact spelling and reports case variants separately
- `sync_all_volunteers.py` — PTV `users_csv` (all registered volunteers) → `ptv_raw_2026.users`; BQ-only, no Airtable leg yet
- `sync_ptv_trainings.py` — PTV **admin GUI** training signups (no API exists) → `ptv_raw_2026.training_signups`, one row per (session, attendee) with a REAL signup timestamp + status/attended/role. Browser automation via the `ptv-tools` package (session layer), verified per-state scoping, daily snapshot partitioned by `as_of_date`. Iterates all states; scheduled/on-demand trainings both handled. BQ-only; built + **validated full 50+DC run (2,603 rows / 19 states / 0 failures / ~16 min)**. **Local-only, not Civis** (browser + Outlook magic-code login are inherently local) — **LIVE** as Windows Task Scheduler job "EP PTV Trainings Sync" (daily 4 AM ET, interactive logon, first fire 2026-07-25) via `scripts/run_ptv_trainings.ps1`; self-heals auth (headless verify, headed Outlook relogin only if dead). Design: `docs/ptv_trainings_sync_spec.md`
- `sync_airtable_bases.py` — registered Airtable bases → `ep_2026_raw` (typed per-(base,table) tables rebuilt each run + JSON history), driven by the `ep.airtable_sync_sources` registry; READ-ONLY toward Airtable. Design: `docs/airtable_bases_sync_spec.md`
- `sync_volunteer_sheets.py` — BQ roster → Google Sheets exports (one sheet per state, one per partner source code) in the "2026 EP Volunteer Exports" shared drive; partner-edit-safe (hidden `_data` tab + formula mirror), driven by the `ep.volunteer_sheet_targets` registry
- `run_misc_jobs.py` — shared runner for small, periodic exports that don't each warrant their own Civis job; one nightly Civis job (~3 AM ET) runs the tasks scheduled for tonight's ET weekday. Task identity lives in the `JOBS` registry; task timing lives in `misc_jobs_schedule.yaml`. Per-task failures isolated. Add a task = new `misc_jobs/` module with `run()` + a `JOBS` row + a YAML entry
- `misc_jobs/` — task modules for `run_misc_jobs.py`: `infrastructure_sheet.py` (50-State EP Coalition Plan **Infrastructure tabs** → `ep_2026_raw.coalition_plan_infrastructure`, melted one row per (state, sheet column), cell text verbatim; READ-ONLY toward the program team's spreadsheet; daily, ~1,632 rows/night; ported from ep-dashboards 2026-08-19 so the ingest runs on Civis and the raw layer lives here. All parsing is in `ep_2026_cleaned.coalition_plan_infrastructure`. Design: `docs/coalition_plan_infrastructure_sync_spec.md`), `asana_ep_kanban.py` (registered Asana EP boards → `asana_raw_2026` daily snapshots; registry `ep.asana_sync_sources`; READ-ONLY toward Asana; **LIVE** since 2026-07-29, feeds `ep_2026_cleaned.asana_pipeline` + the `source_system='asana'` branch of `volunteers`) and `event_975203_signups.py` (RETIRED 2026-08-18 — unregistered reference example; was Mobilize event 975203 FL-training signups → Google Sheet, series ended 2026-08-16). **Asana capture is for states that deploy volunteers outside PTV/Airtable entirely** — NM (CCNM) is the 2026 case. Design + the open questions for NM: `docs/asana_nm_sync_spec.md`. Note `asana_raw_2026` must be created by an admin — `com-dbt@` has no `bigquery.datasets.create`, and `CREATE SCHEMA IF NOT EXISTS` 403s even when the dataset exists
- `misc_jobs_schedule.yaml` — per-task night-of-week schedule for `run_misc_jobs.py` (edit + push to re-time a task; no Civis change)
- `bq/ep_2026_cleaned/` — committed SQL for the `ep_2026_cleaned` interface layer (views + UDFs other projects consume; normalized email/phone/state contract — `norm_state` added 2026-08-19 so sheet-derived sources join to the rest of the layer). Applied via `apply_bq_views.py` (`--check` = drift check)
- `apply_bq_views.py` — apply/drift-check the `bq/ep_2026_cleaned/*.sql` DDL in filename (dependency) order; `--render-generated` rewrites the 3x union-view snapshots
- `airtable_views.py` — generator for the ep_2026_cleaned Airtable union views (per-entity canonical contracts + heuristics + record-link resolution + registry `canonical_overrides`); re-run automatically at the end of every `sync_airtable_bases.py` run
- `docs/ep_2026_cleaned_spec.md` — interface-layer spec (invariants, view contracts, union mechanism, verification suite, consumer status). Other projects consume `ep_2026_cleaned`, not raw datasets
- `docs/airtable_bases_sync_spec.md` — Airtable capture design + the `ep_2026_raw` landing-zone contract the interface layer builds against
- `docs/all_volunteers_sync_spec.md` — all-volunteers sync design + the deferred Airtable-leg notes
- `docs/ptv_trainings_sync_spec.md` — PTV trainings GUI-scrape design (URL hierarchy, attendee DOM, scheduled-vs-on-demand branch, table contract + dedupe recipe, feeds-ep_dashboards note, Civis go-live checklist)
- `docs/asana_nm_sync_spec.md` — NM Asana board recon + capture design; also the **intake contract for absorbing ANY state tool outside our toolset** (§6) and the open question list for the NM program (§7). Read before wiring up another state's own system
- `docs/asana_connector_spec.md` — the buildout spec that produced `AsanaConnector` in ccef-connections (historical; the connector shipped in v0.3.0)
- `bq/asana_ep_kanban_tasks.sql`, `bq/asana_projects.sql` — DDL for the Asana landing tables (`CREATE TABLE IF NOT EXISTS`, partitioned by `as_of_date`, so the sync self-heals a fresh env)
- `bq/asana_sync_sources.sql` — DDL + registration contract + NM seed for the Asana board registry. Carries board **conventions** (stage order, where email/phone live) as data, so a state improving its board is a registry UPDATE rather than a code change
- `bq/ptv_training_signups.sql` — DDL for the training-signups landing table (partitioned by `as_of_date`, `CREATE TABLE IF NOT EXISTS` so the sync self-heals a fresh env)
- `bq/coalition_plan_infrastructure.sql` — DDL for the Coalition Plan Infrastructure melt landing table. Carries the *why* of long format (the program team renames columns without telling anyone) and why it lives in `ep_2026_raw` rather than a per-source dataset (`com-dbt@` can't create datasets). Contains staff names in `cell_value` where `col_name='State Lead'`
- `docs/coalition_plan_infrastructure_sync_spec.md` — Coalition Plan Infrastructure sync design + the ep-dashboards handoff contract (why the melt stays dumb, why the parse is in the cleaned view, and the re-point sequencing that would otherwise go stale silently)
- `docs/volunteer_sheets_spec.md` — volunteer sheets sync design (row-stability contract, registry seeding, go-live checklist)
- `bq/shift_volunteer_sync_targets.sql` — DDL + registration contract for the sync-targets registry
- `bq/shift_sync_log.sql` — DDL for `sync_shift_volunteers.py` per-run telemetry (one row per stage/scope; `CREATE TABLE IF NOT EXISTS`, self-heals). Carries the *why* — the silent-failure modes it exists to surface — plus the monitoring queries. Contains PII in `skipped_keys`
- `bq/v_shift_volunteers_current.sql` — DDL for the per-(state,email) view the Airtable leg reads back. Hand-applied; **not** covered by `apply_bq_views.py` (that script is hardcoded to `ep_2026_cleaned`). Read the email-case note before re-keying it
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
python sync_ptv_trainings.py                       # training signups (GUI scrape, all states → BQ)
python sync_ptv_trainings.py --states OH,PA        # subset override for ops/testing
python sync_ptv_trainings.py --list                # scrape+print current state's training list; no switch, no write
python sync_airtable_bases.py                      # Airtable capture (all enabled registry bases)
python sync_airtable_bases.py --bases ne_field_report,ut_quiz  # subset (ops/testing)
python sync_airtable_bases.py --list               # show discovered tables, write nothing
python sync_airtable_bases.py --check-access       # PAT coverage incl. disabled rows
python sync_volunteer_sheets.py                    # volunteer sheets sync (all enabled registry targets)
python sync_volunteer_sheets.py --targets NE,aclum # subset override for ops/testing
python run_misc_jobs.py                            # misc jobs scheduled for tonight (ET weekday)
python run_misc_jobs.py --as-of mon                # dry-run a specific night (still executes tasks)
python run_misc_jobs.py --only asana_ep_kanban      # single misc task, ignore schedule (ops/testing)
python run_misc_jobs.py --list                     # list registered misc tasks + schedule, run nothing
python misc_jobs/asana_ep_kanban.py                # Asana capture standalone (writes)
python misc_jobs/asana_ep_kanban.py --dry-run      # pull + report distributions, write nothing
python misc_jobs/infrastructure_sheet.py           # Coalition Plan Infrastructure melt standalone (writes)
python misc_jobs/infrastructure_sheet.py --dry-run # melt + report fill counts, write nothing
```
All read credentials from `.env` locally; in Civis they run as scheduled
GitHub-backed container jobs (shift sync daily 6:00 AM ET, all-volunteers
daily 6:30 AM ET, Airtable capture daily 6:45 AM ET,
volunteer sheets daily 7:00 AM ET,
misc jobs nightly 3:00 AM ET, self-selecting tasks per
`misc_jobs_schedule.yaml`)
— see `civis/SCHEDULED_SCRIPTS.md` before touching schedules.

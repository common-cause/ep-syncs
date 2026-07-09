# EP Syncs

> Sync scripts connecting Protect the Vote (PTV) shift scheduling and Airtable to BigQuery for election protection volunteer data.

## Setup

```bash
# Install the shared connections library (do this once per machine).
# Since ccef-connections 0.2.0 the heavy deps live behind extras; this
# project needs [airtable,bigquery,sheets] (PTV is covered by the base install).
pip install -e "C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections[airtable,bigquery,sheets]"

pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

## Usage

### Shift volunteers sync

Pulls volunteer signups from PTV's `shift_volunteers_csv` endpoint per state,
appends a daily snapshot to `ptv_raw_2026.shift_volunteers` (date-partitioned
on `as_of_date`), then upserts a per-volunteer summary into Airtable for each
enabled target in `proj-tmc-mem-com.ep.shift_volunteer_sync_targets`.

```bash
python sync_shift_volunteers.py
```

Designed to be scheduled in Civis. Reads credentials from environment
variables (Civis injects these from Civis Credentials).

**Configuring sync targets:** sync targets live in the BigQuery registry
table `ep.shift_volunteer_sync_targets`. They're written by
`ep-airtable-utilities` as the final step of taking a new base live (see
`bq/shift_volunteer_sync_targets.sql` for the schema and
`civis/SCHEDULED_SCRIPTS.md` for the manual-INSERT escape hatch). Each row
maps a state to one Airtable destination (base + table). A state can appear
in multiple rows when there are multiple downstream bases (primary plus
coalition partners, for example). The default BQ-col → Airtable-col mapping
lives as `DEFAULT_FIELD_MAP` in `sync_shift_volunteers.py`; per-target
overrides go in the row's `field_map_overrides` JSON column.

**Per-state failure isolation:** a PTV pull failing for one state does not
abort the run. Failed states are logged and skipped at the Airtable stage.
The script exits non-zero if any state or sync target failed.

**Idempotency:** rerunning on the same day replaces today's partition rows
for the targeted states before re-inserting. The current-state view dedupes
exact-duplicate rows defensively.

### All-volunteers sync

Pulls all *registered* volunteers (not just shift signups) from PTV's
`users_csv` endpoint for all 50 states + DC, and appends a daily snapshot to
`ptv_raw_2026.users` (date-partitioned on `as_of_date`, clustered by
`state, email`). BQ-only for now — no Airtable leg (deferred; see
`docs/all_volunteers_sync_spec.md` §7). `v_users_current` exposes one cleaned
row per (state, email) from each state's latest snapshot.

```bash
python sync_all_volunteers.py                 # all 50 states + DC
python sync_all_volunteers.py --states NE,PA  # override (ops / testing)
```

Same idempotency and per-state failure isolation as the shift sync. Inserts
are chunked (500 rows/request) because `users_csv` returns tens of thousands
of rows. Live in Civis as "All Volunteers Sync" (daily 6:30 AM ET) — see
`civis/SCHEDULED_SCRIPTS.md`.

### Volunteer sheets sync

Maintains coalition/state-facing Google Sheets from the BQ volunteer roster:
one spreadsheet per state and one per partner source code, in the
"2026 EP Volunteer Exports" shared-drive folder. Each sheet's hidden `_data`
tab is rewritten every run; the visible `Volunteers` tab mirrors it via an
array formula, so partner annotations to the right of the data block survive
refreshes (rows are append-only in stable PTV-id order). Sheets are defined
by registry rows in `ep.volunteer_sheet_targets` — inserting an enabled row
provisions a new sheet on the next run (see `bq/volunteer_sheet_targets.sql`).

```bash
python sync_volunteer_sheets.py                    # all enabled targets
python sync_volunteer_sheets.py --targets NE,aclum # subset (ops / testing)
```

Reads BQ only (no PTV/Airtable). Design: `docs/volunteer_sheets_spec.md`.
Not yet scheduled in Civis — see `civis/SCHEDULED_SCRIPTS.md`.

## Project Structure

```
ep-syncs/
├── .claude/                              # Claude Code configuration
├── .env.example                          # Credential template (copy to .env, never commit .env)
├── README.md
├── requirements.txt
├── bq/
│   ├── shift_volunteer_sync_targets.sql  # DDL for the shift-sync targets registry table
│   └── volunteer_sheet_targets.sql       # DDL + seeds for the sheets-sync targets registry
├── civis/
│   ├── sync_shift_volunteers.sh          # Civis Container Script body (shift sync)
│   ├── sync_all_volunteers.sh            # Civis Container Script body (all-volunteers sync)
│   ├── sync_volunteer_sheets.sh          # Civis Container Script body (volunteer sheets sync)
│   └── SCHEDULED_SCRIPTS.md              # Civis job source-of-truth
├── docs/
│   ├── all_volunteers_sync_spec.md       # All-volunteers sync design + deferred Airtable notes
│   └── volunteer_sheets_spec.md          # Volunteer sheets sync design (BQ -> Google Sheets)
├── count_2025_volunteers.py              # One-off: count unique 2025 shift volunteers
├── parsons test.py                       # Legacy pre-ccef-connections reference — do not copy patterns
├── ptv_sync.py                           # Legacy pre-ccef-connections reference — do not copy patterns
├── sync_shift_volunteers.py              # PTV shift_volunteers_csv -> BQ -> Airtable sync
├── sync_all_volunteers.py                # PTV users_csv -> BQ sync (no Airtable leg)
└── sync_volunteer_sheets.py              # BQ roster -> Google Sheets exports (states + partners)
```

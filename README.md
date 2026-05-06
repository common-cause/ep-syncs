# EP Syncs

> Sync scripts connecting Protect the Vote (PTV) shift scheduling and Airtable to BigQuery for election protection volunteer data.

## Setup

```bash
# Install the shared connections library (do this once per machine)
pip install -e "C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections"

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

## Project Structure

```
ep-syncs/
├── .claude/                              # Claude Code configuration
├── .env.example                          # Credential template (copy to .env, never commit .env)
├── README.md
├── requirements.txt
├── bq/
│   └── shift_volunteer_sync_targets.sql  # DDL for the sync-targets registry table
├── civis/
│   ├── sync_shift_volunteers.sh          # Civis Container Script body
│   └── SCHEDULED_SCRIPTS.md              # Civis job source-of-truth
└── sync_shift_volunteers.py              # PTV -> BQ -> Airtable sync
```

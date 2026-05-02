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
target listed in `config/syncs.yaml`.

```bash
python sync_shift_volunteers.py
```

Designed to be scheduled in Civis. Reads credentials from environment
variables (Civis injects these from Civis Credentials).

**Configuring sync targets:** edit `config/syncs.yaml`. Each entry maps a
state to one Airtable destination (base + table). A state can appear in
multiple entries when there are multiple downstream Airtable bases (primary
plus coalition partners, for example). The `default_field_map` translates
BigQuery view columns to Airtable column names; per-sync `field_map:` blocks
override the default for individual bases.

**Per-state failure isolation:** a PTV pull failing for one state does not
abort the run. Failed states are logged and skipped at the Airtable stage.
The script exits non-zero if any state or sync target failed.

**Idempotency:** rerunning on the same day replaces today's partition rows
for the targeted states before re-inserting. The current-state view dedupes
exact-duplicate rows defensively.

## Project Structure

```
ep-syncs/
├── .claude/                    # Claude Code configuration
├── .env.example                # Credential template (copy to .env, never commit .env)
├── README.md
├── requirements.txt
├── config/
│   └── syncs.yaml              # PTV-state -> Airtable-base mappings
└── sync_shift_volunteers.py    # PTV -> BQ -> Airtable sync
```

# Scheduled Scripts — EP Syncs

*Last verified: 2026-05-02*

Source-of-truth for what's scheduled in Civis from this repo. The
shell script bodies live alongside this file — paste their contents
into Civis when creating or updating a Container Script job. Update
this doc when a job is created, renamed, rescheduled, or retired.

## Scripts

### sync_shift_volunteers.sh

- **Source script:** `civis/sync_shift_volunteers.sh`
- **Runs:** `app/sync_shift_volunteers.py`
- **Type:** Civis Container Script
- **Civis job name:** _(fill in once created)_
- **Schedule:** _(fill in once created)_
- **APIs touched:** PTV (no documented rate limit), BigQuery, Airtable (~5 req/s/base)
- **Description:** For each entry in `config/syncs.yaml`, pulls
  `shift_volunteers_csv` from PTV, appends a daily snapshot to
  `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers` (date-partitioned
  on `as_of_date`), then upserts a per-volunteer summary into the
  target Airtable base on email. Per-state and per-sync failures are
  isolated; exit code is non-zero if any state or sync target failed.

#### Civis configuration

| Field | Value |
|---|---|
| Source repo | `common-cause/ep-syncs` |
| Branch | `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | contents of `civis/sync_shift_volunteers.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field
  (already exists in Civis from other CC sync work)
- `AIRTABLE_API_KEY` — Airtable PAT in password field. Must have base
  access to every base referenced in `config/syncs.yaml` and at least
  `data.records:read` and `data.records:write` scopes.
- `PTV_API_KEY` — PTV API key in password field. Username field can
  hold `colab` (the standard PTV API username) but isn't read by the
  connector — only the password is used.

#### Scheduling notes

- Daily during off-season is fine.
- Mid-October through Election Day: hourly during voting hours is
  supported (sync is idempotent on rerun). Caveat: the BQ raw partition
  key is `as_of_date` (DATE), so multiple intra-day runs collapse into
  the same partition value — Airtable stays fresh but you can't
  reconstruct intra-day raw history. If that matters, add an
  `as_of_time` TIMESTAMP column to the raw table before going hourly.

#### Failure mode

- Script exits non-zero if any state or sync target failed.
- Logs are visible in Civis live-log view; full log on failure.
- Streaming-buffer DML warnings on same-window reruns (~30-90 min)
  are expected and benign — the view's `SELECT DISTINCT` step dedupes
  raw rows. They disappear once schedule gaps exceed the buffer window.

#### Adding new sync targets

Edit `config/syncs.yaml`, commit and push to `main`. Civis pulls fresh
on each run — no Civis-side change needed. If a new base has different
column names than the default field map, add a per-sync `field_map:`
block. Sending an Airtable column name that doesn't exist on the
destination table returns `422 UNKNOWN_FIELD_NAME` and skips that
sync (others continue).

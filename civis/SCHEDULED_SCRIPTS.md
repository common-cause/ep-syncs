# Scheduled Scripts — EP Syncs

*Last verified: 2026-05-02*

## Scripts

### sync_shift_volunteers.py

- **Type:** Individual (cron — see Civis for current cadence)
- **Civis job name:** _(to be filled in once created — e.g. "EP Sync — Shift Volunteers")_
- **APIs:** PTV (no documented rate limit), BigQuery, Airtable (~5 req/s/base)
- **Description:** For each entry in `config/syncs.yaml`, pulls
  `shift_volunteers_csv` from PTV, appends a daily snapshot to
  `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers` (date-partitioned by
  `as_of_date`), then upserts a per-volunteer summary into the target
  Airtable base on email. Per-state and per-sync failures are isolated;
  exit code is non-zero if any state or sync target failed.

#### Civis setup checklist

Create as a **Container Script** (not a Python script — we use shell entry).

1. **Source code**
   - GitHub repo: `common-cause/ep-syncs`
   - Branch: `main`

2. **Docker image**
   - `civisanalytics/datascience-python:latest`

3. **Credentials to attach**
   - `BIGQUERY_CREDENTIALS` — service account JSON in password field
     (this credential should already exist in Civis from other CC sync work)
   - `AIRTABLE_API_KEY` — Airtable PAT in password field. Must have base
     access to every base referenced in `config/syncs.yaml` and at least
     `data.records:read` and `data.records:write` scopes.
   - `PTV_API_KEY` — PTV API key in password field. Username field can
     hold `colab` (the standard PTV API username) but it isn't read by
     the connector — only the password is used.

4. **Script body** (paste into the command field — well under the 2048-char limit):

   ```bash
   pip install git+https://github.com/common-cause/ccef_connections.git
   python app/sync_shift_volunteers.py
   ```

   Note: the Civis docker image already has `python-dotenv`, `pyyaml`,
   `requests`, `tenacity`, `pandas` pre-installed. Installing
   ccef-connections pulls in `google-cloud-bigquery` and
   `pyairtable` as transitive dependencies.

5. **Schedule**
   - Daily during off-season is fine.
   - Mid-October through Election Day: hourly during voting hours is
     supported (architecture is idempotent on rerun). Caveat: the BQ
     raw partition key is `as_of_date` (DATE), so multiple intra-day
     runs collapse into the same partition value — Airtable stays fresh
     but you can't reconstruct "what was the dropdown at 11am vs 2pm"
     from raw. If that matters, add an `as_of_time` TIMESTAMP column
     to the raw table before going hourly.

6. **Failure mode**
   - Script exits non-zero if any state or sync target failed.
   - Logs are visible in Civis live-log view; full log on failure.
   - Streaming-buffer DML warnings on same-window reruns (~30-90 min)
     are expected and benign — the view's `SELECT DISTINCT` step
     dedupes raw rows. They'll disappear once the schedule has gaps
     longer than the buffer window.

#### Adding new sync targets

Edit `config/syncs.yaml`, commit and push to `main`. Civis pulls fresh
on each run — no Civis-side change needed.

If a new base has different column names than the default field map,
add a per-sync `field_map:` block. Sending an Airtable column name
that doesn't exist on the destination table returns
`422 UNKNOWN_FIELD_NAME` and skips that sync (other syncs continue).

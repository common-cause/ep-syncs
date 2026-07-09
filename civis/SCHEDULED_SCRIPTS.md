# Scheduled Scripts — EP Syncs

*Last verified: 2026-06-04 (shift sync). All-volunteers sync added + scheduled
in Civis 2026-07-02.*

Source-of-truth for what's scheduled in Civis from this repo. Jobs are
**GitHub-backed**: the Civis job attaches this repo (branch `main`),
clones it into `app/`, and the job body is just a stub
(`bash app/civis/<script>.sh`). The version-controlled `.sh` files
alongside this doc are the real job bodies — edit and push to change
what runs in Civis; never edit script bodies in the Civis UI. Update
this doc when a job is created, renamed, rescheduled, or retired.

## Scripts

### sync_shift_volunteers.sh

- **Source script:** `civis/sync_shift_volunteers.sh`
- **Runs:** `app/sync_shift_volunteers.py`
- **Type:** Individual (Daily at 6:00 AM ET)
- **Civis job name:** EP Shift Volunteer Sync
- **Schedule:** Daily at 6:00 AM ET (Civis Container Script)
- **APIs:** PTV (no documented rate limit), BigQuery (read/write), Airtable (~5 req/s/base)
- **Description:** For each enabled row in
  `proj-tmc-mem-com.ep.shift_volunteer_sync_targets`, pulls
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
| Command | `bash app/civis/sync_shift_volunteers.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field
  (already exists in Civis from other CC sync work). The service
  account must have read access to both `ptv_raw_2026` (raw table +
  view) and `ep` (sync targets registry).
- `AIRTABLE_API_KEY` — Airtable PAT in password field. Must have base
  access to every base referenced in `ep.shift_volunteer_sync_targets`
  and at least `data.records:read` and `data.records:write` scopes.
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
- **Destination duplicate keys are skipped, not fatal.** When the
  destination Airtable table already has >1 record with the same
  email (a known consequence of the dual write paths: PTV sync +
  emergency self-add form), the sync logs a warning naming the
  affected emails and upserts the rest of the batch normally.
  Sync still exits 0 on success.

#### Open follow-ups

- **Auto-resolve destination duplicates.** Today the sync skips
  records whose email already matches >1 record in Airtable, leaving
  the cleanup to humans. Worth considering: a step that detects these
  cases and either merges (keep the older `Unique ID Column`, copy
  any not-yet-on-the-keeper fields off the dupe, then delete the
  dupe) or surfaces them to a "duplicates to resolve" list (BQ table
  or Slack ping). The dupe pattern is structural — it'll keep
  happening as more states come online and more volunteers self-add
  before showing up in PTV — so doing this systematically beats
  manual cleanup over time. Punt for now; revisit if dupe volume
  grows or if a coalition partner has stricter cleanliness needs
  than NE.

#### Adding new sync targets

Sync targets live in `proj-tmc-mem-com.ep.shift_volunteer_sync_targets`
and are written by `ep-airtable-utilities` as the final step of taking
a new base live. The Civis job picks them up on the next scheduled run
— no Civis-side or repo-side change needed.

Schema and the registration contract are documented in
`bq/shift_volunteer_sync_targets.sql` and in the spec sent to
`ep-airtable-utilities`
(`ep-syncs__shift-volunteer-sync-registration-spec.md`).

Manual additions in a pinch (one-offs while the registration helper
isn't built yet):

```sql
INSERT INTO `proj-tmc-mem-com.ep.shift_volunteer_sync_targets`
  (name, state, base_id, table_name, field_map_overrides,
   enabled, registered_by, registered_at, updated_at, notes)
VALUES
  ('<State> <Role>',
   'XX', 'app...', 'Shifted Volunteers',
   NULL,                                     -- or JSON '{"phone_number":null,"shift_count":"Shifts"}'
   TRUE,
   'manual',
   CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(),
   NULL);
```

The default field map (BQ-col → Airtable-col) is a module constant in
`sync_shift_volunteers.py` (`DEFAULT_FIELD_MAP`). It assumes the
canonical CC "Shifted Volunteers" schema (Email, First Name, Last
Name, Phone Number, County, State). Use `field_map_overrides` to:
- add new mappings (`{"shift_count": "Shifts"}`)
- replace an existing default (`{"phone_number": "Phone"}`)
- remove a default that doesn't exist on the destination
  (`{"phone_number": null}`)

Sending an Airtable column name that doesn't exist on the destination
table returns `422 UNKNOWN_FIELD_NAME` and skips that sync (others
continue) — which is why ep-airtable-utilities is expected to validate
the field map against live Airtable schema before writing the row.

To pause a sync without removing it, set `enabled = FALSE`.

### sync_all_volunteers.sh

- **Source script:** `civis/sync_all_volunteers.sh`
- **Runs:** `app/sync_all_volunteers.py`
- **Type:** Individual (Daily at 6:30 AM ET)
- **Civis job name:** All Volunteers Sync (Civis job id 360150329)
- **Schedule:** Daily at 6:30 AM ET (Civis Container Script)
- **APIs:** PTV (no documented rate limit), BigQuery (read/write). No Airtable.
- **Description:** Pulls PTV's `users_csv` (all *registered* volunteers, not
  just those attached to shifts) for all 50 states + DC and appends a daily
  snapshot to `proj-tmc-mem-com.ptv_raw_2026.users` (date-partitioned on
  `as_of_date`, clustered by `state, email`). No Airtable leg in this phase.
  `v_users_current` exposes one cleaned row per (state, email) from each
  state's latest snapshot. Per-state failures are isolated; exit code is
  non-zero if any attempted state failed to land in BQ. Design + deferred
  Airtable notes: `docs/all_volunteers_sync_spec.md`.

#### Status (2026-07-02)

- BQ objects created (`ptv_raw_2026.users` + `v_users_current`).
- Script + entrypoint committed to the repo and pushed to `main`.
- Verified end-to-end via local run: all 51 states, 59,527 volunteers landed,
  exit 0.
- **Live in Civis:** job "All Volunteers Sync" (id 360150329), GitHub-backed,
  scheduled daily at 6:30 AM ET. First scheduled run also clears the
  smoke-test duplicate NE/PA snapshot (buffer will have cleared by then).
- Confirm failure notifications are enabled on the job (recommended — the
  shift job silently exited 1 for ~3 weeks before notifications were added).

#### Civis configuration

| Field | Value |
|---|---|
| Source repo | `common-cause/ep-syncs` |
| Branch | `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | `bash app/civis/sync_all_volunteers.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field (reuse the
  same Civis credential the shift job uses, ID 38653). Needs read/write on
  `ptv_raw_2026`.
- `PTV_API_KEY` — PTV API key in password field (reuse ID 39093). Username
  field can hold `colab` but isn't read by the connector.
- *No Airtable credential needed this phase.*

#### Scheduling notes

- Pulls all 50 states + DC every run (~51 API calls). `users_csv` returns no
  data for empty states, so new program states appear automatically with no
  config change. Scope at query time against the raw table if needed.
- Same off-season/in-season cadence tradeoff as the shift sync: the raw
  partition key is `as_of_date` (DATE), so multiple intra-day runs collapse
  into one partition value.

#### Failure mode

- Script exits non-zero if any attempted state failed to land in BQ.
- Same-day reruns pre-delete today's partition for the pulled states, then
  re-insert. Streaming-buffer DML warnings on same-window reruns (~30-90 min)
  are expected and benign — `v_users_current`'s `SELECT DISTINCT` dedupes
  exact-duplicate rows so the current view stays correct.
- Inserts are chunked (500 rows/request) because `users_csv` returns tens of
  thousands of rows — a single streaming request would exceed the payload
  limit.

### sync_volunteer_sheets.sh

- **Source script:** `civis/sync_volunteer_sheets.sh`
- **Runs:** `app/sync_volunteer_sheets.py`
- **Type:** Individual (Daily at 7:00 AM ET — after the all-volunteers sync
  lands at 6:30)
- **Civis job name:** *not created yet* — suggested "EP Volunteer Sheets Sync"
- **Schedule:** Daily at 7:00 AM ET (Civis Container Script)
- **APIs:** BigQuery (read only), Google Sheets + Drive (write; 60
  requests/min/user quota — per-call 429s are retried with backoff)
- **Description:** For each enabled row in
  `proj-tmc-mem-com.ep.volunteer_sheet_targets`, maintains one Google Sheet
  in the "2026 EP Volunteer Exports" shared-drive folder (state targets under
  `By State/`, partner source-code targets under `By Partner/`). Rewrites the
  hidden `_data` tab from the all-time PTV roster; the visible `Volunteers`
  tab mirrors it via an array formula and is never overwritten, so partner
  annotations to the right of the data block survive every refresh. Design:
  `docs/volunteer_sheets_spec.md`; registry contract:
  `bq/volunteer_sheet_targets.sql`.

#### Status (2026-07-08)

- BQ registry created and seeded (51 state targets enabled; 83 partner-code
  targets seeded from `ep_archive.source_codes external='Y'` — **needs a
  curation pass before the job is scheduled**, see the spec).
- Script + entrypoint in the repo; verified end-to-end via local runs:
  all 51 state sheets + the ACLUM partner prototype created and populated;
  rerun idempotency and partner-edit preservation tested.
- **Not yet in Civis.**

#### Civis configuration

| Field | Value |
|---|---|
| Source repo | `common-cause/ep-syncs` |
| Branch | `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | `bash app/civis/sync_volunteer_sheets.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field (reuse
  Civis credential ID 38653). Needs read on `ptv_raw_2026` and `ep`.
- `GOOGLE_SHEETS_CREDENTIALS` — the `sheets-controllers@sheets-controllers`
  service-account JSON in the password field. **May need to be created in
  Civis** (custom credential, JSON in password field, same pattern as
  BIGQUERY_CREDENTIALS); it exists locally in this project's `.env`.
- *No PTV or Airtable credential needed — this job only reads BQ.*

#### Scheduling notes

- Run after the all-volunteers sync (6:30 AM ET) so sheets reflect the
  morning's PTV snapshot; 7:00 AM ET leaves ~30 min of slack.
- Full run over ~130 targets makes ~1,500 Sheets/Drive API calls; with the
  60/min write quota a run can take 20–30 minutes. That's fine daily; don't
  schedule it more often than hourly.
- Adding a sheet = inserting an enabled registry row (see
  `bq/volunteer_sheet_targets.sql`); the job picks it up next run. No
  Civis-side or repo-side change needed.

#### Failure mode

- Script exits non-zero if any selected target failed; per-target failures
  are isolated (one bad sheet doesn't block the rest).
- Reruns are idempotent: sheets are looked up by title within their
  subfolder; `_data`/`README` are rewritten, `Volunteers` and any
  partner-added tabs/columns are left alone.
- If a partner deletes the `Volunteers!A1` mirror formula, the next run
  re-seeds it (only when A1 is empty — a `#REF!` blockage from partner data
  inside the mirror block is left for a human to resolve).
- End-of-run report logs WARNINGs for active source codes ≥25 volunteers
  that no enabled target covers — watch these for new partner codes to
  register.

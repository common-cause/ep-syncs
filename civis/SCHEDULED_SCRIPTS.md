# Scheduled Scripts — EP Syncs

*Last verified: 2026-06-04*

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
- **Type:** Civis Container Script
- **Civis job name:** _(fill in once created)_
- **Schedule:** _(fill in once created)_
- **APIs touched:** PTV (no documented rate limit), BigQuery, Airtable (~5 req/s/base)
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

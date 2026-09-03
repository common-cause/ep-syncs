# Scheduled Scripts — EP Syncs

*Last verified: 2026-08-28 (ccef-connections pins bumped to v0.12.1 on BOTH the
misc jobs runner and the volunteer sheets sync, clearing two separate
pin-drift outages — see each job's Failure mode / Status. Misc jobs confirmed
green by an ad-hoc run the same day; the sheets job's zero-volunteer targets
needed a second release, v0.12.1, and are unverified until the next run). Previously verified
2026-08-19 (`infrastructure_sheet` task registered on the misc jobs runner;
that job's `APIs:` line corrected). Volunteer sheets sync created +
scheduled in Civis 2026-08-11. Shift sync decoupled to a national pull +
Airtable bases capture created + scheduled 2026-07-23. All-volunteers sync added
+ scheduled in Civis 2026-07-02.*

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
- **APIs:** PTV (no documented rate limit; ~51 calls/run), BigQuery
  (read/write), Airtable (~5 req/s/base)
- **Description:** Pulls `shift_volunteers_csv` from PTV for **all 50
  states + DC** (`PULL_STATES`, unioned with any registry state outside
  that list) and appends a daily snapshot to
  `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers` (date-partitioned
  on `as_of_date`, inserts chunked at 500 rows/request). Then, for each
  enabled row in `proj-tmc-mem-com.ep.shift_volunteer_sync_targets`,
  upserts a per-volunteer summary into the target Airtable base on
  email — **the registry drives only this Airtable leg** (decoupled
  2026-07-23; before that the BQ pull was limited to registry states).
  Per-state and per-sync failures are isolated; exit code is non-zero
  if any attempted state or sync target failed.
- **CLI (local ops/testing):** `--states NE,PA` pulls exactly that
  subset — registry targets outside the subset are skipped *without*
  failing (deliberate subsetting isn't an error); `--bq-only` skips the
  Airtable leg entirely (doesn't need the Airtable credential).

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
  emergency self-add form, plus hand-loads via
  ep-airtable-utilities' `load_volunteers.py`), the sync logs a
  warning naming the affected emails and upserts the rest of the
  batch normally. Sync still exits 0 on success.
- **Every run appends to `proj-tmc-mem-com.ep.shift_sync_log`**
  (one row per stage/scope — see `bq/shift_sync_log.sql`). This
  exists because the skip above and a state that stops reporting
  both end in `exit 0`, so neither was visible in Civis. The
  closing log line now also names any target with skipped
  volunteers. **Monitoring query — who is silently stale:**
  ```sql
  SELECT as_of_date, scope, skipped_dupe_exact, case_variants,
         JSON_VALUE_ARRAY(skipped_keys, '$.skipped_dupe_exact') AS keys
  FROM `proj-tmc-mem-com.ep.shift_sync_log`
  WHERE stage = 'airtable_upsert'
    AND as_of_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
    AND (skipped_dupe_exact > 0 OR case_variants > 0)
  ORDER BY as_of_date DESC, scope;
  ```
  Swap the predicate for `status = 'regressed'` and
  `stage = 'ptv_pull'` to find states that stopped returning data.
- **Email case:** Airtable's `performUpsert` matches
  `fieldsToMergeOn` **case-sensitively** — verified 2026-08-19
  against a live base (with `zz.casetest@…` and `ZZ.CaseTest@…` both
  present, an upsert keyed on the lowercase spelling patched only
  the exact-case record). The duplicate guard used to count
  case-insensitively, making it stricter than the operation it
  guards and freezing any volunteer with a case-variant twin. It now
  gates the skip on exact spelling and reports case variants
  separately as `case_variants` (upserted normally, but two records
  for one human). Audited all 8 enabled targets 2026-08-19: 572
  records, 19 non-lowercase, **zero** case-variant pairs.

#### Open follow-ups

- **Resolve destination duplicates (15 groups as of 2026-08-19:
  MI 9, NE 3, PA 3).** 13 of the affected volunteers are still in
  the incoming sync set, so they are skipped on every run.
  **Do not script a delete-and-keep merge.** 14 of the 15 groups
  differ on `Field Reports` and/or `Checklist Submissions` — both
  records carry *different* linked submissions from real EP work, so
  deleting either side orphans field reports. Merging means unioning
  the link arrays into the survivor, which is native in the Airtable
  UI and belongs to whoever owns those reports. Route the list from
  the query above to the MI/NE/PA leads. Low urgency: none of the 13
  have upcoming shifts. The pattern is structural and will recur as
  more states come online.
- **`Unique ID Column` is never read or written.** No PTV user id is
  in the payload at all (`DEFAULT_FIELD_MAP` carries only
  email/first/last/phone/county/state, and the source view exposes
  no id), which is why Email carries the entire identity burden.
  Populating it would give the upsert a stable non-email key. Note
  the current behaviour is partly a *feature*: a hand-created row is
  indistinguishable from a sync-created one, so it gets adopted and
  patched rather than duplicated.

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
- **Type:** Individual (Daily at 8:15 AM ET — after the all-volunteers sync
  lands at 6:30)
- **Civis job name:** EP Volunteer Sheets Sync (Civis job id 365009753,
  created 2026-08-11)
- **Schedule:** Daily at 8:15 AM ET (Civis Container Script; 7:00 → 7:30 → 8:15 on 2026-09-03 — see the collision note below)
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

#### Status (2026-08-11)

- BQ registry created and seeded 2026-07-08; partner-code curation pass
  landed 2026-07-09 (registry now 51 state + 99 source-code targets, all
  enabled).
- Script + entrypoint in the repo; verified end-to-end via local runs:
  all 51 state sheets + the ACLUM partner prototype created and populated;
  rerun idempotency and partner-edit preservation tested.
- **Live in Civis** (2026-08-11): scheduled daily at 8:15 AM ET. Confirm
  failure notifications are enabled on the job (standing lesson — the shift
  job silently exited 1 for ~3 weeks before notifications were added).

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
  morning's PTV snapshot.
- **Moved 7:00 → 7:30 → 8:15 AM ET on 2026-09-03.** First to clear a collision
  the Airtable backfill created, then again to leave deliberate room for
  growth. Registering 44 more bases took `sync_airtable_bases.sh` from ~2
  minutes to ~25 (6:46 → 7:11 ET), and its final step regenerates the
  `ep_2026_cleaned` union views this job reads. At 7:00 this job was starting
  mid-regeneration: individual views are replaced atomically but the SET is
  not, so it could read a half-updated layer. 7:30 fixed that with no margin;
  8:15 gives the capture **~1h30m of runway against a 25-minute job**, sized
  for the general-election push where the base count is expected to
  proliferate. Nothing else is scheduled at 8:15 (checked fleet-wide).
- Full run over 173 targets makes ~1,500 Sheets/Drive API calls and takes
  **35–66 minutes** (measured 2026-08-28..09-03, not the 20–30 this line used
  to claim). That's fine daily; don't schedule it more often than hourly.

##### The morning chain, measured (2026-09-03) — read this before re-timing anything

| job | fires | observed | idle after |
|---|---|---|---|
| Shifted Volunteers | 6:00 | ~6 min | 24 min |
| All Volunteers | 6:30 | ~4 min | 12 min |
| Sync Airtable Bases | 6:45 | ~25 min | 64 min |
| Volunteer Sheets | 8:15 | 35–66 min | — |

Ordering constraints, so a future re-time doesn't break one silently:
- Airtable capture must follow the **6:00** shift sync, whose Airtable
  upserts it captures the same morning.
- Its view-regeneration step reads `ptv_raw_2026.v_users_current` for
  `shifted_volunteers.in_ptv`, so it also wants the **6:30** all-volunteers
  sync finished. Running earlier is not an error — `in_ptv` just reflects
  yesterday's roster.
- This job reads `ep_2026_cleaned.volunteers`, which includes the Airtable
  self-add branch, so it must follow the capture's regeneration. **That
  ordering is the one that actually matters.**

##### Resource ceilings — three of five jobs were CPU-throttled

Measured peaks against request, 2026-09-03. Civis' defaults are far too low
and every job here was sized by habit rather than evidence:

| job | CPU peak / request | memory peak / limit |
|---|---|---|
| Shifted Volunteers | **258 / 256m (101%)** | 483 / 1024MB |
| All Volunteers | **259 / 256m (101%)** | 502 / 1024MB |
| Sync Airtable Bases | 182 / 250m (73%) | 512 / 1000MB |
| Volunteer Sheets | **260 / 250m (104%)** | **784 / 1000MB (78%)** |
| Misc jobs runner | **201 / 250m (80%)** | 575 / 1000MB |

Raised 2026-09-03 to **cpu=1024m memory=2048MB** on Sync Airtable Bases and
Volunteer Sheets — the two jobs in the collision. Some of the Airtable job's
25 minutes was throttling, not work, so the runway above is a conservative
estimate.

**The volunteer-sheets memory number is the one to watch.** It was at 78% of
1000MB, and the driver is the **all-time roster** loaded into pandas (63,535
rows on 2026-09-03), which grows with volunteer signups regardless of how many
sheets are registered. That would have OOM-killed mid-run — worse than a fast
failure, because a killed container leaves partner sheets half-refreshed.
2048MB buys roughly a doubling of the roster.

**Still on Civis defaults and still throttled** (not changed, since they were
outside the collision): Shifted Volunteers, All Volunteers, and the misc jobs
runner. Both PTV jobs are pinned at ~100% CPU, so their 6- and 4-minute
runtimes are floors imposed by throttling.

**Where this stops scaling.** Both growth-path jobs are single-threaded with a
fixed per-item cost — ~16s per Airtable table, ~20s per sheet target. The
schedule and resource headroom above absorb roughly one doubling. Past that,
the fix is structural (parallelism, or incremental capture that skips
unchanged bases), not another schedule shuffle.

**Image tag drift, noted not fixed:** this job, Shifted Volunteers and All
Volunteers run `datascience-python:latest`; Sync Airtable Bases and the misc
runner are pinned to `8.5.0`. An unpinned image on a production job can change
under you.
- Adding a sheet = inserting an enabled registry row (see
  `bq/volunteer_sheet_targets.sql`); the job picks it up next run. No
  Civis-side or repo-side change needed.

#### Failure mode

- Script exits non-zero if any selected target failed; per-target failures
  are isolated (one bad sheet doesn't block the rest).
- **THE DOMINANT FAILURE MODE IS A TRANSIENT 503, NOT QUOTA — and the retry
  path is built for quota.** Diagnosed 2026-09-03: 5 of the last 8 runs
  failed, and the error is
  `APIError: [503]: The service is currently unavailable.`, not a 429.
  Why it bites:
  - `retry_google_operation` in ccef-connections retries **429 only**
    (`_is_google_rate_limit`), and its own docstring notes gspread has no
    retry of its own. So a 503 gets **zero** connector-level retries.
  - It falls through to this script's 2-attempt loop, which waits a fixed
    `QUOTA_COOLDOWN_SECONDS = 65` — a quota-window wait applied to a
    transient server error that typically clears in seconds — and then
    gives up, failing the target and the whole job.
  - 173 targets × ~4 calls is ~700 calls/run, so even a fraction-of-a-percent
    503 rate reliably produces 1–2 failed targets a night. That matches the
    observed pattern exactly (`failed=['promotethevote','NV']` on 09-03,
    `NH` on 08-28).
  Confirmed transient: re-running just those two targets locally the same
  morning exited **0**, with NV succeeding on the cooldown retry. **No
  partner data was ever wrong** — the sheet just kept yesterday's rows for a
  day. The cost is the alarm: a red job every other morning means a real
  failure looks identical. Same lesson as the two pin outages.
  **The fix belongs in ccef-connections**, not here: `_is_google_rate_limit`
  should also cover transient 5xx (500/502/503/504) so the existing
  exponential backoff handles them, since every Sheets consumer in the fleet
  inherits this gap. Then this script's 65s loop goes back to being a
  genuine last resort.
- **A target with zero volunteers used to fail to provision** (fixed in
  ccef-connections **v0.12.1**, pinned here 2026-08-28). The `_data` tab holds
  only its header, `write_worksheet` sizes the grid to exactly one row, and
  Sheets refuses to freeze every visible row. This is the *normal* state of a
  target registered the moment its source code is issued, before any
  volunteer uses it — which is the whole point of registering it early. The
  15 partner targets added 2026-08-24 hit it and took the job red 08-25..
  08-28 while the other 158 targets updated fine, so nobody lost data but the
  exit-1 signal was worthless for four days. If this recurs, check the pin
  before you touch the registry.
  **It took two releases, and v0.12.0 alone is worse than useless here.**
  A header-only tab is a fight between two operations that both insist on the
  same row: `format_header_row` freezes row 1, `write_worksheet` resizes the
  grid to exactly `len(data)`. v0.12.0 fixed only the first (grow to 2, then
  freeze), so the sheet provisioned once and then failed on *every* later run,
  because the next write tried to shrink a frozen row 1 back to a 1-row grid.
  All 15 failed again on the 08-28 verification run. v0.12.1 fixes the other
  half. **Never pin this job to v0.12.0.**
- Reruns are idempotent: sheets are looked up by title within their
  subfolder; `_data`/`README` are rewritten, `Volunteers` and any
  partner-added tabs/columns are left alone.
- If a partner deletes the `Volunteers!A1` mirror formula, the next run
  re-seeds it (only when A1 is empty — a `#REF!` blockage from partner data
  inside the mirror block is left for a human to resolve).
- End-of-run report logs WARNINGs for active source codes ≥25 volunteers
  that no enabled target covers — watch these for new partner codes to
  register.

### sync_airtable_bases.sh

- **Source script:** `civis/sync_airtable_bases.sh`
- **Runs:** `app/sync_airtable_bases.py`
- **Type:** Individual (Daily at 6:45 AM ET)
- **Civis job name:** EP Airtable Bases Sync (Civis job id 362699252,
  created 2026-07-23)
- **Schedule:** Daily at 6:45 AM ET (Civis Container Script)
- **APIs:** Airtable (read only — records + metadata; ~5 req/s/base,
  sequential, pyairtable retries 429s), BigQuery (read `ep`, write
  `ep_2026_raw`). No PTV.
- **Description:** For each enabled row in
  `proj-tmc-mem-com.ep.airtable_sync_sources`, discovers every table in
  the Airtable base (minus `exclude_tables`) and captures it into
  `ep_2026_raw`: TYPED per-(base, table) tables rebuilt each run
  (`{prefix}__{table}`, full-replace via load job — schema-drift-proof,
  columns/types re-derived from Airtable field metadata every run), plus
  one JSON-payload row per record appended to
  `ep_2026_raw.airtable_records_history` (as_of_date-partitioned audit
  trail). READ-ONLY toward Airtable — never writes or deletes records.
  Per-base and per-table failures isolated; exit non-zero on any failure.
  Design: `docs/airtable_bases_sync_spec.md`; registry contract:
  `bq/airtable_sync_sources.sql`.
- **Schedule rationale:** the shift sync's Airtable-upsert leg runs at
  6:00 and finishes within minutes; capturing at 6:45 means the day's
  snapshot of every "Shifted Volunteers" table INCLUDES that morning's
  upserts, so the `ep_2026_cleaned` interface layer sees a coherent
  morning state across `ptv_raw_2026` (landed 6:00/6:30) and
  `ep_2026_raw`. No API contention with the 6:30 all-vols job
  (different APIs).

#### Status (2026-07-23)

- BQ objects created (`ep.airtable_sync_sources` registry seeded with 14
  bases — 6 field-report + 8 quiz, all PAT-verified via `--check-access`
  and enabled; `ep_2026_raw.airtable_records_history`).
- Script + entrypoint in the repo. Verified end-to-end via local runs:
  full run captured 14/14 bases, 34 tables, 2,335 rows, exit 0;
  `JSON_VALUE` extraction on the history JSON column confirmed working;
  same-day rerun idempotency confirmed (ROW_NUMBER dedupe recipe returns
  exactly one row per record).
- **Live in Civis** (2026-07-23): job created, first run succeeded,
  scheduled daily at 6:45 AM ET. Confirm failure notifications are
  enabled on the job (standing lesson from the shift job).

#### Civis configuration

| Field | Value |
|---|---|
| Source repo | `common-cause/ep-syncs` |
| Branch | `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | `bash app/civis/sync_airtable_bases.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field (reuse
  Civis credential ID 38653). Needs read on `ep` (registry) and
  read/write on `ep_2026_raw` — **verify the `com-dbt@` SA's grant on
  `ep_2026_raw` before the first scheduled run** (prior grants were
  scoped to `ptv_raw_2026`/`ep`; local verification ran as the same SA,
  so if local runs worked this is already in place).
- `AIRTABLE_API_KEY` — Airtable PAT in password field (reuse ID 38226,
  the "sync operations" token). Needs `schema.bases:read` +
  `data.records:read` and per-base access to every registered base
  (validated at registration; `--check-access` re-verifies).
- *No PTV credential needed.*
- **Enable failure notifications at creation time** (the shift job
  silently exited 1 for ~3 weeks before notifications were added).

#### Scheduling notes

- Daily at 6:45 AM ET. Runtime is minutes (14 bases, sequential; ~1
  request per 100 records per table + 1 schema call per base).
- Adding a base = inserting a registry row (see
  `bq/airtable_sync_sources.sql` for the contract — insert disabled,
  `--check-access`, review `--list`, enable). No Civis-side or repo-side
  change. ep-airtable-utilities does this at base go-live.
- In-season cadence can increase like the other jobs; the history
  partition key is `as_of_date` (DATE), so intra-day runs collapse into
  one partition value (the ROW_NUMBER dedupe recipe handles it — reads
  stay correct, you just can't reconstruct intra-day history).

#### Failure mode

- Per-base and per-table failures are isolated; exit non-zero if any
  enabled base or table failed.
- Streaming-buffer DML warnings on the history pre-delete during
  same-window reruns (~30-90 min) are expected and benign — readers
  dedupe via the ROW_NUMBER recipe in `bq/airtable_records_history.sql`.
- A 403 on one base (PAT lost access) fails that base only; fix the PAT
  or set the row `enabled = FALSE` with a `notes` explaining why.
- **Renaming an Airtable table orphans its old typed table** (the sync
  lands the new name next run; the old `{prefix}__{old_name}` table
  stays frozen). Drop the orphan manually. Same applies to changing a
  row's `bq_table_prefix`.
- An Airtable field-type change changes the BQ column type on the next
  run (by design — full replace). `ep_2026_cleaned` views CAST
  defensively; the history JSON is the recovery path for anything lost.

### run_misc_jobs.sh

- **Source script:** `civis/run_misc_jobs.sh`
- **Runs:** `app/run_misc_jobs.py` (no args)
- **Type:** Individual (Nightly, ~3:00 AM ET)
- **Civis job name:** "EP Misc Sync Jobs" (Civis job id 361625051)
- **Schedule:** Nightly at 3:00 AM ET (Civis Container Script)
- **APIs:** BigQuery (**read/write** — `asana_raw_2026`, `ep_2026_raw`, `ep`),
  Asana (read only), Google Sheets (**read only**), Airtable (**read only** —
  metadata `list_bases` call only, no record reads). No Drive writes.
  *(Corrected 2026-08-19: this line read "BigQuery (read only), Google Sheets +
  Drive (write)", which described the retired FL-signups task, not this runner.
  Both halves had inverted since. This file is machine-parsed by the
  meta-project's cross-project schedule rollup, whose API-conflict detection
  depends on it — re-check this line whenever the task set changes.)*
- **Description:** Shared runner for small, periodic exports that don't each
  warrant their own Civis job. **One nightly job**; on each run the runner
  reads `misc_jobs_schedule.yaml` and executes only the tasks scheduled for
  tonight's weekday **in US/Eastern**. Task *identity* (the `run()` callable)
  lives in a module under `misc_jobs/`, registered in `JOBS` in
  `run_misc_jobs.py`; task *timing* lives in the YAML, keyed by job key.
  Per-task failures are isolated and the runner exits non-zero if any selected
  task failed. **Add a scheduled export by committing a task module + a `JOBS`
  row + a YAML entry — no Civis-side change. Re-time an existing task by
  editing the YAML and pushing.**

  Because the job fires at 3 AM ET, a task belongs to the ET calendar day it
  fires on — so a "Sunday night" task is scheduled on `mon` (the Monday 3 AM
  ET run). This is deliberate: it lets one nightly job cover every day-of-week
  pattern from a single config, instead of one Civis job per cadence.

#### Registered tasks

| Key | Scheduled (see YAML) | What it does |
|---|---|---|
| `asana_ep_kanban` | `daily` | Snapshots every enabled board in `ep.asana_sync_sources` (today: the NM `EP Volunteer Onboarding Kanban`) into `asana_raw_2026.ep_kanban_tasks` + `asana_raw_2026.projects`, partitioned by `as_of_date`. READ-ONLY toward Asana. Daily grain is load-bearing: Asana exposes only a task's CURRENT section, so a skipped night permanently loses that day's stage-transition evidence — do not park this task casually. Feeds `ep_2026_cleaned.asana_pipeline` + the `source_system='asana'` branch of `volunteers`. Design: `docs/asana_nm_sync_spec.md`. |
| `infrastructure_sheet` | `daily` | Melts the two Infrastructure tabs of the 50-State EP Coalition Plan spreadsheet (`Infrastructure (General)` / `(Primary)`) into `ep_2026_raw.coalition_plan_infrastructure` — one row per (state, sheet column), cell text verbatim, ~1,632 rows/night, partitioned by `as_of_date` (stamped in **ET**, matching the runner's weekday convention). READ-ONLY toward the spreadsheet: program staff own it. Same-day reruns replace the day's partition via a load job (no streaming buffer, so a Civis retry is always clean). Parsed by `ep_2026_cleaned.coalition_plan_infrastructure`; consumed by ep-dashboards' infrastructure marts + Hex pages. Fails loudly on a missing/renamed tab or a row-3 layout change, and logs the header row every run so a column rename is visible the morning it happens. Design: `docs/coalition_plan_infrastructure_sync_spec.md`. |
| `partner_source_codes` | `daily` | Snapshots **both** tabs of the 2026 EP Partner Engagement Form (`Form Responses 1` + `Source Codes`) into `ep_2026_raw.partner_source_codes`, ~161 rows/night, partitioned by `as_of_date`. The only capture of the **issuing** side of a source code — every other view of a code is downstream of a volunteer *using* one, so an issued-but-unused code, or one whose spelling drifted between issuance and capture, is invisible everywhere else. READ-ONLY toward the form; deliberately narrow on PII (org labels only, no requester names/emails). Reconciled by `ep_2026_cleaned.source_code_resolution`. Registered 2026-08-26 (`9cef07e`); confirmed running 2026-08-28. Design: `docs/volunteer_sheets_spec.md` §4.1. |
| `hub_host_tracker` | `daily` | Refreshes the `Volunteer Landing Page` of every enabled tracker in `ep.hub_host_trackers` (today: MI's `EP Hub Host Tracker`) from that state's quiz bases — one row per volunteer who submitted any registered quiz, joined to `ep_2026_cleaned.volunteers` for phone/county and to all-time `ptv_raw_2026.shift_volunteers` for the `Ever Shifted?` latch. Writes exactly one tab (hidden `_data`) plus a README; the visible page is an array-formula mirror of it, and the human-owned `Assigned Host` dropdown sits outside the mirrored block, so nothing a staffer typed is ever clobbered and no keyed per-column update is needed. Rows are never removed or reordered — column A of `_data` is an append-only ledger read back each run, so a deleted Airtable quiz record is carried forward and flagged rather than shifting every host assignment below it up by one. Per-host tabs are hand-cloned from `TEMPLATE` and pull their distribution list with a single `FILTER`; those formulas are installed only by `--install-scaffolding`, never nightly, because that tab's kit checklist is program-staff content. READ-ONLY toward Airtable and toward the `Hosts` tab. Design: `docs/hub_host_tracker_spec.md`. |

| `airtable_base_visibility` | `daily` | Records every Airtable base the sync-operations PAT can currently see into `ep.airtable_base_visibility` (one row per base, forever; first/last sighting + a triage verdict column the sweep never touches). One metadata call, ~192 MERGE rows/night. Exists because capture registration is only automatic for bases built through ep-airtable-utilities' spec system — its audit walks specs→registry and is structurally blind to a base with no spec, which is most of them. Worse, PAT access is *granted* by people who have no idea capture is a separate step, so a grant is silent (Amy's FL base became readable the week of 2026-09-01 and nothing noticed). **Not a discovery mechanism** — `list_bases()` returns only what has been granted; the human canvass finds bases, this catches them once granted. Reads `ep.v_airtable_base_triage_queue` for the log line. Judgment about what turns up is deliberately NOT here — see the `airtable-base-triage` dispatch task type in `.claude/dispatch.yaml`. Registered 2026-09-02. |

**Retired tasks:** `event_975203_signups` (Mobilize event 975203 FL-training
signup roster → Google Sheet for FL program, ran `mon` Jul 14 – Aug 18 2026;
retired 2026-08-18 after the series ended 2026-08-16 with the final roster
written — 1,288 signups / 894 people. Module kept in `misc_jobs/` as the
reference example of a task module; only the `JOBS` row + YAML entry were
removed, per the retire contract).

#### Status (2026-08-19)

- **`infrastructure_sheet` registered 2026-08-19, LIVE on Civis 2026-08-20**
  (assignment from ep-dashboards, which had been running the same melt from a
  laptop Task Scheduler job into `ep_dashboards.infrastructure_raw`). Verified
  locally before commit: 1,632 rows landed, same-day rerun replaced them
  cleanly, and the cleaned view is row-for-row identical to the ep-dashboards
  dbt model it replaces. **Needed no new credential and no pin bump** — but the
  entrypoint's extras gained `pandas` (it is *not* part of the `bigquery` extra,
  and this task loads via `load_dataframe`).
  **First green run: ad-hoc, 2026-08-20 16:20:34 UTC** (the push landed after
  that morning's 3 AM ET fire). 1,632 rows, and exactly **one distinct
  `synced_at` in the partition** — a local test run had written the same
  partition hours earlier, so this also proves the pre-delete works on the real
  container, not just locally. ep-dashboards pinged; they keep their local step
  until they re-point `sources.yml` (different destination table, so the two
  cannot collide).
- **`hub_host_tracker` registered 2026-08-21. LIVE — first green Civis run
  2026-08-28** (ad-hoc, run 857135415: `ok=4`, MI `40 kept + 0 new`).
  ⚠️ It was registered on the claim that it **"needs no new credential, no pin
  bump and no entrypoint change"** — the credentials and extras were indeed
  already covered, but **the pin claim was wrong and broke every nightly run
  2026-08-25..08-28**: it calls `SheetsWriterConnector.open_spreadsheet`, which
  did not exist until ccef-connections v0.8.0, against a v0.7.1 pin. Import-time
  coverage is not call-time coverage. Pin is now v0.12.1. Verified locally before commit: 40 MI volunteers from the
  two MI quiz bases landed on the landing page, a second run reported
  `40 kept + 0 new` (ledger stable), the scaffolding pass is idempotent, and
  the host `FILTER` was exercised end to end — two volunteers assigned to
  Kevin Fisher appeared on his tab with the right seven columns, including a
  deliberately case- and whitespace-mangled `"kevin fisher "` (test assignments
  reverted). Ledger carry-forward, ordering and duplicate-key collapse are
  covered by direct tests of `merge_with_ledger`. Awaiting its first green
  Civis run.
  **Before registering another state:** the `sheets-controllers@` SA must be a
  writer on that spreadsheet — unlike the volunteer-export sheets, this job
  does NOT create the file, and program staff own it.
- **Incident 2026-08-25 → 08-28:** every nightly run exited 1 because
  `hub_host_tracker` called `SheetsWriterConnector.open_spreadsheet`, added in
  ccef-connections **v0.8.0**, while this entrypoint was still pinned to
  **v0.7.1**. Unlike the Asana incident below this one failed at *call* time,
  not import, so per-task isolation held and the other three tasks landed
  normally every night — only MI's landing page went stale (08-24 → 08-28,
  recoverable: the next run rebuilds it from Airtable + BQ). Fixed 2026-08-28
  by cutting ccef-connections **v0.12.0** and bumping the pin.
  **The lesson the Asana incident did not teach:** the task was verified
  locally at go-live and worked, because the editable local install is always
  newer than the pin. Local success is evidence about your laptop, not about
  the container. Check the *tag* for every method a task calls, not just for
  the connectors it imports.
- **Incident 2026-07-30 → 08-17:** every nightly run failed at import
  (`ImportError: cannot import name 'AsanaConnector'`) because the
  `asana_ep_kanban` task was registered 2026-07-29 without bumping the
  entrypoint's ccef-connections pin (v0.2.0 predates `AsanaConnector`, which
  shipped in v0.3.0). Fixed 2026-08-18: pin bumped to v0.7.1 and the
  `ASANA_API_KEY` credential added to the attach list above. Cost: the
  2026-07-30..08-17 Asana daily snapshots are permanently unrecoverable
  (Asana exposes only current section state), and the FL signups sheet missed
  its Aug 3 / 10 / 17 refreshes (recoverable — rebuilt from BQ on the next
  run).
- Runner (nightly + YAML day-matcher), `misc_jobs/` package, schedule file, and
  entrypoint in the repo (pushed to `main` 2026-07-14).
- Verified end-to-end via local run: `event_975203_signups` refreshed its
  sheet (240 signups, 232 active, 8 cancelled, 167 people), exit 0. Confirmed
  the `sheets-controllers@` SA has write access to the destination folder.
  Day-matching verified (`mon` selects the task; other nights are a clean
  exit-0 no-op).
- **Live in Civis:** job "EP Misc Sync Jobs" (id 361625051), GitHub-backed,
  scheduled nightly at 3:00 AM ET. The FL signups task fires on the Monday
  run (`mon` in the schedule); other nights are a no-op until more tasks are
  registered.
- Confirm failure notifications are enabled on the job (recommended — the
  shift job silently exited 1 for ~3 weeks before notifications were added).
- Sanity note on the schedule timezone: the runner derives the run night from
  the ET wall-clock weekday, so this only lines up with "Sunday night" if the
  Civis 3:00 AM schedule is ET (as the other EP jobs are). If it were UTC
  (~11 PM ET the prior evening) the task would land Monday night instead —
  still weekly, just shifted.

#### Civis configuration

| Field | Value |
|---|---|
| Source repo | `common-cause/ep-syncs` |
| Branch | `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | `bash app/civis/run_misc_jobs.sh` |

#### Credentials to attach

- `BIGQUERY_CREDENTIALS` — service account JSON in password field. Needs
  read/write on the datasets registered tasks land in (today: `asana_raw_2026`,
  `ep_2026_raw`) and **read/write on `ep`** — `airtable_base_visibility` writes
  a registry-dataset table, which the earlier "read on `ep`" note did not cover.
  *The live job is wired to credential **39428** (`BQ com-dbt`), not the 38653
  this line used to name. A key rotation broke 39428 overnight 2026-09-01/02 and
  turned the whole Civis board red; every task in this job failed 2026-09-02
  03:00 ET with `Failed to parse JSON credential BIGQUERY_CREDENTIALS_PASSWORD`.
  Fixed same day and confirmed green by a manual re-run (858535496).*
- `GOOGLE_SHEETS_CREDENTIALS` — the `sheets-controllers@sheets-controllers`
  service-account JSON in the password field (same credential the volunteer
  sheets job uses). The SA must be able to reach every spreadsheet or Drive
  folder a task targets. Currently read-only use: `infrastructure_sheet` reads
  the 50-State EP Coalition Plan workbook (access confirmed 2026-08-19 by a
  local run under this same SA).
- `ASANA_API_KEY` — Asana PAT in the password field (exposes
  `ASANA_API_KEY_PASSWORD`, which `asana_ep_kanban` reads). Currently Rob's
  personal PAT (sees the whole `commoncause.org` workspace); a dedicated PAT is
  an open question in `docs/asana_nm_sync_spec.md` §7.
- `AIRTABLE_API_KEY` — the sync-operations PAT (Civis credential **39288**,
  "Airtable Sync Operations PAT 2026-06" — the same one the Airtable bases sync
  job uses). Exposes `AIRTABLE_API_KEY_PASSWORD`, read by
  `airtable_base_visibility`. **NOT YET ATTACHED as of 2026-09-02 — this is a
  hard prerequisite for pushing that task.** `civis_tune` cannot attach
  credentials; it is a Civis UI action. Without it the task fails at 3 AM,
  and because the runner exits non-zero when any selected task fails, the whole
  job goes red and emails on failure even though the other four tasks succeeded.
  The `airtable` pip extra is the matching half and is already in
  `civis/run_misc_jobs.sh` — the ccef-connections PIN (v0.12.1) was never the
  problem here, `list_bases` has existed since v0.5.0; this job had simply never
  installed pyairtable.
- *No PTV or Airtable credential needed for the current task set.*

**Registering a new task means re-checking this list**, plus the entrypoint's
`pip install` extras. A task's credential has to be attached to the Civis job,
the entrypoint's ccef-connections pin has to include the connector it imports,
**and the extras have to cover its Python dependencies** — "no Civis change
needed" is only true when all three already hold. (The Asana task was registered
2026-07-29 without the first two; every nightly run failed at import until
2026-08-18. `infrastructure_sheet` needed the third: `pandas` is its own extra,
not part of `bigquery`.) Import-time dependencies are the dangerous kind — the
runner imports every task module at startup, so one missing package takes down
every task, not just its own. New tasks should import optional heavy deps inside
their functions.

#### Scheduling notes

- Set the Civis schedule to **nightly, ~3:00 AM ET** and leave it there; day
  selection is the runner's job, not Civis's. The 3 AM slot sits well clear of
  the UTC date boundary, so tonight's ET weekday is unambiguous, and there's no
  upstream-freshness dependency tighter than the ~daily `mobilize_cleaned`
  mirror refresh.
- **Which tasks run which nights is `misc_jobs_schedule.yaml`, not Civis.** Edit
  the YAML and push; no Civis change. `days:` takes weekday abbreviations
  (`mon`..`sun`) or `daily`. Set `enabled: false` to park a task.
- Nights with nothing scheduled are a clean exit-0 no-op — a nightly job that
  mostly does nothing is expected and cheap.
- Enable failure notifications on the job (the shift job silently exited 1 for
  ~3 weeks before notifications were added).
- Local dry-run of any night: `python run_misc_jobs.py --as-of mon`
  (still executes the selected tasks); `--list` shows each task's schedule
  without running anything.

#### Failure mode

- Per-task failures are isolated (one failing task doesn't block the others);
  the runner exits non-zero if any selected task raised, so Civis flags it.
- Nothing scheduled for tonight → the runner logs "nothing to do" and exits 0.
  A malformed schedule file (bad weekday token, unreadable YAML) exits 1 before
  running anything. `--only <bad-key>` exits 1.
- A `JOBS` task with no YAML entry logs a warning and never runs (so a
  forgotten schedule entry is visible in the logs); a YAML entry with no
  matching `JOBS` task logs a warning and is ignored.

#### Target-sheet edits (filters / sorts)

*No currently registered task writes to a Google Sheet* — `infrastructure_sheet`
only reads one, and the sheet-writing FL task retired 2026-08-18. This section
is the contract for the next task that does write one.

Each such task **fully owns** its sheet's job-managed tabs: every run clears and
rewrites them wholesale from BigQuery. The job never reads the sheet back, so
a user filtering or sorting the data view cannot misalign or corrupt anything
(unlike the volunteer sheets job, which preserves partner columns and so
forbids sorting). Consequences to know:
- A **basic filter's** hide criteria and any **sort** applied through it do not
  survive a refresh in a useful way: the rewrite restores canonical row order
  and re-applies stale criteria to fresh rows. No data is lost.
- **Filter views** (the personal, non-destructive kind) persist and are the
  safe way to slice the data.
- Notes typed **inside** the Read me / Signups / By session tabs are
  overwritten every run; the README tells users to annotate on a separate tab.

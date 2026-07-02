# All-Volunteers Sync — Draft Spec

*Drafted 2026-07-02. Status: **built + verified locally** (2026-07-02). Only
remaining step is creating the Civis job (§5) — Rob.*
*Scope: **PTV → BigQuery only** for now. Airtable is deferred (see §7).*

A second EP sync, sibling to `sync_shift_volunteers.py`, that pulls PTV's
**`users_csv`** endpoint (all *registered* volunteers, not just those attached
to shifts) and lands a daily snapshot in BigQuery. No Airtable stage in this
phase — this just gets the full volunteer roster into BQ where it's queryable.

This is the sync the 2024 `ep_all_vols` dataset was a first cut at; that build
also fed Airtable, but we're deferring that half.

---

## 1. What "all volunteers" means

| | Shift volunteers (existing) | All volunteers (this spec) |
|---|---|---|
| PTV endpoint | `shift_volunteers_csv` | `users_csv` |
| ccef-connections call | `ptv.get_shift_volunteers(state)` | `ptv.get_users(state)` |
| Grain | one row per (volunteer, shift claimed) | one row per registered volunteer |
| Volume (2024) | ~6k shift rows | **~42k–58k users** |
| Has `zip_code`? | No | Yes |
| Destination | BQ raw + Airtable | **BQ raw only (this phase)** |

`users_csv` fields (14, confirmed stable — `ep_all_vols.users_2024_raw` matches
the live endpoint schema in `ptv.md`):

`id (INT)`, `email`, `join_date (TIMESTAMP)`, `phone_number`, `first_name`,
`last_name`, `county`, `zip_code`, `source_code`, `regional_admin`, `shifted`,
`training`, `role`, `state`

---

## 2. Architecture (mirror the shift sync, minus Airtable)

`sync_all_volunteers.py` at the project root — structurally a slimmed clone of
`sync_shift_volunteers.py`, stopping at the BQ load:

```
PTV users_csv ──(get_users per state)──▶ ptv_raw_2026.users     (append-only, partitioned by as_of_date)
                                              │
                                              ▼
                                     v_users_current             (one row / (state,email), cleaned — analysis convenience)
```

Reused wholesale from the shift sync:
- **Per-state failure isolation** — try/except around each state's PTV pull;
  exit code 1 if any state failed.
- **Same-day idempotency** — pre-delete today's partition rows for the pulled
  states, then insert fresh. The view's `SELECT DISTINCT` covers the case where
  the streaming buffer blocks the DELETE (streaming-buffer-blocks-DML, ~30–90 min).

Code deltas vs. the shift script:
- endpoint call `get_shift_volunteers` → `get_users`
- **no config/registry read, no Airtable stage** — the state list is the only
  input (§3)
- row coercion: `id` → INT (empty→None); `join_date` kept as STRING in raw
  (safest for streaming insert), cast in the view

---

## 3. Which states to pull

No Airtable targets means the state list can't come from a destination registry
the way the shift sync derives it. Two options:

- **(Recommended) Pull all 50 states + DC every run.** `users_csv` returns
  `[]` for states with no data (the connector already handles the PTV
  "Not Found" sentinel), so empty states simply write no rows. Zero config,
  complete coverage, and new program states light up automatically. Cost is
  ~51 API calls/day — trivial.
- **Curated list.** A module constant or a small `ep.*` config table scoped to
  active EP states (2024 program states were ~20: AZ CO FL GA HI IL IN MA MD MI
  MN MO NE NH NM NY OH PA RI WY). More to maintain; only worth it if we want to
  deliberately exclude coalition-only states from the raw table.

Recommendation: start with **all states + DC** as a module constant
`PULL_STATES`. Scoping can always happen at query time against the raw table.

---

## 4. BigQuery objects (DDL)

### 4a. Raw table
```sql
CREATE TABLE `proj-tmc-mem-com.ptv_raw_2026.users`
(
  as_of_date     DATE NOT NULL,
  id             INT64,
  email          STRING,
  join_date      STRING,      -- kept STRING for safe streaming insert; cast in view
  phone_number   STRING,
  first_name     STRING,
  last_name      STRING,
  county         STRING,
  zip_code       STRING,
  source_code    STRING,
  regional_admin STRING,
  shifted        STRING,
  training       STRING,
  role           STRING,
  state          STRING
)
PARTITION BY as_of_date
CLUSTER BY state, email
OPTIONS(description="Raw registered-volunteer snapshots from PTV users_csv. Append-only, full per-state snapshot per as_of_date. Use v_users_current for the current per-volunteer view.");
```

### 4b. Current view (analysis convenience)
One row per (state, email) from each state's latest snapshot, with light
normalization. Not required for the sync to function, but makes the raw table
usable without everyone re-deriving "latest + dedup".

```sql
CREATE VIEW `proj-tmc-mem-com.ptv_raw_2026.v_users_current` AS
WITH latest AS (
  SELECT state, MAX(as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.ptv_raw_2026.users`
  GROUP BY state
),
dedup AS (
  SELECT DISTINCT u.*
  FROM `proj-tmc-mem-com.ptv_raw_2026.users` u
  JOIN latest USING (state, as_of_date)
)
SELECT
  state,
  TRIM(LOWER(email))                             AS email,
  ANY_VALUE(TRIM(first_name))                    AS first_name,
  ANY_VALUE(TRIM(last_name))                     AS last_name,
  ANY_VALUE(RIGHT(CAST(phone_number AS STRING), 10)) AS phone_number,
  ANY_VALUE(county)                              AS county,
  ANY_VALUE(zip_code)                            AS zip_code,
  ANY_VALUE(role)                                AS role,
  ANY_VALUE(source_code)                         AS source_code,
  ANY_VALUE(SAFE_CAST(join_date AS TIMESTAMP))   AS join_date,
  ANY_VALUE(training)                            AS training,
  ANY_VALUE(shifted)                             AS shifted,
  MAX(id)                                        AS ptv_id,
  ANY_VALUE(as_of_date)                          AS last_synced_at
FROM dedup
WHERE email IS NOT NULL AND email != ''
GROUP BY state, TRIM(LOWER(email));
```

Normalization borrowed from the 2024 `users_for_airtable_export` view:
`email = TRIM(LOWER())`, `phone = last 10 digits`. I dropped 2024's
`INITCAP`-with-exceptions name-casing — it had a bug (last-name casing was
keyed off whether *first_name* contained a space) and `INITCAP` mangles
`McDonald`/`O'Brien`. Names pass through `TRIM`-only. Adjust in §7 if desired.

---

## 5. Civis job

New GitHub-backed container job, mirroring "EP Shift Volunteer Sync":

| Field | Value |
|---|---|
| Job name | `EP All-Volunteers Sync` |
| Source repo / branch | `common-cause/ep-syncs` / `main` |
| Docker image | `civisanalytics/datascience-python:latest` |
| Command | `bash app/civis/sync_all_volunteers.sh` |
| Schedule | Daily, offset from the shift job (e.g. 6:30 AM ET) |
| Credentials | `PTV_API_KEY` (39093), `BIGQUERY_CREDENTIALS` (38653) — Airtable cred not needed this phase |

Enable failure notifications at setup (lesson from the shift job's silent
3-week failure).

`civis/sync_all_volunteers.sh`:
```bash
#!/bin/bash
# BQ-only phase: airtable extra not required yet.
pip install "ccef-connections[bigquery] @ git+https://github.com/common-cause/ccef_connections.git@v0.2.0"
python app/sync_all_volunteers.py
```

Add a section to `civis/SCHEDULED_SCRIPTS.md` once the job exists.

---

## 6. Build checklist

- [x] Create `ptv_raw_2026.users` + `v_users_current` (§4). No registry table
      needed this phase.
- [x] `sync_all_volunteers.py` — clone shift script; swap endpoint to
      `get_users`, drop the Airtable stage + registry read, set `PULL_STATES`,
      adjust row coercion (§2). Added a `--states` override for ops/testing.
- [x] `civis/sync_all_volunteers.sh` — entrypoint (§5)
- [x] End-to-end local run → 2026-07-02: all 51 states, 59,527 volunteers
      landed, exit 0. Insert chunked at 500/request. View dedup confirmed.
- [ ] **Create Civis job + enable failure notifications** (Rob)
- [x] Document in `civis/SCHEDULED_SCRIPTS.md`

---

## 7. Deferred to a later phase (Airtable leg)

Kept here so the reverse-engineering isn't lost when we pick this back up. The
2024 `ep_all_vols` pipeline fed Airtable via:
- a **state→base registry** (`ep_all_vols_to_airtable_mapping_2024_raw`:
  `state`, `airtable_base_key`, `airtable_table_name`) — the ancestor of
  `ep.shift_volunteer_sync_targets`;
- a **field map** (`airtable_column_mappings_raw`: `source_column`,
  `airtable_column`, `source_id` bool = match key, `type`);
- **`unique_id = CONCAT(email,'_',state)`** → the `Unique ID Column`.

When we add the Airtable leg, mirror the shift sync's Stage 3: a
`ep.all_volunteer_sync_targets` registry + a `DEFAULT_FIELD_MAP` (contact cols
**plus `zip_code → "Zip"`**, which `users_csv` carries and the shift schema
lacks), email-keyed `batch_upsert`, and the duplicate-key skip guard. Open
questions to resolve then: destination table name (`All Volunteers`?), which
bases, and whether to sync bulk-upload records (~35k of 58k in 2024 were spring
bulk uploads of prior-year lists — fine for a name source, noise for counts).
```

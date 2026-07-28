-- ep_2026_cleaned.volunteers — THE 2026 EP volunteer roster.
--
-- Grain: one row per (state, email) EVER seen in PTV users snapshots.
-- All-time: rows never disappear (current-state consumers filter is_active).
-- This matches the append-only roster contract sync_volunteer_sheets.py needs.
--
-- Three branches, in precedence order -- a person in more than one system
-- lands ONCE, on the richest branch, via NOT EXISTS guards:
--   'ptv' -- every (state, email) ever seen in ptv_raw_2026.users.
--   'airtable_self_add' -- Airtable "Shifted Volunteers" records (via the
--   generated ep_2026_cleaned.shifted_volunteers union, which is why this
--   file sorts AFTER the 3x generated views) whose (state, email) has no
--   PTV counterpart -- the emergency self-add form's output. A self-add who
--   later registers in PTV flips to the PTV branch on the next capture.
--   'asana' -- volunteers on a registered state Asana board (via
--   asana_raw_2026.ep_kanban_tasks) with no PTV and no Airtable counterpart.
--   These are states that deploy volunteers outside our toolset entirely:
--   Common Cause offers PTV and the Airtable templates but does not require
--   them, and NM (2026) uses neither. Without this branch a state's whole
--   volunteer file is invisible to every consumer of this layer. See
--   docs/asana_nm_sync_spec.md and the knowledge-library entry
--   `ep-state-deployment-tools-the-stack-is-offered-not-mandated`.
--
-- Coverage caveat on the Asana branch: it is keyed on an email parsed out of a
-- board that has no structured contact field, so volunteers whose card carries
-- no address CANNOT appear here at all (8 of 42 on the NM board at go-live,
-- all of them in its most-advanced stage). ep_2026_cleaned.asana_pipeline
-- retains those rows -- query it to see who the roster is losing.
--
-- is_bulk_upload / joined_this_cycle are lifted from ep-dashboards
-- stg_ptv__users (vars: ep_cycle_start='2025-12-01',
-- bulk_upload_hourly_threshold=100) so the two stay in agreement until
-- ep-dashboards re-points here. Keep the constants in lockstep.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.volunteers`
OPTIONS(description="All-time 2026 EP volunteer roster: one row per (state, email) ever seen in PTV (ptv_raw_2026.users) UNION Airtable self-adds with no PTV counterpart (source_system='airtable_self_add', from the emergency self-add form) UNION volunteers on registered state Asana boards with neither counterpart (source_system='asana', for states deploying outside PTV/Airtable — NM in 2026). Non-PTV branches carry in_ptv=FALSE and are deduped against the richer branches, so a person in two systems appears once. Asana rows are keyed on an email parsed from an unstructured board, so board entries lacking an address are absent entirely — see ep_2026_cleaned.asana_pipeline for who is lost. Rows never disappear; filter is_active for the current roster. email/phone normalized (norm_email/norm_phone); email_raw preserves the as-delivered value. Attributes come from the person's newest snapshot row; shift rollups derive from ep_2026_cleaned.shift_signups. is_bulk_upload flags prior-year bulk loads (source_code='previous_years' or >=100 joins in the same state+hour) — flagged, never filtered. joined_this_cycle = joined_at >= 2025-12-01 (self-adds: Airtable record creation). Freshness: ptv_as_of_date (NULL on self-add rows — check sync_health for the Airtable stream).")
AS
WITH latest AS (
  SELECT state, MAX(as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.ptv_raw_2026.users`
  GROUP BY state
),
ranked AS (
  -- Newest snapshot row per (state, normalized email) over ALL time.
  SELECT
    u.*,
    `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(u.email) AS email_n,
    ROW_NUMBER() OVER (
      PARTITION BY u.state, `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(u.email)
      ORDER BY u.as_of_date DESC, u.id DESC
    ) AS rn
  FROM `proj-tmc-mem-com.ptv_raw_2026.users` u
  WHERE `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(u.email) IS NOT NULL
),
ever AS (
  SELECT
    state,
    email_n,
    MIN(as_of_date) AS first_seen_date,
    MAX(as_of_date) AS last_seen_date,
    ARRAY_AGG(DISTINCT NULLIF(LOWER(source_code), '') IGNORE NULLS) AS source_codes_ever
  FROM ranked
  GROUP BY state, email_n
),
persons AS (
  SELECT
    r.state,
    r.email_n                                                  AS email,
    r.email                                                    AS email_raw,
    NULLIF(TRIM(r.first_name), '')                             AS first_name,
    NULLIF(TRIM(r.last_name), '')                              AS last_name,
    `proj-tmc-mem-com.ep_2026_cleaned.norm_phone`(r.phone_number) AS phone,
    NULLIF(TRIM(r.county), '')                                 AS county,
    NULLIF(TRIM(r.zip_code), '')                               AS zip_code,
    NULLIF(TRIM(r.role), '')                                   AS role,
    NULLIF(TRIM(r.source_code), '')                            AS source_code,
    NULLIF(TRIM(r.training), '')                               AS training,
    SAFE_CAST(r.join_date AS TIMESTAMP)                        AS joined_at,
    r.id                                                       AS ptv_id,
    NULLIF(TRIM(r.shifted), '')                                AS ptv_shifted_flag,
    r.as_of_date                                               AS row_as_of_date
  FROM ranked r
  WHERE r.rn = 1
),
-- Bulk-upload heuristic (mirrors ep-dashboards stg_ptv__users): prior-year
-- lists load in batches, producing >= 100 accounts in the same (state, hour).
hourly_counts AS (
  SELECT
    state,
    TIMESTAMP_TRUNC(joined_at, HOUR) AS join_hour,
    COUNT(*)                         AS signups_in_hour
  FROM persons
  WHERE joined_at IS NOT NULL
  GROUP BY 1, 2
),
shift_rollup AS (
  SELECT
    state,
    email,
    COUNT(*)                 AS shift_count,
    COUNTIF(is_upcoming)     AS upcoming_shift_count,
    MIN(shift_date)          AS first_shift_date,
    MAX(shift_date)          AS latest_shift_date
  FROM `proj-tmc-mem-com.ep_2026_cleaned.shift_signups`
  WHERE email IS NOT NULL
  GROUP BY state, email
),
-- Asana branch inputs. Reads the raw snapshots directly (as the PTV branch
-- does) rather than ep_2026_cleaned.asana_pipeline, because that view is
-- current-snapshot-only and this roster is ALL-TIME: a volunteer whose card is
-- deleted from a board must not vanish from the roster.
asana_reg AS (
  SELECT project_gid
  FROM `proj-tmc-mem-com.ep.asana_sync_sources`
  WHERE enabled AND project_kind = 'volunteer_pipeline'
),
asana_snap AS (
  SELECT t.*
  FROM `proj-tmc-mem-com.asana_raw_2026.ep_kanban_tasks` t
  JOIN asana_reg USING (project_gid)
  WHERE t.parsed_email IS NOT NULL
  -- Dedupe the ~90-min streaming-buffer rerun case before aggregating.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.as_of_date, t.task_gid ORDER BY t.task_modified_at DESC
  ) = 1
),
asana_latest AS (
  SELECT project_gid, MAX(as_of_date) AS as_of_date
  FROM asana_snap
  GROUP BY project_gid
),
asana_ever AS (
  SELECT
    state,
    parsed_email                AS email,
    MIN(as_of_date)             AS first_seen_date,
    MAX(as_of_date)             AS last_seen_date,
    ARRAY_AGG(DISTINCT NULLIF(LOWER(source_tags), '') IGNORE NULLS)
                                AS source_codes_ever
  FROM asana_snap
  GROUP BY state, parsed_email
),
-- Exclusion set for the Asana branch's Airtable guard, as a CTE rather than a
-- correlated NOT EXISTS: shifted_volunteers is itself a large generated UNION
-- view, and BigQuery cannot de-correlate a subquery against it ("Correlated
-- subqueries that reference other tables are not supported unless they can be
-- de-correlated"). That failure hits ANY query touching this view, not just
-- Asana rows, so this must stay an anti-join. (The PTV guard on the
-- airtable_self_add branch below is fine as NOT EXISTS -- ptv_raw_2026.users
-- is a plain table, which BigQuery does de-correlate.)
airtable_people AS (
  SELECT DISTINCT state, email
  FROM `proj-tmc-mem-com.ep_2026_cleaned.shifted_volunteers`
  WHERE email IS NOT NULL
),
asana_persons AS (
  -- Newest snapshot row per (state, email); a board with two cards for the
  -- same address collapses to the most recently modified one.
  SELECT
    s.state,
    s.parsed_email AS email,
    s.parsed_phone,
    s.task_name,
    s.source_tags,
    s.stage,
    s.task_created_at,
    s.as_of_date,
    s.project_gid,
    (s.as_of_date = l.as_of_date) AS is_active
  FROM asana_snap s
  JOIN asana_latest l ON l.project_gid = s.project_gid
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.state, s.parsed_email
    ORDER BY s.as_of_date DESC, s.task_modified_at DESC
  ) = 1
)
SELECT
  p.state,
  p.email,
  p.email_raw,
  p.first_name,
  p.last_name,
  p.phone,
  p.county,
  p.zip_code,
  p.role,
  p.source_code,
  e.source_codes_ever,
  p.training,
  p.joined_at,
  COALESCE(p.joined_at >= TIMESTAMP('2025-12-01'), FALSE)     AS joined_this_cycle,
  (COALESCE(LOWER(p.source_code) = 'previous_years', FALSE)
   OR COALESCE(h.signups_in_hour >= 100, FALSE))              AS is_bulk_upload,
  p.ptv_id,
  p.ptv_shifted_flag,
  COALESCE(s.shift_count, 0)                                  AS shift_count,
  COALESCE(s.upcoming_shift_count, 0)                         AS upcoming_shift_count,
  s.first_shift_date,
  s.latest_shift_date,
  TRUE                                                        AS in_ptv,
  'ptv'                                                       AS source_system,
  p.row_as_of_date = l.as_of_date                             AS is_active,
  e.first_seen_date,
  e.last_seen_date,
  l.as_of_date                                                AS ptv_as_of_date
FROM persons p
JOIN ever e
  ON e.state = p.state AND e.email_n = p.email
JOIN latest l
  ON l.state = p.state
LEFT JOIN hourly_counts h
  ON h.state = p.state
 AND h.join_hour = TIMESTAMP_TRUNC(p.joined_at, HOUR)
LEFT JOIN shift_rollup s
  ON s.state = p.state AND s.email = p.email

UNION ALL

-- Airtable self-adds: Shifted Volunteers records with no PTV counterpart
-- (state, email) anywhere in the PTV snapshots. Typed captures are
-- current-state, so any row here is present in the latest sync.
SELECT
  sv.state,
  sv.email,
  sv.email_raw,
  sv.first_name,
  sv.last_name,
  sv.phone,
  sv.county,
  sv.zip_code,
  CAST(NULL AS STRING)                                        AS role,
  CAST(NULL AS STRING)                                        AS source_code,
  CAST([] AS ARRAY<STRING>)                                   AS source_codes_ever,
  CAST(NULL AS STRING)                                        AS training,
  sv.created_at                                               AS joined_at,
  COALESCE(sv.created_at >= TIMESTAMP('2025-12-01'), FALSE)   AS joined_this_cycle,
  FALSE                                                       AS is_bulk_upload,
  CAST(NULL AS INT64)                                         AS ptv_id,
  CAST(NULL AS STRING)                                        AS ptv_shifted_flag,
  COALESCE(s.shift_count, 0)                                  AS shift_count,
  COALESCE(s.upcoming_shift_count, 0)                         AS upcoming_shift_count,
  s.first_shift_date,
  s.latest_shift_date,
  FALSE                                                       AS in_ptv,
  'airtable_self_add'                                         AS source_system,
  TRUE                                                        AS is_active,
  DATE(sv.created_at)                                         AS first_seen_date,
  DATE(sv.synced_at)                                          AS last_seen_date,
  CAST(NULL AS DATE)                                          AS ptv_as_of_date
FROM `proj-tmc-mem-com.ep_2026_cleaned.shifted_volunteers` sv
LEFT JOIN shift_rollup s
  ON s.state = sv.state AND s.email = sv.email
WHERE sv.email IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM `proj-tmc-mem-com.ptv_raw_2026.users` u
    WHERE u.state = sv.state
      AND `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(u.email) = sv.email
  )
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY sv.state, sv.email
  ORDER BY sv.created_at DESC
) = 1

UNION ALL

-- Asana boards: volunteers tracked by a state that deploys outside PTV and
-- Airtable (NM in 2026). Excluded if the same (state, email) exists in PTV or
-- in the Airtable self-add set, so the (state, email) grain holds and a person
-- in two systems lands once -- on the richer branch. The cross-guard against
-- shifted_volunteers costs nothing today (no state runs both) and is the point
-- of the exercise: the next state to stand up its own tool may well run both.
SELECT
  a.state,
  a.email,
  a.email                                                     AS email_raw,
  NULLIF(TRIM(REGEXP_EXTRACT(a.task_name, r'^(\S+)')), '')    AS first_name,
  NULLIF(TRIM(REGEXP_REPLACE(a.task_name, r'^\S+\s*', '')), '') AS last_name,
  a.parsed_phone                                              AS phone,
  CAST(NULL AS STRING)                                        AS county,
  CAST(NULL AS STRING)                                        AS zip_code,
  CAST(NULL AS STRING)                                        AS role,
  NULLIF(LOWER(a.source_tags), '')                            AS source_code,
  e.source_codes_ever,
  CAST(NULL AS STRING)                                        AS training,
  a.task_created_at                                           AS joined_at,
  COALESCE(a.task_created_at >= TIMESTAMP('2025-12-01'), FALSE) AS joined_this_cycle,
  FALSE                                                       AS is_bulk_upload,
  CAST(NULL AS INT64)                                         AS ptv_id,
  CAST(NULL AS STRING)                                        AS ptv_shifted_flag,
  COALESCE(s.shift_count, 0)                                  AS shift_count,
  COALESCE(s.upcoming_shift_count, 0)                         AS upcoming_shift_count,
  s.first_shift_date,
  s.latest_shift_date,
  FALSE                                                       AS in_ptv,
  'asana'                                                     AS source_system,
  a.is_active,
  e.first_seen_date,
  e.last_seen_date,
  CAST(NULL AS DATE)                                          AS ptv_as_of_date
FROM asana_persons a
JOIN asana_ever e
  ON e.state = a.state AND e.email = a.email
LEFT JOIN shift_rollup s
  ON s.state = a.state AND s.email = a.email
-- Anti-join against PTV. Reuses `ever` (every (state, email) seen in any PTV
-- snapshot) rather than re-scanning ptv_raw_2026.users.
LEFT JOIN ever pe
  ON pe.state = a.state AND pe.email_n = a.email
-- Anti-join against the Airtable self-add set (see airtable_people above).
LEFT JOIN airtable_people ap
  ON ap.state = a.state AND ap.email = a.email
WHERE pe.email_n IS NULL
  AND ap.email IS NULL;

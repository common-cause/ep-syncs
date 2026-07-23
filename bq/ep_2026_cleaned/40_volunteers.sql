-- ep_2026_cleaned.volunteers — THE 2026 EP volunteer roster.
--
-- Grain: one row per (state, email) EVER seen in PTV users snapshots.
-- All-time: rows never disappear (current-state consumers filter is_active).
-- This matches the append-only roster contract sync_volunteer_sheets.py needs.
--
-- Two branches:
--   'ptv' -- every (state, email) ever seen in ptv_raw_2026.users.
--   'airtable_self_add' -- Airtable "Shifted Volunteers" records (via the
--   generated ep_2026_cleaned.shifted_volunteers union, which is why this
--   file sorts AFTER the 3x generated views) whose (state, email) has no
--   PTV counterpart -- the emergency self-add form's output. A self-add who
--   later registers in PTV flips to the PTV branch on the next capture.
--
-- is_bulk_upload / joined_this_cycle are lifted from ep-dashboards
-- stg_ptv__users (vars: ep_cycle_start='2025-12-01',
-- bulk_upload_hourly_threshold=100) so the two stay in agreement until
-- ep-dashboards re-points here. Keep the constants in lockstep.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.volunteers`
OPTIONS(description="All-time 2026 EP volunteer roster: one row per (state, email) ever seen in PTV (ptv_raw_2026.users) UNION Airtable self-adds with no PTV counterpart (source_system='airtable_self_add', in_ptv=FALSE, from the emergency self-add form). Rows never disappear; filter is_active for the current roster. email/phone normalized (norm_email/norm_phone); email_raw preserves the as-delivered value. Attributes come from the person's newest snapshot row; shift rollups derive from ep_2026_cleaned.shift_signups. is_bulk_upload flags prior-year bulk loads (source_code='previous_years' or >=100 joins in the same state+hour) — flagged, never filtered. joined_this_cycle = joined_at >= 2025-12-01 (self-adds: Airtable record creation). Freshness: ptv_as_of_date (NULL on self-add rows — check sync_health for the Airtable stream).")
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
) = 1;

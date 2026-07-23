-- ep_2026_cleaned.sync_health — per-stream freshness for the interface layer.
--
-- One row per source stream: PTV users / PTV shift signups per state, and
-- one per captured Airtable (base, table) that has ever had records.
-- Consumers should check staleness_days <= 1 before trusting data.
--
-- latest_sync (TIMESTAMP) is NULL for PTV streams — the raw tables only
-- carry as_of_date. Airtable streams populate it from synced_at.
-- Airtable tables that have never had a record don't appear (the history
-- table only receives rows for records; their typed tables still exist).

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.sync_health`
OPTIONS(description="Freshness per source stream feeding ep_2026_cleaned. source: 'ptv_users' | 'ptv_shift_volunteers' | 'airtable'. scope: state code (PTV) or base_key__table_key (Airtable). row_count = deduped rows in the scope's latest snapshot. Check staleness_days <= 1 before trusting data. Airtable tables with no records ever don't appear.")
AS
WITH users_by_day AS (
  SELECT state, as_of_date, COUNT(*) AS c
  FROM `proj-tmc-mem-com.ptv_raw_2026.users`
  GROUP BY state, as_of_date
),
users_latest AS (
  SELECT
    state,
    ARRAY_AGG(STRUCT(as_of_date, c) ORDER BY as_of_date DESC LIMIT 1)[OFFSET(0)] AS last
  FROM users_by_day
  GROUP BY state
),
shifts_by_day AS (
  SELECT state, as_of_date, COUNT(*) AS c
  FROM `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers`
  GROUP BY state, as_of_date
),
shifts_latest AS (
  SELECT
    state,
    ARRAY_AGG(STRUCT(as_of_date, c) ORDER BY as_of_date DESC LIMIT 1)[OFFSET(0)] AS last
  FROM shifts_by_day
  GROUP BY state
)
SELECT
  'ptv_users'                                        AS source,
  state                                              AS scope,
  CAST(NULL AS TIMESTAMP)                            AS latest_sync,
  last.as_of_date                                    AS latest_as_of,
  last.c                                             AS row_count,
  DATE_DIFF(CURRENT_DATE(), last.as_of_date, DAY)    AS staleness_days
FROM users_latest
UNION ALL
SELECT
  'ptv_shift_volunteers'                             AS source,
  state                                              AS scope,
  CAST(NULL AS TIMESTAMP)                            AS latest_sync,
  last.as_of_date                                    AS latest_as_of,
  last.c                                             AS row_count,
  DATE_DIFF(CURRENT_DATE(), last.as_of_date, DAY)    AS staleness_days
FROM shifts_latest
UNION ALL
SELECT
  'airtable'                                         AS source,
  scope,
  latest_sync,
  latest_as_of,
  row_count,
  DATE_DIFF(CURRENT_DATE(), latest_as_of, DAY)       AS staleness_days
FROM (
  SELECT
    CONCAT(bq_table_prefix, '__', table_key) AS scope,
    ARRAY_AGG(
      STRUCT(as_of_date AS latest_as_of, max_synced AS latest_sync, c AS row_count)
      ORDER BY as_of_date DESC LIMIT 1
    )[OFFSET(0)].*
  FROM (
    SELECT
      bq_table_prefix, table_key, as_of_date,
      MAX(synced_at) AS max_synced,
      COUNT(DISTINCT airtable_record_id) AS c
    FROM `proj-tmc-mem-com.ep_2026_raw.airtable_records_history`
    GROUP BY bq_table_prefix, table_key, as_of_date
  )
  GROUP BY scope
);

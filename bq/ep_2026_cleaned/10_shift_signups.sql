-- ep_2026_cleaned.shift_signups — event-level shift signups, latest snapshot.
--
-- One row per raw signup row (volunteer x shift) in each state's latest
-- ptv_raw_2026.shift_volunteers partition. This is the event grain that
-- v_shift_volunteers_current aggregates away; the volunteers view derives
-- its shift rollups from THIS view so the numbers always agree.
--
-- Identity: email/phone normalized via ep_2026_cleaned.norm_* (the raw
-- v_shift_volunteers_current does NOT lowercase email — this view is the
-- normalized surface; consumers should join here, not on the raw view).
-- NULL-email rows are kept (event grain keeps every signup).

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.shift_signups`
OPTIONS(description="Event-level PTV shift signups from each state's latest ptv_raw_2026.shift_volunteers snapshot (all 50 states + DC). One row per (volunteer, shift) signup. email/phone normalized via ep_2026_cleaned.norm_email/norm_phone; email_raw preserves the as-delivered value. NULL-email rows kept. Freshness: ptv_as_of_date.")
AS
WITH latest AS (
  SELECT state, MAX(as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers`
  GROUP BY state
),
dedup AS (
  -- Same-day reruns can double the snapshot when the streaming buffer blocks
  -- the pre-delete; DISTINCT collapses exact duplicates (house pattern).
  SELECT DISTINCT s.*
  FROM `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers` s
  JOIN latest USING (state, as_of_date)
)
SELECT
  state,
  `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(email)      AS email,
  email                                                     AS email_raw,
  NULLIF(TRIM(first_name), '')                              AS first_name,
  NULLIF(TRIM(last_name), '')                               AS last_name,
  `proj-tmc-mem-com.ep_2026_cleaned.norm_phone`(phone_number) AS phone,
  NULLIF(TRIM(county), '')                                  AS county,
  NULLIF(TRIM(role), '')                                    AS role,
  NULLIF(TRIM(source), '')                                  AS source_code,
  shift_id,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', inserted_at)    AS claimed_at,
  date                                                      AS shift_date,
  start_time,
  end_time,
  timezone,
  NULLIF(TRIM(locations), '')                               AS location,
  COALESCE(date >= CURRENT_DATE(), FALSE)                   AS is_upcoming,
  as_of_date                                                AS ptv_as_of_date
FROM dedup;

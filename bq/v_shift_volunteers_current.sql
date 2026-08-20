-- One row per (state, email) from the latest snapshot per state, with shift
-- counts rolled up. This is the view sync_shift_volunteers.py Stage 3 reads
-- back to feed the Airtable leg.
--
-- Hand-applied, like the other bq/*.sql files here. NOT covered by
-- apply_bq_views.py -- that script is hardcoded to DATASET = "ep_2026_cleaned"
-- and globs bq/ep_2026_cleaned/*.sql only. Apply changes manually and keep
-- this file in step with the deployed view.
--
-- NOTE ON EMAIL CASE: the GROUP BY below is on raw `email`, so `Bob@x.com`
-- and `bob@x.com` are two rows -- one human, two records downstream. Wrapping
-- the key in LOWER(TRIM(email)) would collapse them, but Airtable's
-- performUpsert matches `fieldsToMergeOn` case-SENSITIVELY (verified against a
-- live base 2026-08-19), so re-keying here would orphan every existing
-- mixed-case Airtable record rather than patch it. Any such change needs a
-- paired migration of the destination rows in the same window -- see
-- docs/ep_2026_cleaned_spec.md and the email-case discussion in
-- civis/SCHEDULED_SCRIPTS.md.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ptv_raw_2026.v_shift_volunteers_current` AS
WITH latest AS (
  SELECT state, MAX(as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers`
  GROUP BY state
),
dedup AS (
  SELECT DISTINCT s.*
  FROM `proj-tmc-mem-com.ptv_raw_2026.shift_volunteers` s
  JOIN latest USING (state, as_of_date)
)
SELECT
  state,
  email,
  ANY_VALUE(first_name)             AS first_name,
  ANY_VALUE(last_name)              AS last_name,
  ANY_VALUE(phone_number)           AS phone_number,
  ANY_VALUE(county)                 AS county,
  ANY_VALUE(role)                   AS role,
  ANY_VALUE(source)                 AS source,
  COUNT(*)                          AS shift_count,
  MIN(date)                         AS first_shift_date,
  MAX(date)                         AS latest_shift_date,
  COUNTIF(date >= CURRENT_DATE())   AS upcoming_shift_count,
  ANY_VALUE(as_of_date)             AS last_synced_at
FROM dedup
WHERE email IS NOT NULL AND email != ''
GROUP BY state, email;

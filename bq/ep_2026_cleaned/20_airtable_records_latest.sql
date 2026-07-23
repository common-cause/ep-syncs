-- ep_2026_cleaned.airtable_records_latest — latest JSON payload per record.
--
-- The escape hatch for state-specific fields the canonical union views
-- don't carry: every field of every record in every captured base, as
-- verbatim JSON keyed by the UNSANITIZED Airtable field name. Pull values
-- with e.g. JSON_VALUE(fields, '$."Is the dropbox accessible?"').
--
-- "Just add column X for state Y" requests get answered with this view
-- (or a registry canonical_override), not with contract changes.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.airtable_records_latest`
OPTIONS(description="Latest JSON payload per Airtable record across all captured 2026 EP bases (ep_2026_raw.airtable_records_history). One row per (base_id, table_key, record). fields keys are UNSANITIZED Airtable field names -- JSON_VALUE(fields, '$.\"Field Name\"'). is_in_latest_run=FALSE means the record no longer appears in that table's newest sync (deleted/filtered in Airtable). Attachment URLs inside payloads expire within hours of capture.")
AS
WITH ranked AS (
  SELECT
    h.*,
    ROW_NUMBER() OVER (
      PARTITION BY base_id, table_key, airtable_record_id
      ORDER BY as_of_date DESC, synced_at DESC
    ) AS rn,
    MAX(as_of_date) OVER (PARTITION BY base_id, table_key) AS latest_run
  FROM `proj-tmc-mem-com.ep_2026_raw.airtable_records_history` h
)
SELECT
  state,
  base_type,
  base_name,
  bq_table_prefix AS base_key,
  base_id,
  table_name,
  table_key,
  airtable_record_id AS record_id,
  airtable_created_time AS created_at,
  fields,
  as_of_date,
  synced_at,
  as_of_date = latest_run AS is_in_latest_run
FROM ranked
WHERE rn = 1;

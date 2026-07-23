-- Append-only JSON history of every record in every registered Airtable
-- base (ep.airtable_sync_sources). Written by ep-syncs/sync_airtable_bases.py:
-- one row per record per run, streaming-inserted after a per-(day, base)
-- pre-delete (idempotency; the delete is skipped with a warning when the
-- streaming buffer blocks it -- see the dedupe recipe below).
--
-- This is the schema-drift-proof audit trail: partners can rename/add/
-- retype fields freely and the verbatim `fields` payload still lands.
-- The TYPED current-state surface is the per-(base, table) tables
-- `ep_2026_raw.{prefix}__{table}` rebuilt each run (full-replace).
--
-- READER DEDUPE RECIPE -- JSON columns are not groupable/comparable, so
-- SELECT DISTINCT does NOT work here (unlike the ptv_raw_2026 views).
-- Take the latest copy per record per day with:
--
--   QUALIFY ROW_NUMBER() OVER (
--     PARTITION BY as_of_date, bq_table_prefix, table_key, airtable_record_id
--     ORDER BY synced_at DESC) = 1
--
-- NOTE: Airtable attachment URLs inside `fields` expire within hours of
-- capture -- this table preserves structure/audit, not files.
--
-- Run once to create. Re-running will fail with "Already Exists" --
-- safe; do not change to CREATE OR REPLACE without checking rows.

CREATE TABLE `proj-tmc-mem-com.ep_2026_raw.airtable_records_history` (
  as_of_date            DATE      NOT NULL  OPTIONS(description="Run date (UTC). Partition key. One snapshot of every captured record per day; same-day reruns pre-delete this partition per base and rewrite (streaming-buffer caveat: dedupe reads with the ROW_NUMBER recipe in the table DDL comments)."),
  base_id               STRING    NOT NULL  OPTIONS(description="Airtable base ID (app...)."),
  base_name             STRING              OPTIONS(description="Registry `name` at capture time (e.g. 'NE Field Report')."),
  bq_table_prefix       STRING    NOT NULL  OPTIONS(description="Registry prefix; join key to the typed tables and to ep.airtable_sync_sources. Clustering key."),
  table_name            STRING    NOT NULL  OPTIONS(description="Airtable table name, verbatim (unsanitized)."),
  table_key             STRING    NOT NULL  OPTIONS(description="sanitize_column_name(table_name) -- matches the typed-table suffix in ep_2026_raw.{prefix}__{table_key}. Clustering key."),
  state                 STRING              OPTIONS(description="Registry state code at capture time."),
  base_type             STRING              OPTIONS(description="Registry base_type at capture time ('field_report' | 'quiz' | 'tracker')."),
  airtable_record_id    STRING    NOT NULL  OPTIONS(description="Airtable record ID (rec...). Stable for the record's lifetime."),
  airtable_created_time TIMESTAMP           OPTIONS(description="Airtable's createdTime for the record."),
  fields                JSON                OPTIONS(description="Full Airtable fields payload, verbatim (UNSANITIZED field names as JSON keys). Empty fields are absent, per Airtable API semantics. Attachment URLs expire within hours."),
  synced_at             TIMESTAMP NOT NULL  OPTIONS(description="Run timestamp (UTC). Tie-breaker for same-day reruns via the ROW_NUMBER dedupe recipe.")
)
PARTITION BY as_of_date
CLUSTER BY bq_table_prefix, table_key
OPTIONS(
  description="Append-only daily history of every record in every registered Airtable base (ep.airtable_sync_sources). One row per record per run; `fields` is the verbatim JSON payload (drift-proof). Written by ep-syncs/sync_airtable_bases.py. Dedupe reads with ROW_NUMBER over (as_of_date, bq_table_prefix, table_key, airtable_record_id) ORDER BY synced_at DESC -- JSON columns can't SELECT DISTINCT."
);

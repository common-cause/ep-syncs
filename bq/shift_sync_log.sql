-- Per-run telemetry for sync_shift_volunteers.py.
--
-- WHY THIS EXISTS: both legs of the shift sync had failure modes that ended in
-- `exit 0` with nothing but a log line nobody reads:
--
--   * the Airtable leg skips any volunteer whose email already matches >1
--     destination record (it would 422 the whole batch). 13 volunteers across
--     MI/NE/PA were being skipped on every run, discovered only by hand-auditing
--     the bases on 2026-08-19.
--   * a state that stops returning shift data looks identical to the ~43 states
--     that legitimately return none. MA silently went to zero on 2026-08-18
--     when the '24 roles were wiped in PTV -- real, intended, and invisible.
--
-- 'regressed' is an EVENT, not a standing condition: it fires on the run where
-- a state's rows disappear and then goes quiet, because `prior_rows` is read
-- from the previous SNAPSHOT rather than from that state's last non-empty one.
-- Anchoring it to the last non-empty snapshot would re-report MA every run
-- forever, which trains everyone to ignore the warning. For the ongoing
-- condition -- a state whose roster is frozen because nothing new lands --
-- use ep_2026_cleaned.sync_health.staleness_days instead.
--
-- ep_2026_cleaned.sync_health covers BigQuery *landing* freshness, but it can
-- only see rows that landed -- it cannot see what the Airtable stage decided to
-- skip, because nothing recorded it. This table is that record.
--
-- Contains volunteer emails in `skipped_keys` (needed to action a skip).
-- BigQuery is an access-controlled system, which is where row-level people-data
-- belongs -- do not export this to a repo, sheet, or ticket unmasked.
--
-- CREATE TABLE IF NOT EXISTS so the sync self-heals a fresh environment.
-- Hand-applied; not covered by apply_bq_views.py (ep_2026_cleaned only).

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.ep.shift_sync_log` (
  run_at              TIMESTAMP NOT NULL OPTIONS(description="UTC start of the sync run. Groups all rows from one invocation."),
  as_of_date          DATE      NOT NULL OPTIONS(description="Snapshot date the run wrote, matching ptv_raw_2026.shift_volunteers.as_of_date."),
  stage               STRING    NOT NULL OPTIONS(description="'ptv_pull' (PTV -> BigQuery, one row per pulled state) or 'airtable_upsert' (one row per enabled registry target)."),
  scope               STRING    NOT NULL OPTIONS(description="State code for stage='ptv_pull'; sync-target name for stage='airtable_upsert'."),
  state               STRING             OPTIONS(description="State code, populated for both stages so either can be joined on state."),
  status              STRING    NOT NULL OPTIONS(description="'ok' | 'empty' (0 rows, and 0 in the previous snapshot -- normal for the ~43 states with no program) | 'regressed' (0 rows but the previous snapshot had some -- fires once, on the run where a state stops reporting) | 'failed' | 'skipped'."),
  rows_in             INT64              OPTIONS(description="Rows offered to the stage: pulled from PTV, or read from the view for this target."),
  rows_written        INT64              OPTIONS(description="Rows actually written: inserted into BigQuery, or sent to batch_upsert."),
  prior_rows          INT64              OPTIONS(description="stage='ptv_pull': this state's row count in the PREVIOUS snapshot -- the most recent as_of_date before this run that landed any rows (gap-tolerant; not literally yesterday). Drives 'regressed', which is why that status fires once rather than every run."),
  skipped_blank       INT64              OPTIONS(description="stage='airtable_upsert': rows dropped for an empty upsert key."),
  skipped_dupe_exact  INT64              OPTIONS(description="stage='airtable_upsert': rows skipped because the exact-spelling key already matches >1 destination record. These volunteers are NOT being updated -- merge the duplicates in Airtable to clear it."),
  case_variants       INT64              OPTIONS(description="stage='airtable_upsert': rows whose key has a case-variant twin in the destination. NOT skipped -- Airtable matches case-sensitively so these patch correctly -- but each one means two records for one human."),
  skipped_keys        JSON               OPTIONS(description="The actual keys behind skipped_dupe_exact and case_variants, as {\"skipped_dupe_exact\":[...],\"case_variants\":[...]}. Contains PII."),
  detail              STRING             OPTIONS(description="Free-text note: exception summary for status='failed', reason for 'skipped'.")
)
PARTITION BY as_of_date
OPTIONS(
  description="Per-run telemetry for sync_shift_volunteers.py, one row per (stage, scope). Makes the Airtable leg's silent skips and per-state pull regressions queryable. See bq/shift_sync_log.sql for why. Contains PII in skipped_keys."
);

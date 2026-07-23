-- Airtable base registry for the Airtable -> BigQuery capture sync.
--
-- Owned by ep-syncs (read at the start of each run by
-- sync_airtable_bases.py). One row = one Airtable base whose tables are
-- captured into ep_2026_raw: per-(base, table) TYPED tables rebuilt each
-- run (full-replace, schema-drift-proof) named
-- `ep_2026_raw.{bq_table_prefix}__{sanitized_table_name}`, plus one row
-- per record per run appended to `ep_2026_raw.airtable_records_history`
-- (JSON payload; see bq/airtable_records_history.sql).
--
-- Inserting an enabled row is how a base's data starts landing in BQ:
-- the next run discovers every table in the base (minus exclude_tables)
-- and captures them automatically. ep-airtable-utilities writes a row
-- here as the final step of taking a base live (see the registration
-- spec mailed to that project).
--
-- REGISTRATION CONTRACT (validate BEFORE inserting; the sync assumes any
-- enabled row passed these):
--   1. PAT access: AirtableConnector.get_base_schema(base_id) succeeds
--      with the same PAT the Civis job runs with (AIRTABLE_API_KEY,
--      Rob's "sync operations" token; needs scope schema.bases:read +
--      data.records:read and per-base access). If it 403s, register with
--      enabled = FALSE and a notes explaining who owns the base.
--   2. bq_table_prefix: matches ^[a-z][a-z0-9_]*$, contains NO double
--      underscore ('__' is the landed-table separator), and no existing
--      row has the same prefix (BQ doesn't enforce uniqueness -- check
--      first). Convention: '{state}_{kind}' e.g. 'ne_field_report',
--      'or_dropbox_quiz'.
--   3. Idempotency: match on base_id -- UPDATE in place (bump updated_at,
--      preserve registered_at/registered_by) rather than inserting a
--      duplicate row.
--   4. Read-only guarantee: this sync only ever READS Airtable.
--      Registration imposes zero requirements on base contents.
--
-- Recommended flow for new bases: insert with enabled = FALSE, run
-- `python sync_airtable_bases.py --check-access`, review discovered
-- tables with `--list` (set exclude_tables for template leftovers),
-- then UPDATE enabled = TRUE.
--
-- Run once to create. Re-running will fail with "Already Exists" --
-- safe; do not change to CREATE OR REPLACE without checking rows.

CREATE TABLE `proj-tmc-mem-com.ep.airtable_sync_sources` (
  name                STRING    NOT NULL  OPTIONS(description="Human-readable label, logically unique. Convention: '<State> <Kind>' e.g. 'NE Field Report', 'OR Drop Box Quiz'. Used in sync logs."),
  state               STRING    NOT NULL  OPTIONS(description="Two-letter US state code. Downstream grouping metadata (ep_2026_cleaned views take state from THIS column, never from record fields); does not gate capture."),
  base_id             STRING    NOT NULL  OPTIONS(description="Airtable base ID (app...). Logically unique -- one registry row per base. The registration contract upserts on this key."),
  base_type           STRING    NOT NULL  OPTIONS(description="'field_report' | 'quiz' | 'tracker'. Semantic hint for the ep_2026_cleaned union-view generator (which entity views a base's tables feed). No trackers registered in 2026 (OH BOE tracker deliberately out of scope)."),
  bq_table_prefix     STRING    NOT NULL  OPTIONS(description="Unique across rows. Lowercase snake_case matching ^[a-z][a-z0-9_]*$, MUST NOT contain '__' (that's the landed-table separator). Landed tables are ep_2026_raw.{prefix}__{sanitized_table_name}. Changing it orphans previously landed tables (drop them manually)."),
  exclude_tables      ARRAY<STRING>       OPTIONS(description="Exact Airtable table names (unsanitized) to skip. NULL/[] = capture every table in the base. New tables added to the base are captured automatically -- use this only for scratch/template-leftover tables."),
  canonical_overrides JSON                OPTIONS(description="Optional per-entity field-name overrides for the ep_2026_cleaned union-view generator (NOT read by the capture sync itself). Shape: {\"<entity_view>\": {\"<canonical_col>\": \"<sanitized_source_col>\" | null}}. String sets/replaces the source column for that canonical column; null removes it (NULL-padded). Merge semantics mirror shift_volunteer_sync_targets.field_map_overrides."),
  enabled             BOOL      NOT NULL  OPTIONS(description="Sync skips rows where FALSE. Registration flow: insert disabled -> verify PAT access (--check-access) -> review tables (--list) -> enable."),
  registered_by       STRING              OPTIONS(description="Source identifier, free-form. e.g. 'ep-syncs seed 2026-07-23', 'ep-airtable-utilities'."),
  registered_at       TIMESTAMP NOT NULL  OPTIONS(description="When the row was first written."),
  updated_at          TIMESTAMP NOT NULL  OPTIONS(description="When any field last changed."),
  notes               STRING              OPTIONS(description="Free-form. Use for PAT-access caveats (e.g. 'PAT lacks access; owner=<org> -- pending share') and exclude_tables rationale.")
)
OPTIONS(
  description="Registry of Airtable bases captured to BigQuery by ep-syncs/sync_airtable_bases.py (typed full-replace tables + append-only JSON history in ep_2026_raw). One row per base; tables are discovered from the live base schema each run (minus exclude_tables). Written by ep-airtable-utilities at base go-live. Insert an enabled row = start capturing a base."
);

-- ---------------------------------------------------------------------------
-- Seed: the 2026 inventory known at creation time (2026-07-23), from
-- ep-airtable-utilities specs + the shift-sync registry. All seeded
-- DISABLED; enable after --check-access verifies PAT coverage and --list
-- confirms the table inventory (watch UT for template leftovers).
-- Template bases (app64MZeqXk6BuuPi, app00ZGvBKtveksbn, appTt4SsXD0lsBU6i)
-- and the OH BOE tracker (appTQh59UzvukR6rL) are deliberately NOT captured.
-- ---------------------------------------------------------------------------

INSERT INTO `proj-tmc-mem-com.ep.airtable_sync_sources`
  (name, state, base_id, base_type, bq_table_prefix, exclude_tables,
   canonical_overrides, enabled, registered_by, registered_at, updated_at, notes)
VALUES
  ('NE Field Report',           'NE', 'app4rdMLSJUpT57Ht', 'field_report', 'ne_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('PA Field Report',           'PA', 'app3EDk60ZEfTR79P', 'field_report', 'pa_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('WI Field Report',           'WI', 'appbIWiYDwdtuV5so', 'field_report', 'wi_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('UT Field Report',           'UT', 'app220U4z726HQ2DY', 'field_report', 'ut_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), 'Bespoke "Poll Monitoring Checklist" table instead of stock Checklist Submissions; may retain unused cloned template tables -- review --list output before enabling.'),
  ('MD Field Report',           'MD', 'appl4mIwgXu46SucR', 'field_report', 'md_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('MI Field Report',           'MI', 'appcfBgP8lC85htsQ', 'field_report', 'mi_field_report',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('NY Quiz',                   'NY', 'appCpQYy3YBtGLFKb', 'quiz',         'ny_quiz',                   NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('PA Quiz',                   'PA', 'appHu5TQJ0prMJncn', 'quiz',         'pa_quiz',                   NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('UT Quiz',                   'UT', 'appJFl1L0WuZR3ZpP', 'quiz',         'ut_quiz',                   NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('OR Quiz',                   'OR', 'appEyPXAVmRHUkRv2', 'quiz',         'or_quiz',                   NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('OR Drop Box Quiz',          'OR', 'app8xBuT9zyUo0S28', 'quiz',         'or_dropbox_quiz',           NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('OR Trusted Messenger Quiz', 'OR', 'appQEJiaDCKt0SKEA', 'quiz',         'or_trusted_messenger_quiz', NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('MI Poll Monitor Quiz',      'MI', 'appS6G8BSqMM1Ho2a', 'quiz',         'mi_poll_monitor_quiz',      NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL),
  ('MI Rover Quiz',             'MI', 'appwb0e1CaVbTOBFm', 'quiz',         'mi_rover_quiz',             NULL, NULL, FALSE, 'ep-syncs seed 2026-07-23', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL);

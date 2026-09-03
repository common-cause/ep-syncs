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

-- ---------------------------------------------------------------------------
-- Backfill registration, 2026-09-02.
--
-- Two things landed here at once:
--   1. Four field-report bases ep-dashboards found configured-but-uncaptured
--      (MA, RI, OH general, OH primary). MA was rendering as "not captured"
--      in a primaries funder report despite actively collecting.
--   2. Every remaining non-template quiz base the sync-operations PAT can see
--      (40 of them), on the standing decision that quiz completion is the
--      training-progress signal and is worth having in BQ whether or not a
--      dashboard reads it yet.
--
-- WHY THE ARCHIVES ARE SAFE TO CAPTURE: 3,082 of the 3,557 newly-captured
-- quiz records predate 2026. They are NOT separable at base granularity --
-- MA/MD/MN/MO reuse ONE standing base across cycles, so a single base holds
-- both 2024 and 2026 responses. Cycle selection therefore happens per ROW in
-- the ep_2026_cleaned.quiz_responses view (created_at >= 2026-01-01), added
-- in the same commit. Registering an archive base does not put its rows in
-- the 2026 interface layer; it puts them in ep_2026_raw, which is the point.
--
-- Empty bases are registered deliberately: a state reusing a dormant base is
-- exactly the failure this backfill is cleaning up, and an enabled row makes
-- that capture automatic instead of dependent on someone noticing.
--
-- MERGE (not INSERT) on base_id per the registration contract above, so
-- re-running this file is idempotent and preserves registered_at.
-- ---------------------------------------------------------------------------

MERGE `proj-tmc-mem-com.ep.airtable_sync_sources` T
USING (
  SELECT * FROM UNNEST([
    STRUCT<name STRING, state STRING, base_id STRING, base_type STRING,
           bq_table_prefix STRING, exclude_tables ARRAY<STRING>, notes STRING>
    ('MA Field Report', 'MA', 'appqtgaUrIVwIpIsV', 'field_report', 'ma_field_report',
     CAST(NULL AS ARRAY<STRING>), 'MA 2026 primary. Bespoke \'Polling Place Checklist\' table instead of stock Checklist Submissions -- added to the checklist_submissions entity table_keys in airtable_views.py in the same commit, or its 135 rows land in raw and never reach the cleaned view.'),
    ('RI Field Report', 'RI', 'app79YXggpoQPIpem', 'field_report', 'ri_field_report',
     CAST(NULL AS ARRAY<STRING>), 'RI 2026 primary (votes 09-09). Incident Reports empty at registration.'),
    ('OH General Field Report', 'OH', 'appElL9zBgVfQ94gP', 'field_report', 'oh_general_field_report',
     CAST(NULL AS ARRAY<STRING>), 'OH 2026 general. All five tables empty at registration -- capture exists so the cleaned views can tell \'collected nothing\' from \'never wired\'.'),
    ('OH Primary Field Report', 'OH', 'appVP4TefFTHTPOH4', 'field_report', 'oh_primary_field_report',
     CAST(NULL AS ARRAY<STRING>), 'OH 2026 primary. Pre-spec base; 3,145 polling places loaded.'),
    ('MA Primary Quiz 2026', 'MA', 'apptEGlKqTEpxYzdx', 'quiz', 'ma_2026_primary_quiz',
     CAST(NULL AS ARRAY<STRING>), 'MA 2026 primary certification quiz. 304 responses, all 2026.'),
    ('NM Quiz', 'NM', 'appCRnzg5MOqKySxF', 'quiz', 'nm_quiz',
     CAST(NULL AS ARRAY<STRING>), 'LIVE 2026: 51 responses May-Aug 2026. NM was documented as using neither PTV nor Airtable (hence the Asana capture in misc_jobs/asana_ep_kanban.py) -- that is wrong for quizzes. See docs/asana_nm_sync_spec.md.'),
    ('NM Quiz 2', 'NM', 'appHaQmPRR7ySUjyh', 'quiz', 'nm_quiz_2',
     CAST(NULL AS ARRAY<STRING>), 'Duplicate-named empty NM quiz base. Registered so a reuse is captured automatically rather than silently missed.'),
    ('MA Quiz', 'MA', 'apphofTflHDyWfwPt', 'quiz', 'ma_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MA quiz base, NOT a per-cycle clone: 695 responses spanning 2024-09 to 2026-09 (664 pre-2026, 31 in 2026). Cycle cannot be separated at base level -- the cleaned view\'s 2026 predicate does it per row.'),
    ('MD Quiz', 'MD', 'appiTRLmZURrfDYlq', 'quiz', 'md_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MD quiz base. 62 responses, 34 pre-2026 / 28 in 2026.'),
    ('MN Quiz', 'MN', 'appaGoPvqUNjLgePV', 'quiz', 'mn_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MN quiz base. 61 responses, 52 pre-2026 / 9 in 2026.'),
    ('MN Quiz 2', 'MN', 'app9zbiKxghWvolcR', 'quiz', 'mn_quiz_2',
     CAST(NULL AS ARRAY<STRING>), 'Second standing MN quiz base. 42 responses, 41 pre-2026 / 1 in 2026.'),
    ('MO Quiz', 'MO', 'appYd6rWC3UwNDkkh', 'quiz', 'mo_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MO quiz base. 462 responses, 427 pre-2026 / 35 in 2026.'),
    ('MO Legal Monitor Quiz', 'MO', 'appybtefyBFwRgxLG', 'quiz', 'mo_legal_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MO legal-monitor quiz. 39 responses, 36 pre-2026 / 3 in 2026.'),
    ('MO Roving Monitor Quiz', 'MO', 'app0FeBv7PdFHiGLh', 'quiz', 'mo_roving_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Standing MO roving-monitor quiz. 121 responses, 108 pre-2026 / 13 in 2026.'),
    ('MO Field Monitor Quiz', 'MO', 'appxeBwiz4MFcnpWy', 'quiz', 'mo_field_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty MO field-monitor quiz base.'),
    ('MI 2025 Poll Monitor Quiz', 'MI', 'appsXsltntbrhiIVr', 'quiz', 'mi_2025_poll_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 27 responses, all 2025.'),
    ('MI 2025 Rover Quiz', 'MI', 'appNUzr57HOjwGtlR', 'quiz', 'mi_2025_rover_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 32 responses, all 2025.'),
    ('MI Aug 2024 Poll Monitor Quiz', 'MI', 'appPBsj9kt6ExO7Ea', 'quiz', 'mi_2024aug_poll_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 40 responses, all 2024.'),
    ('MI Aug 2024 Rover Quiz', 'MI', 'appBNU7zA8cozoFwA', 'quiz', 'mi_2024aug_rover_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 10 responses, all 2024.'),
    ('MI Nov 2024 Poll Monitor Quiz', 'MI', 'appfJBlyUxkMgAr5J', 'quiz', 'mi_2024nov_poll_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 110 responses, all 2024.'),
    ('MI Nov 2024 Rover Quiz', 'MI', 'apphqcmDWqLazYCQF', 'quiz', 'mi_2024nov_rover_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 38 responses (37 in 2024, 1 in 2025).'),
    ('MI Nov 2024 Rover Quiz 2', 'MI', 'appBX8AbSJZshgUOI', 'quiz', 'mi_2024nov_rover_quiz_2',
     CAST(NULL AS ARRAY<STRING>), 'Duplicate-named empty MI rover base.'),
    ('MI Quiz', 'MI', 'appqrfUBHeyLElp4c', 'quiz', 'mi_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty standing MI quiz base.'),
    ('AZ PPE Quiz', 'AZ', 'appn7MUHZVg7GP3nx', 'quiz', 'az_ppe_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 88 responses, all 2024.'),
    ('AZ 2024 Primary Quiz', 'AZ', 'appYnsJVieUJ01mQJ', 'quiz', 'az_2024_primary_quiz',
     ['Sheet1'], 'Archive: 88 responses, all 2024. Carries an empty \'Sheet1\' import leftover -- excluded.'),
    ('CO Quiz', 'CO', 'appGVJz2eKJStqEBM', 'quiz', 'co_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 150 responses, all 2024.'),
    ('PA 2024 Quiz', 'PA', 'appKkWc6WsUrEJExg', 'quiz', 'pa_2024_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive and the largest single quiz base: 1,090 responses, all 2024. Distinct from the registered 2026 pa_quiz base.'),
    ('UT 2024 Quiz', 'UT', 'appH8fPWHHeawz9pF', 'quiz', 'ut_2024_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 32 responses, all 2024. Base name is misspelled \'Utahe Election Protection Quiz\' in Airtable -- do not \'fix\' it, the id is what matters.'),
    ('UT Roving Quiz', 'UT', 'appUxHOTmGQvDfWuW', 'quiz', 'ut_roving_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 8 responses, all 2024.'),
    ('UT Legacy Quiz', 'UT', 'appHNtD1RXQppBv4C', 'quiz', 'ut_legacy_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty pre-2026 UT quiz base (\'Utah Election Protection Quiz\').'),
    ('FL Quiz', 'FL', 'appn1FMQXWJJ5gJrC', 'quiz', 'fl_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 1 response, 2024.'),
    ('GA Quiz', 'GA', 'appuRbSbt4RIjElGZ', 'quiz', 'ga_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Archive: 3 responses, 2024. Unrelated to the GA poll-monitor Interface-page blocker (app15GgANJlyh2C6X), which registration cannot solve.'),
    ('CA Quiz', 'CA', 'appRYY6O0UpCCYRS9', 'quiz', 'ca_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('IL Quiz', 'IL', 'appq8UZtfaifIQOYG', 'quiz', 'il_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('IN Quiz', 'IN', 'appeq65B9UnHc5WrZ', 'quiz', 'in_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('NE Quiz', 'NE', 'app5UaFI5cwTVHjnh', 'quiz', 'ne_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('NE 2025 Municipal Quiz', 'NE', 'appKIfQllL5sIoEep', 'quiz', 'ne_2025_municipal_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('OH Quiz', 'OH', 'app0UrJf6TDyB4ZJc', 'quiz', 'oh_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('RI Quiz', 'RI', 'app4Up9fHXPMnmBDb', 'quiz', 'ri_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('WI Quiz', 'WI', 'appqx3wNVRqnsu8yM', 'quiz', 'wi_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Empty at registration.'),
    ('HI Test Poll Monitor Quiz', 'HI', 'appqVQ7liq9qrWQLZ', 'quiz', 'hi_test_poll_monitor_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Test base: 3 responses, 2024.'),
    ('HI Test Roving Quiz', 'HI', 'appu2ZL9aKOKyWPq3', 'quiz', 'hi_test_roving_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Test base, empty.'),
    ('HI Test Quiz 3', 'HI', 'app2JKY8lfuLT4WUN', 'quiz', 'hi_test_quiz_3',
     CAST(NULL AS ARRAY<STRING>), 'Test base, empty.'),
    ('Tabletop Quiz', 'US', 'appnFRWWYLPbGotEp', 'quiz', 'tabletop_quiz',
     CAST(NULL AS ARRAY<STRING>), 'Tabletop-exercise quiz, not a state programme -- state is \'US\', the one non-state code in this registry. Empty; if it is ever used, \'US\' shows up in the cleaned views rather than a plausible-looking wrong state.')
  ])
) S
ON T.base_id = S.base_id
WHEN MATCHED THEN UPDATE SET
  name            = S.name,
  state           = S.state,
  base_type       = S.base_type,
  bq_table_prefix = S.bq_table_prefix,
  exclude_tables  = S.exclude_tables,
  enabled         = TRUE,
  updated_at      = CURRENT_TIMESTAMP(),
  notes           = S.notes
WHEN NOT MATCHED THEN INSERT
  (name, state, base_id, base_type, bq_table_prefix, exclude_tables,
   canonical_overrides, enabled, registered_by, registered_at, updated_at, notes)
VALUES
  (S.name, S.state, S.base_id, S.base_type, S.bq_table_prefix, S.exclude_tables,
   NULL, TRUE, 'ep-syncs backfill 2026-09-02', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), S.notes);

-- ---------------------------------------------------------------------------
-- FL field report, registered 2026-09-03 on Rob's instruction.
--
-- The last base from ep-dashboards' 2026-09-02 note still uncaptured. They
-- excluded it ("CCFL sent us the export directly and we've loaded it through
-- our own state-submission path, so it's covered either way -- though
-- registering it would make it repeatable rather than a one-off"), and the
-- same-day quiz sweep did not reach it because it is a field-report base.
--
-- Registered precisely BECAUSE it was built off-process: it has no spec in
-- ep-airtable-utilities, so nothing would ever register it automatically, and
-- it goes dark again the moment ep-dashboards stops hand-loading the export.
--
-- NON-STANDARD TABLE NAMES, and this is the MA lesson repeating. The base has
-- 'Field Reports' (not 'Incident Reports') and 'Volunteers' (not 'Shifted
-- Volunteers'). Both are empty today so nothing is lost by registering now;
-- 'Polling Places' (177) and 'Checklist Submissions' (18) carry stock names
-- and map straight through. `field_reports` is added to the incident_reports
-- entity's table_keys in airtable_views.py in this same commit, so CCFL
-- filing its first report lands in the cleaned layer rather than vanishing
-- into raw. `Volunteers` is deliberately NOT mapped to shifted_volunteers:
-- 7 fields against the stock 12, and empty, so the mapping is unverifiable --
-- guessing it is exactly what the triage runbook says to escalate.
-- ---------------------------------------------------------------------------

MERGE `proj-tmc-mem-com.ep.airtable_sync_sources` T
USING (
  SELECT
    'FL Field Report'   AS name,
    'FL'                AS state,
    'appMFo7pcyrJSRI6t' AS base_id,
    'field_report'      AS base_type,
    'fl_field_report'   AS bq_table_prefix,
    'FL 2026 (March). Spun up outside the ep-airtable-utilities spec system, so nothing registers it automatically. Non-stock table names: "Field Reports" (mapped via the incident_reports table_keys) and "Volunteers" (NOT mapped -- 7 fields vs the stock 12 and empty, so the shifted_volunteers mapping is unverified; revisit when CCFL puts rows in it). At registration: 177 polling places, 18 checklist submissions, 0 field reports, 0 volunteers.' AS notes
) S
ON T.base_id = S.base_id
WHEN MATCHED THEN UPDATE SET
  name            = S.name,
  state           = S.state,
  base_type       = S.base_type,
  bq_table_prefix = S.bq_table_prefix,
  enabled         = TRUE,
  updated_at      = CURRENT_TIMESTAMP(),
  notes           = S.notes
WHEN NOT MATCHED THEN INSERT
  (name, state, base_id, base_type, bq_table_prefix, exclude_tables,
   canonical_overrides, enabled, registered_by, registered_at, updated_at, notes)
VALUES
  (S.name, S.state, S.base_id, S.base_type, S.bq_table_prefix, NULL,
   NULL, TRUE, 'ep-syncs 2026-09-03', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), S.notes);

-- Asana board registry for the Asana -> BigQuery capture sync.
--
-- Owned by ep-syncs (read at the start of each run by
-- misc_jobs/asana_ep_kanban.py). One row = one Asana project (board) captured
-- into asana_raw_2026.ep_kanban_tasks + .projects.
--
-- Inserting an enabled row is how a board's data starts landing in BQ. This is
-- the seam that makes "another state stood up its own tool" a registry insert
-- rather than a code change -- the reason the sync is registry-driven at all
-- when only one board exists today.
--
-- The registry also carries the board's CONVENTIONS, not just its identity:
-- stage_order, and where to find the volunteer's email. That is deliberate.
-- Boards outside our control encode meaning in section names and free text, so
-- when a state improves its board (e.g. moves email from the notes body into a
-- real custom field), the sync adapts via an UPDATE here instead of a code
-- change and deploy. See docs/asana_nm_sync_spec.md for the design and the
-- knowledge-library entry
-- `ep-state-deployment-tools-the-stack-is-offered-not-mandated` for why EP
-- integrates state-chosen tools at all.
--
-- REGISTRATION CONTRACT (validate BEFORE enabling a row):
--   1. PAT access: AsanaConnector().get_project(project_gid) succeeds with the
--      same PAT the job runs with (ASANA_API_KEY). Asana PATs inherit their
--      human owner's project membership, so a board the token's user isn't a
--      member of fails as "project not found" rather than a permission error.
--      If it fails, register with enabled = FALSE and a notes explaining who
--      owns the board.
--   2. stage_order: list the board's sections in pipeline order, exactly as
--      Asana spells them. A section NOT in this array lands with
--      stage_order = NULL and logs a warning -- the signal to update this row.
--      Sections not yet in use (e.g. an empty final stage) SHOULD still be
--      listed.
--   3. Grain: the sync assumes ONE TASK PER PERSON. A board organised any
--      other way (tasks as activities, sessions, or work items) must not be
--      registered with project_kind='volunteer_pipeline' -- it would land
--      garbage in ep_2026_cleaned.volunteers.
--   4. Email resolution: set email_field_name if the board has a real custom
--      field holding it. Leave email_from_notes = TRUE only while the board's
--      convention is that the notes body holds the address. If NEITHER
--      resolves, the person cannot enter ep_2026_cleaned.volunteers, which is
--      keyed on (state, email) -- capture still happens, but that volunteer is
--      invisible to downstream consumers.
--   5. Idempotency: match on project_gid -- UPDATE in place (bump updated_at,
--      preserve registered_at/registered_by) rather than inserting a duplicate.
--   6. Read-only guarantee: this sync only ever READS Asana. Registration
--      imposes zero requirements on board contents and nothing writes back to
--      a board a state team maintains by hand.
--
-- Run once to create. Re-running will fail with "Already Exists" -- safe; do
-- not change to CREATE OR REPLACE without checking rows.

CREATE TABLE `proj-tmc-mem-com.ep.asana_sync_sources` (
  name              STRING    NOT NULL  OPTIONS(description="Human-readable label, logically unique. Convention: '<State> <Purpose>' e.g. 'NM EP Volunteer Onboarding'. Used in sync logs."),
  state             STRING    NOT NULL  OPTIONS(description="Two-letter US state code. THE source of truth for state on every landed row -- ep_2026_cleaned takes state from here, never from Asana task or team fields."),
  project_gid       STRING    NOT NULL  OPTIONS(description="Asana project (board) GID, an opaque string. Logically unique -- one registry row per board. The registration contract upserts on this key."),
  workspace_gid     STRING              OPTIONS(description="Asana workspace GID. Informational (the sync addresses boards by project_gid); commoncause.org is 8446703955071."),
  project_kind      STRING    NOT NULL  OPTIONS(description="'volunteer_pipeline' = one task per volunteer, sections are pipeline stages (feeds ep_2026_cleaned.asana_pipeline and the volunteers union). 'goal_timeline' = tasks are milestones/targets, NOT people -- captured raw only, never fed into volunteers. Any other value is captured raw and ignored by the cleaned layer."),
  stage_order       ARRAY<STRING>       OPTIONS(description="The board's section names in pipeline order, spelled exactly as Asana does. Drives ep_kanban_tasks.stage_order (0-based index). A captured section missing from this array gets stage_order = NULL and a logged warning. List not-yet-used stages too."),
  email_field_name  STRING              OPTIONS(description="Name of the Asana CUSTOM FIELD holding the volunteer's email, if the board has one. Checked first when resolving parsed_email. NULL = no such field (the 2026 NM board has no custom fields at all). Setting this is how a board's convention upgrade reaches the sync without a code change."),
  email_from_notes  BOOL      NOT NULL  OPTIONS(description="When TRUE, fall back to regex-matching the first email address in the task `notes` body. Required for boards whose convention is 'the note IS the email'. Set FALSE once email_field_name is populated and backfilled, to stop silently trusting free text."),
  phone_field_name  STRING              OPTIONS(description="Name of the Asana CUSTOM FIELD holding the volunteer's phone, if the board has one. Checked first when resolving parsed_phone. Same convention-upgrade path as email_field_name."),
  phone_from_notes  BOOL      NOT NULL  OPTIONS(description="When TRUE, fall back to matching the first phone-shaped run of digits in the task `notes` body (after the email is removed). The 2026 NM board's partner-imported records use an 'email then phone' notes convention. Set FALSE for boards where free-text digits would be something other than a phone number."),
  enabled           BOOL      NOT NULL  OPTIONS(description="Sync skips rows where FALSE. Registration flow: insert disabled -> verify PAT access -> confirm stage_order and grain -> enable. Disable (don't delete) when a state retires a board; history stays queryable."),
  registered_by     STRING              OPTIONS(description="Source identifier, free-form. e.g. 'ep-syncs seed 2026-07-28'."),
  registered_at     TIMESTAMP NOT NULL  OPTIONS(description="When the row was first written."),
  updated_at        TIMESTAMP NOT NULL  OPTIONS(description="When any field last changed."),
  notes             STRING              OPTIONS(description="Free-form. Use for board conventions, PAT-access caveats, open questions with the owning state program, and anything a future reader needs in order to trust the landed data.")
)
OPTIONS(
  description="Registry of Asana boards captured into asana_raw_2026 by ep-syncs misc_jobs/asana_ep_kanban.py. One row per board; insert an enabled row to start capturing. Carries board CONVENTIONS (stage_order, email location) as data so a state improving its board is a registry UPDATE, not a code change. state on every landed row comes from here, never from Asana. READ-ONLY toward Asana. See docs/asana_nm_sync_spec.md."
);

-- Seed: NM (CCNM) EP volunteer onboarding board.
--
-- Verified live 2026-07-25: team CCNM, owner Mason Graham, 41 tasks, grain one
-- task per volunteer, ZERO custom fields, volunteer email pasted as the task
-- notes body (33 of 41), tags carrying recruitment source. The 'Deployed'
-- stage is listed but empty -- registered anyway per contract item 2.
--
-- The *_field_name columns are NULL / *_from_notes TRUE because the board has
-- no custom fields today. Question 1 to the state program is whether contact
-- info can move into real fields; if it does, UPDATE this row (set the field
-- names, then flip the from_notes flags FALSE once backfilled) -- no code
-- change needed.
--
-- phone_from_notes is TRUE because the partner-imported ('indivisible') records
-- follow an 'email then phone' notes convention -- discovered on the first
-- capture run via notes_residual, not from the board's documentation.
INSERT INTO `proj-tmc-mem-com.ep.asana_sync_sources` (
  name, state, project_gid, workspace_gid, project_kind, stage_order,
  email_field_name, email_from_notes, phone_field_name, phone_from_notes,
  enabled, registered_by, registered_at, updated_at, notes
)
VALUES (
  'NM EP Volunteer Onboarding',
  'NM',
  '1216633527817242',
  '8446703955071',
  'volunteer_pipeline',
  ['Sign Up', 'Training Completed', 'Shifted', 'Deployed'],
  NULL,
  TRUE,
  NULL,
  TRUE,
  TRUE,
  'ep-syncs seed 2026-07-28',
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP(),
  'CCNM board, owner Mason Graham (Cesar Marquez also assigned to some tasks). NM uses neither PTV deployment nor an Airtable base, so this board is the only record of its EP volunteers. No custom fields exist: email is the notes body. 8 of 41 tasks had NO email at recon and ALL 8 were in Shifted -- those volunteers cannot enter ep_2026_cleaned.volunteers (keyed on state+email). "Shifted" here is state-defined and disagreed with PTV shift records (10 tasks vs 1 confirmed), so it deliberately does NOT feed ep_2026_cleaned.shift_signups. Open questions with the program are in docs/asana_nm_sync_spec.md section 7.'
);

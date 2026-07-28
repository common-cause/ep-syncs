-- Board-metadata companion to asana_raw_2026.ep_kanban_tasks
-- (written by the same run of misc_jobs/asana_ep_kanban.py).
--
-- One row per (registered board, as_of_date) holding the board's shape:
-- section list, custom-field settings, owner, team, archived flag.
--
-- Why capture this separately: the task table records each task's CURRENT
-- stage as a bare section name. Without a snapshot of the board's own
-- structure, a renamed section looks like every task silently changing stage,
-- and a newly added custom field is invisible until someone notices values
-- appearing. Capturing the structure daily makes both legible in history --
-- and specifically means that when a state answers "yes, we'll put email in a
-- real custom field", the field's appearance shows up here on its own.
--
-- Cheap: one row per board per day.
--
-- Same idempotency and streaming-buffer caveat as the task table: the sync
-- pre-deletes today's partition for the boards it rewrites; a rerun inside
-- BQ's ~90-min streaming window can leave duplicate rows. Dedupe on
-- (as_of_date, project_gid).
--
-- No volunteer PII (board structure + CC staff names only).
--
-- IF NOT EXISTS: the sync runs this at startup so a fresh env self-heals.

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.asana_raw_2026.projects` (
  as_of_date          DATE      NOT NULL OPTIONS(description="Snapshot date (UTC). Partition key. Dedupe on (as_of_date, project_gid)."),
  project_gid         STRING    NOT NULL OPTIONS(description="Asana project GID. Matches ep.asana_sync_sources.project_gid and ep_kanban_tasks.project_gid."),
  project_name        STRING             OPTIONS(description="Board name as read from Asana at capture time -- compare across snapshots to catch renames."),
  state               STRING             OPTIONS(description="Two-letter USPS code from the REGISTRY row, never from Asana."),
  workspace_gid       STRING             OPTIONS(description="Asana workspace GID (commoncause.org is 8446703955071)."),
  team_name           STRING             OPTIONS(description="Asana team the board belongs to (e.g. 'CCNM'). Often the only place a board's owning state program is recorded inside Asana -- which is why we take state from our registry instead."),
  owner_name          STRING             OPTIONS(description="Board owner's name. The person to talk to about board conventions."),
  archived            BOOL               OPTIONS(description="Asana's archived flag. A board flipping to TRUE is the signal that a state retired it -- the sync keeps capturing until the registry row is disabled."),
  project_created_at  TIMESTAMP          OPTIONS(description="When the board was created."),
  project_modified_at TIMESTAMP          OPTIONS(description="When the board was last modified."),
  project_notes       STRING             OPTIONS(description="The board's description text, verbatim. Sometimes states document their own stage conventions here."),
  section_names       ARRAY<STRING>      OPTIONS(description="Section names in Asana's own display order -- the board's pipeline as it actually stands. Compare against ep.asana_sync_sources.stage_order to detect stages the registry doesn't know about."),
  sections_json       JSON               OPTIONS(description="Full section objects (gid + name) verbatim, so a rename is traceable by GID rather than guessed from position."),
  custom_fields_json  JSON               OPTIONS(description="The board's custom_field_settings verbatim: field name, type, and enum options where applicable. Empty array on a board with no custom fields. Watch this for a board gaining structured fields."),
  n_custom_fields     INT64              OPTIONS(description="Convenience count of custom fields defined on the board. 0 means every meaningful value on that board lives in free text, tags, or section membership.")
)
PARTITION BY as_of_date
CLUSTER BY project_gid
OPTIONS(
  description="Daily structural snapshots of registered Asana EP boards (sections, custom-field settings, owner, team, archived), written by ep-syncs misc_jobs/asana_ep_kanban.py alongside asana_raw_2026.ep_kanban_tasks. Makes section renames and newly added custom fields legible in history instead of appearing as silent stage churn. Dedupe on (as_of_date, project_gid). No volunteer PII. See docs/asana_nm_sync_spec.md."
);

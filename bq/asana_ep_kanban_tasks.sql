-- Raw landing table for the Asana EP volunteer-pipeline sync
-- (misc_jobs/asana_ep_kanban.py).
--
-- One row per (Asana task, as_of_date) for every board registered in
-- ep.asana_sync_sources. The 2026 first case is CCNM's "EP Volunteer
-- Onboarding Kanban", where the grain is one task per VOLUNTEER and the
-- board's sections are the pipeline stages. READ-ONLY toward Asana -- these
-- boards are maintained by hand by state staff and nothing here writes back.
--
-- Why this table exists: NM uses neither PTV deployment nor an Airtable base,
-- so its tracked EP volunteers were invisible to every ep_2026_cleaned
-- consumer. See docs/asana_nm_sync_spec.md and the knowledge-library entry
-- `ep-state-deployment-tools-the-stack-is-offered-not-mandated`.
--
-- Append-only: one full snapshot per board per as_of_date, mirroring
-- ptv_raw_2026.users / .shift_volunteers. The sync pre-deletes today's
-- partition for the boards it is about to (re)write, so a same-day rerun is
-- idempotent EXCEPT when BigQuery's streaming buffer blocks the DELETE (rows
-- streamed in the last ~90 min); in that window a rerun can leave exact-
-- duplicate rows. Consumers MUST dedupe on (as_of_date, task_gid) --
-- ep_2026_cleaned.asana_pipeline does this with ROW_NUMBER.
--
-- Snapshots are also the ONLY source of stage-transition history: Asana
-- exposes a task's CURRENT section and nothing else (moves live in the
-- Stories API, deliberately out of AsanaConnector v1 scope). Derive
-- transitions with LAG(stage) OVER (PARTITION BY task_gid ORDER BY
-- as_of_date) -- available from first capture forward, never retroactively.
--
-- PII: `parsed_email`, `task_name` (a volunteer's full name on pipeline
-- boards), and `notes_residual` are row-level PII. This table lives in
-- access-controlled BigQuery and must never be exported to git or shared
-- corpora un-masked. `assignee_*` is CC staff, not a volunteer.
--
-- IF NOT EXISTS: the sync runs this at startup so a fresh Civis container
-- self-heals. Editing columns here does NOT alter an existing table -- ALTER
-- deliberately (a schema change is a coordinated migration).

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.asana_raw_2026.ep_kanban_tasks` (
  as_of_date        DATE      NOT NULL OPTIONS(description="Snapshot date (UTC) this row was captured. Partition key. Append-only, one full per-board snapshot per day."),
  task_gid          STRING    NOT NULL OPTIONS(description="Asana task GID (opaque string, never an int). The only stable identity for a board row -- task names and sections both change. Dedupe key within a snapshot: (as_of_date, task_gid)."),
  project_gid       STRING    NOT NULL OPTIONS(description="Asana project GID of the board this task was captured from. Matches ep.asana_sync_sources.project_gid."),
  project_name      STRING             OPTIONS(description="Board name as read from Asana at capture time (not the registry label) -- so a rename is visible in history."),
  state             STRING             OPTIONS(description="Two-letter USPS code from the REGISTRY row, never from task fields. House convention: ep_2026_cleaned takes state from our own registration, not from source records."),
  task_name         STRING             OPTIONS(description="Asana task name verbatim. On volunteer-pipeline boards this is the volunteer's FULL NAME as one string (PII) -- ep_2026_cleaned.asana_pipeline derives a best-effort first/last split. Kept unsplit here to stay faithful to the source."),
  stage             STRING             OPTIONS(description="Section name within THIS project (from memberships[project=this].section.name) -- the pipeline stage. NULL if the task somehow has no section membership in the board."),
  stage_gid         STRING             OPTIONS(description="Section GID, so a renamed stage is still trackable across snapshots."),
  stage_order       INT64              OPTIONS(description="0-based index of `stage` in the registry's declared stage_order array. NULL when the section is NOT in that array -- i.e. someone added a stage the registry doesn't know about. NULL here is the signal to update the registry; the sync logs a warning."),
  source_tags       STRING             OPTIONS(description="Asana tag names, sorted and comma-joined ('' when untagged). On the NM board these read as recruitment source ('jotform', 'indivisible', 'PTV'), which is why they are captured -- but they are free-form tags, not a validated enum."),
  parsed_email      STRING             OPTIONS(description="Volunteer email, normalized (TRIM+LOWER, blank->NULL) to satisfy the ep_2026_cleaned.norm_email contract on arrival. Resolved in registry-declared priority: the custom field named email_field_name if populated, else the first email matched in `notes` when email_from_notes. NULL when neither yields one -- those people CANNOT enter ep_2026_cleaned.volunteers, which is keyed on (state, email). PII."),
  email_source      STRING             OPTIONS(description="Where parsed_email came from: 'custom_field' | 'notes' | NULL (none found). Lets a consumer tell a structured value from a free-text scrape, and shows convention migration progress."),
  notes_had_email   BOOL               OPTIONS(description="Whether an email was resolved at all. The headline data-quality signal for a board: FALSE means this volunteer is invisible downstream."),
  parsed_phone      STRING             OPTIONS(description="Volunteer phone, normalized to the ep_2026_cleaned.norm_phone contract (strip non-digits, keep last 10, blank->NULL). Resolved like parsed_email: the custom field named phone_field_name if populated, else the first phone-shaped run of digits in `notes` when phone_from_notes. On the 2026 NM board the partner-imported records use an 'email then phone' notes convention -- found because notes_residual flagged it on the first run. PII."),
  phone_source      STRING             OPTIONS(description="Where parsed_phone came from: 'custom_field' | 'notes' | NULL (none found)."),
  notes_residual    STRING             OPTIONS(description="The `notes` body with the matched email AND phone removed, trimmed. Expected to be '' on boards whose convention is fully understood. NON-EMPTY MEANS THE CONVENTION DRIFTED (or holds data we aren't capturing yet) and the parser may need revisiting -- watch it rather than discovering breakage months later. This is how the NM board's phone convention was found: it showed up as residual on 12 of 42 tasks. May contain PII."),
  assignee_name     STRING             OPTIONS(description="Assigned CC staffer's name. Board owner/organizer, NOT the volunteer."),
  assignee_email    STRING             OPTIONS(description="Assigned CC staffer's email. NOT the volunteer."),
  completed         BOOL               OPTIONS(description="Asana's task-complete flag, verbatim. WARNING: semantically ambiguous on pipeline boards -- on the NM board it was set on only 2 of 41 tasks while 10 sat in the 'Shifted' stage, so it does not mean 'finished the pipeline'. Do not build metrics on it without confirming the board's convention."),
  completed_at      TIMESTAMP          OPTIONS(description="When `completed` was set."),
  due_on            DATE               OPTIONS(description="Task due date. On pipeline boards this is often used as a follow-up date rather than a real deadline -- interpret per board."),
  start_on          DATE               OPTIONS(description="Task start date. Rarely populated on pipeline boards."),
  num_subtasks      INT64              OPTIONS(description="Subtask count. The sync does NOT descend into subtasks (pipeline boards are flat); a non-zero value here means a board is being used in a way the flattening does not capture."),
  task_created_at   TIMESTAMP          OPTIONS(description="When the Asana task was created = when the person was added to the BOARD. NOT a signup or recruitment timestamp -- an imported partner list all lands on its import date. ep_2026_cleaned.volunteers exposes this as joined_at with that caveat documented."),
  task_modified_at  TIMESTAMP          OPTIONS(description="Last modification to the task. Any field, including a stage move; not a stage-transition timestamp on its own."),
  custom_fields_json JSON              OPTIONS(description="The task's full `custom_fields` array verbatim (empty array when the board defines none). Carried raw so a board that later adds real fields is captured with NO schema migration -- read with JSON_VALUE / JSON_QUERY_ARRAY. Note JSON columns cannot SELECT DISTINCT (dedupe with ROW_NUMBER)."),
  permalink_url     STRING             OPTIONS(description="Direct Asana link to the task, for ops click-through from a report.")
)
PARTITION BY as_of_date
CLUSTER BY project_gid, parsed_email
OPTIONS(
  description="Raw per-(task, day) snapshots of registered Asana EP boards, captured by ep-syncs misc_jobs/asana_ep_kanban.py. Append-only, one full per-board snapshot per as_of_date; dedupe on (as_of_date, task_gid). 2026 first case: CCNM's volunteer-onboarding Kanban, grain one task per volunteer, sections as pipeline stages. READ-ONLY toward Asana. Snapshots are the only source of stage-transition history (Asana exposes current section only). PII (parsed_email, task_name, notes_residual); access-controlled. See docs/asana_nm_sync_spec.md."
);

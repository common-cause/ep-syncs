-- ep_2026_cleaned.volunteer_activity — one volunteer, whole story.
--
-- Long/tall event stream across every 2026 EP touchpoint: PTV
-- registration, shift claims, quiz submissions, checklist submissions,
-- incident reports, Airtable self-adds, and state Asana board activity.
-- Pure UNION ALL over the other ep_2026_cleaned views — zero new logic, so
-- it always agrees with them.
--
-- Grain: one row per (source_system, source_ref/event) — NOT deduped per
-- person. Join/aggregate on (state, email). NULL-email events are
-- excluded (identity-keyed stream).
--
-- Note: rows reflect the sources' CURRENT state (views, not an immutable
-- log). A self_added event disappears if that person later registers in
-- PTV (they flip to a ptv_registered event); Airtable-derived events
-- disappear if the record is deleted in Airtable.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.volunteer_activity`
OPTIONS(description="Long/tall 2026 EP volunteer event stream: ptv_registered | shift_claimed | quiz_submitted | checklist_submitted | incident_reported | self_added | asana_board_added | asana_stage_* , one row per event, keyed by normalized (state, email). asana_stage_* events carry a state's OWN board vocabulary (namespaced deliberately — those stages are state-defined and do not map onto PTV shift semantics) and are dated by task modification, the closest proxy Asana exposes for a stage change. Pure union over the other ep_2026_cleaned views (always agrees with them; reflects current state, not an immutable log). detail is a short human label per event type (source code, location, score, category).")
AS
SELECT
  state,
  email,
  'ptv_registered'                       AS event_type,
  joined_at                              AS event_at,
  DATE(joined_at)                        AS event_date,
  'ptv'                                  AS source_system,
  CAST(ptv_id AS STRING)                 AS source_ref,
  source_code                            AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.volunteers`
WHERE in_ptv AND joined_at IS NOT NULL

UNION ALL

SELECT
  state,
  email,
  'self_added'                           AS event_type,
  joined_at                              AS event_at,
  DATE(joined_at)                        AS event_date,
  'airtable'                             AS source_system,
  CAST(NULL AS STRING)                   AS source_ref,
  'emergency self-add form'              AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.volunteers`
WHERE source_system = 'airtable_self_add' AND joined_at IS NOT NULL

UNION ALL

SELECT
  state,
  email,
  'asana_board_added'                    AS event_type,
  joined_at                              AS event_at,
  DATE(joined_at)                        AS event_date,
  'asana'                                AS source_system,
  CAST(NULL AS STRING)                   AS source_ref,
  CONCAT('added to state Asana board',
         COALESCE(CONCAT(' (', source_code, ')'), '')) AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.volunteers`
WHERE source_system = 'asana' AND joined_at IS NOT NULL

UNION ALL

-- Current pipeline stage on a state Asana board, as an event.
--
-- Board-stage semantics are STATE-DEFINED, so the event_type is namespaced
-- (asana_stage_*) rather than mapped onto ptv/shift vocabulary. Notably NM's
-- 'Shifted' stage disagreed with PTV shift records at go-live (10 board cards
-- vs 1 confirmable PTV shift), which is why it deliberately does NOT feed
-- shift_signups or produce a shift_claimed event.
--
-- event_at is the task's last modification, the closest available proxy for
-- "when it reached this stage" -- Asana exposes no stage-transition timestamp
-- (that lives in the Stories API). For true transitions, diff snapshots:
-- LAG(stage) OVER (PARTITION BY task_gid ORDER BY as_of_date) on
-- asana_raw_2026.ep_kanban_tasks.
SELECT
  state,
  email,
  CONCAT('asana_stage_',
         LOWER(REGEXP_REPLACE(stage, r'[^A-Za-z0-9]+', '_'))) AS event_type,
  board_modified_at                      AS event_at,
  DATE(board_modified_at)                AS event_date,
  'asana'                                AS source_system,
  task_gid                               AS source_ref,
  CONCAT(board_name, ': ', stage)        AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.asana_pipeline`
WHERE email IS NOT NULL AND stage IS NOT NULL

UNION ALL

SELECT
  state,
  email,
  'shift_claimed'                        AS event_type,
  claimed_at                             AS event_at,
  COALESCE(DATE(claimed_at), shift_date) AS event_date,
  'ptv'                                  AS source_system,
  CAST(shift_id AS STRING)               AS source_ref,
  location                               AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.shift_signups`
WHERE email IS NOT NULL

UNION ALL

SELECT
  state,
  email,
  'quiz_submitted'                       AS event_type,
  created_at                             AS event_at,
  DATE(created_at)                       AS event_date,
  'airtable'                             AS source_system,
  record_id                              AS source_ref,
  CONCAT(base_key,
         IF(score IS NOT NULL,
            CONCAT(': ', CAST(score AS STRING), '/', CAST(score_max AS STRING)),
            ''))                         AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.quiz_responses`
WHERE email IS NOT NULL

UNION ALL

SELECT
  state,
  volunteer_email                        AS email,
  'checklist_submitted'                  AS event_type,
  created_at                             AS event_at,
  DATE(created_at)                       AS event_date,
  'airtable'                             AS source_system,
  record_id                              AS source_ref,
  polling_place                          AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.checklist_submissions`
WHERE volunteer_email IS NOT NULL

UNION ALL

SELECT
  state,
  volunteer_email                        AS email,
  'incident_reported'                    AS event_type,
  created_at                             AS event_at,
  DATE(created_at)                       AS event_date,
  'airtable'                             AS source_system,
  record_id                              AS source_ref,
  category                               AS detail
FROM `proj-tmc-mem-com.ep_2026_cleaned.incident_reports`
WHERE volunteer_email IS NOT NULL;

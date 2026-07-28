-- ep_2026_cleaned.asana_pipeline — state Asana boards as they stand today.
--
-- Grain: one row per TASK in the latest captured snapshot of each registered
-- board whose project_kind = 'volunteer_pipeline' (one task per volunteer,
-- sections as pipeline stages). The Asana analogue of the generated
-- shifted_volunteers view: a source-shaped entity view that the canonical
-- contracts build on.
--
-- Why this exists: Common Cause offers PTV and the Airtable templates to state
-- EP programs but does not require them. NM runs volunteer onboarding on an
-- Asana board and uses neither, so without this its volunteers appear nowhere
-- in the interface layer. See docs/asana_nm_sync_spec.md and the KL entry
-- `ep-state-deployment-tools-the-stack-is-offered-not-mandated`.
--
-- Contract notes:
--   - email / phone are ALREADY normalized in the landing table (the sync
--     applies the norm_email / norm_phone rules on arrival), so they satisfy
--     the layer's identity contract without re-normalizing here.
--   - Rows with a NULL email are RETAINED here on purpose. They cannot enter
--     `volunteers` (grain (state, email)), and this view is where you can see
--     who is being lost -- filter `email IS NOT NULL` to match roster scope.
--   - first_name / last_name are a BEST-EFFORT split of the task title
--     (first whitespace-delimited token, then the remainder). These boards
--     carry one display-name string, so multi-word given names, particles and
--     suffixes will land wrong. full_name is the faithful value; prefer it.
--   - stage_order is NULL when the board has a section the registry's
--     stage_order array doesn't list (the sync logs a warning) -- treat NULL
--     as "unranked", never as "first".
--   - Current snapshot only. For stage transitions use the raw table:
--     LAG(stage) OVER (PARTITION BY task_gid ORDER BY as_of_date).
--
-- Dedupes on (as_of_date, task_gid): a same-day rerun inside BigQuery's
-- ~90-minute streaming-buffer window can leave duplicate rows, since the
-- sync's pre-delete cannot succeed in that window.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.asana_pipeline`
OPTIONS(description="Registered state Asana EP boards as of their latest capture: one row per task (= one volunteer on 'volunteer_pipeline' boards), with pipeline stage, recruitment source tags, and normalized email/phone. Source for the source_system='asana' branch of ep_2026_cleaned.volunteers. state comes from ep.asana_sync_sources, never from Asana. NULL-email rows are RETAINED here (they cannot enter volunteers, which is keyed on (state,email)) -- filter email IS NOT NULL for roster scope. first_name/last_name are a best-effort split of a single display-name string; full_name is faithful. stage_order NULL = section not listed in the registry's stage_order. Current snapshot only; derive transitions from asana_raw_2026.ep_kanban_tasks with LAG(stage) over as_of_date. Freshness: check sync_health source='asana'.")
AS
WITH reg AS (
  SELECT project_gid, name AS board_name, project_kind
  FROM `proj-tmc-mem-com.ep.asana_sync_sources`
  WHERE enabled AND project_kind = 'volunteer_pipeline'
),
latest AS (
  SELECT t.project_gid, MAX(t.as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.asana_raw_2026.ep_kanban_tasks` t
  JOIN reg USING (project_gid)
  GROUP BY t.project_gid
)
SELECT
  t.state,
  t.parsed_email                                            AS email,
  -- No distinct as-delivered form is retained for Asana (the sync normalizes
  -- on arrival, and nothing upserts back into Asana), so email_raw mirrors
  -- email. Present for union-compatibility with the PTV/Airtable branches.
  t.parsed_email                                            AS email_raw,
  t.parsed_phone                                            AS phone,
  t.task_name                                               AS full_name,
  NULLIF(TRIM(REGEXP_EXTRACT(t.task_name, r'^(\S+)')), '')  AS first_name,
  NULLIF(TRIM(REGEXP_REPLACE(t.task_name, r'^\S+\s*', '')), '') AS last_name,
  t.stage,
  t.stage_order,
  t.source_tags,
  t.notes_had_email,
  t.email_source,
  t.phone_source,
  t.completed                                               AS task_completed,
  t.due_on,
  t.task_created_at                                         AS added_to_board_at,
  t.task_modified_at                                        AS board_modified_at,
  t.assignee_name                                           AS staff_assignee,
  r.board_name,
  t.project_gid,
  t.project_name,
  t.task_gid,
  t.permalink_url,
  t.as_of_date
FROM `proj-tmc-mem-com.asana_raw_2026.ep_kanban_tasks` t
JOIN reg r USING (project_gid)
JOIN latest l
  ON l.project_gid = t.project_gid AND l.as_of_date = t.as_of_date
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY t.as_of_date, t.task_gid
  ORDER BY t.task_modified_at DESC
) = 1;

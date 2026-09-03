-- Ledger of every Airtable base the sync-operations PAT can see, and when we
-- first saw it. Written nightly by misc_jobs/airtable_base_visibility.py.
--
-- WHY THIS EXISTS
--
-- Capture registration is now automatic for bases built through
-- ep-airtable-utilities' spec system: its register_capture_source.py writes
-- ep.airtable_sync_sources at go-live, and audit_capture_registration.py
-- asserts every spec has a row. That audit walks SPECS -> REGISTRY, so it is
-- structurally blind to a base that exists in Airtable with no spec -- a state
-- cloning the quiz template by hand, a prior-cycle base, or a staffer spinning
-- one up off-process. Forty such bases were found in a one-off sweep on
-- 2026-09-02, some collecting live.
--
-- The other direction has no owner either. A PAT is granted access to a base
-- at some point by some person, and that grant is SILENT: whoever grants it
-- has no idea BigQuery capture is a separate step and will not mention it.
-- Amy's FL field-report base (appMFo7pcyrJSRI6t) is the worked example -- it
-- became reachable in the week of 2026-09-01 and nothing anywhere noticed.
--
-- So this table is the baseline nobody had. On 2026-09-02 the question "was
-- this base visible to us last month?" was unanswerable for every one of the
-- 192 bases, because the list had never once been written down. From the first
-- sweep on, it is answerable forever.
--
-- WHAT IT IS NOT: a discovery mechanism. list_bases() returns what the PAT has
-- been GRANTED. A base nobody has shared with us is invisible here no matter
-- how often this runs, and the only thing that finds those is asking humans
-- what they are using -- the general-election canvass. This table's job is to
-- make sure that when a grant does land, it does not sit unnoticed: the
-- canvass finds the bases, this catches them.
--
-- GRAIN: one row per base_id, forever. Rows are never deleted -- a base that
-- stops being visible keeps its row with a stale last_seen_date, which is how
-- lost access becomes detectable instead of becoming absence.
--
-- Run once to create. Re-running fails with "Already Exists" -- safe.

CREATE TABLE `proj-tmc-mem-com.ep.airtable_base_visibility` (
  base_id          STRING    NOT NULL OPTIONS(description="Airtable base ID (app...). Primary key; one row per base, forever."),
  base_name        STRING             OPTIONS(description="Base name as of the most recent sweep that saw it. Airtable names are user-editable and DO drift -- this is the latest observation, not an identity. Never join on it."),
  permission_level STRING             OPTIONS(description="permissionLevel the PAT reports for this base ('create' | 'edit' | 'comment' | 'read'). All 192 bases read 'create' as of 2026-09-02, which is consistent with workspace-scoped token access rather than per-base grants."),
  first_seen_date  DATE      NOT NULL OPTIONS(description="ET date of the first sweep that saw this base. THE point of the table: first_seen_date = the latest sweep date means the base appeared since the last run, which means someone granted access (or created it in a workspace we already hold)."),
  last_seen_date   DATE      NOT NULL OPTIONS(description="ET date of the most recent sweep that saw this base. Behind the latest sweep date = the PAT can no longer read it. For a base with an enabled registry row that is an outage, not a curiosity."),
  triage_status    STRING    NOT NULL OPTIONS(description="'pending' (default, awaiting a decision) | 'ignored' (reviewed, deliberately not captured -- this is the mute, and the ONLY status the triage pass must write for a base it declines) | 'needs_review' (triage could not decide; escalated to Rob) | 'registered' (bookkeeping only -- the queue view derives registration from ep.airtable_sync_sources, so this is never load-bearing)."),
  triage_notes     STRING             OPTIONS(description="Why this verdict. Required in practice for 'ignored' and 'needs_review': an unexplained mute is indistinguishable from a base someone forgot about, which is the failure this table exists to prevent."),
  triaged_by       STRING             OPTIONS(description="Who decided. 'agent:<YYYY-MM-DD>-base-triage' for a dispatch pass, an email for a human. A human verdict is never overwritten by an agent -- see the dispatch contract's guards."),
  triaged_at       TIMESTAMP          OPTIONS(description="When the verdict was recorded."),
  created_at       TIMESTAMP NOT NULL OPTIONS(description="When this row was inserted (first sighting)."),
  updated_at       TIMESTAMP NOT NULL OPTIONS(description="When any observation column last changed.")
)
OPTIONS(
  description="Append-and-update ledger of Airtable bases visible to the sync-operations PAT, with first/last sighting and a triage verdict. Written nightly by misc_jobs/airtable_base_visibility.py; read by ep.v_airtable_base_triage_queue. Rows are never deleted -- a base that stops being visible keeps its row so lost access is detectable rather than silent."
);


-- ---------------------------------------------------------------------------
-- The detection surface. The dispatch contract points at THIS, not at the
-- table -- a triage pass that read the raw table would have to re-implement
-- the "what counts as needing attention" rule, and two copies of that rule is
-- how a queue silently stops surfacing a category.
--
-- Deliberately NOT a member of the queue: a base with a registry row. Whether
-- a base is registered is derived live from ep.airtable_sync_sources, so
-- registering one is what removes it from the queue. triage_status is never
-- consulted for that -- it exists to MUTE ('ignored') or ESCALATE
-- ('needs_review'), nothing else. This means a base cannot be dropped from the
-- queue by forgetting to update a status column.
--
-- Hand-applied (like bq/v_shift_volunteers_current.sql). apply_bq_views.py is
-- hardcoded to ep_2026_cleaned and does not cover the `ep` dataset.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep.v_airtable_base_triage_queue`
OPTIONS(description="Airtable bases needing a human or agent decision. reason: 'new_unregistered' (visible, no registry row, not muted -- someone granted access or built a base off-process), 'access_lost' (enabled registry row but the PAT can no longer read it -- capture for this base is failing or about to), 'needs_review' (a prior triage pass escalated it). Empty is the healthy steady state. Detection surface for the airtable-base-triage dispatch task type.")
AS
WITH latest AS (
  SELECT MAX(last_seen_date) AS sweep_date
  FROM `proj-tmc-mem-com.ep.airtable_base_visibility`
),
v AS (
  SELECT b.*, l.sweep_date, b.last_seen_date = l.sweep_date AS is_visible
  FROM `proj-tmc-mem-com.ep.airtable_base_visibility` b
  CROSS JOIN latest l
),
reg AS (
  SELECT base_id, name AS registry_name, enabled, bq_table_prefix
  FROM `proj-tmc-mem-com.ep.airtable_sync_sources`
)
-- Newly visible and unregistered: the canvass caught something, or someone
-- built off-process. 'ignored' mutes; a registry row removes it entirely.
SELECT
  'new_unregistered' AS reason,
  v.base_id,
  v.base_name,
  v.first_seen_date,
  v.last_seen_date,
  v.sweep_date,
  DATE_DIFF(v.sweep_date, v.first_seen_date, DAY) AS days_known,
  v.triage_status,
  v.triage_notes,
  CAST(NULL AS STRING) AS bq_table_prefix
FROM v
LEFT JOIN reg ON reg.base_id = v.base_id
WHERE v.is_visible
  AND reg.base_id IS NULL
  AND v.triage_status NOT IN ('ignored', 'needs_review')

UNION ALL

-- Registered and enabled, but the PAT stopped being able to read it. The
-- nightly capture will be failing this base; an unregistered base going dark
-- is invisible, which is why this is worth its own reason.
SELECT
  'access_lost' AS reason,
  v.base_id,
  v.base_name,
  v.first_seen_date,
  v.last_seen_date,
  v.sweep_date,
  DATE_DIFF(v.sweep_date, v.last_seen_date, DAY) AS days_known,
  v.triage_status,
  v.triage_notes,
  reg.bq_table_prefix
FROM v
JOIN reg ON reg.base_id = v.base_id
WHERE NOT v.is_visible
  AND reg.enabled

UNION ALL

-- Escalated by a prior pass. Stays until a human resolves it, so an
-- unresolved escalation cannot age out of sight.
SELECT
  'needs_review' AS reason,
  v.base_id,
  v.base_name,
  v.first_seen_date,
  v.last_seen_date,
  v.sweep_date,
  DATE_DIFF(v.sweep_date, v.first_seen_date, DAY) AS days_known,
  v.triage_status,
  v.triage_notes,
  reg.bq_table_prefix
FROM v
LEFT JOIN reg ON reg.base_id = v.base_id
WHERE v.triage_status = 'needs_review'
  AND reg.base_id IS NULL;

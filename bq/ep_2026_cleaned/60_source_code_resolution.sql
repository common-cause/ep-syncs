-- One row per recruitment source code, assembling every fact needed to decide
-- what to do about it -- and a `needs_action` verdict saying whether a human
-- has to.
--
-- THE PROBLEM THIS SURFACES
-- A source code exists in three places that never talk to each other:
--   1. ISSUED     -- ep_2026_raw.partner_source_codes: what a partner was told
--                    to use (the engagement form). The only record of the
--                    issuing side.
--   2. IDENTIFIED -- ep_dashboards.source_code_map: which org a code belongs
--                    to, plus channel and is_external. Authored by the nightly
--                    source-code triage pass over live PTV traffic.
--   3. CAPTURED   -- ep.volunteer_sheet_targets: which codes feed which partner
--                    export sheet.
-- When they disagree the partner's volunteers land in no sheet at all, with no
-- error anywhere: nothing selects the rows, so nothing fails. `pbvrc` ran that
-- way for weeks with 43 volunteers before 2026-08-26.
--
-- DELIBERATE CROSS-PROJECT DEPENDENCY on ep_dashboards.source_code_map. Org
-- identity is authored there (that project's dispatch contract states it
-- supersedes ep_archive.source_codes.external) and is NOT re-derived here --
-- one question, one authority. This view consumes it and adds the two facts
-- ep-dashboards cannot see: what was issued, and what is captured.
--
-- Note the asymmetry it exposes both ways: the map's detection surface is live
-- PTV traffic, so an issued-but-unused code is invisible to it, while the form
-- often names an org the map has sitting in needs_review (`htff`, `pbvrc`).
-- Rows where `map_org` is unknown but `issued_to_org` is not are free wins --
-- evidence the triage pass has never been shown.
--
-- READ THIS VIEW, NOT THE RAW TABLES: the ?source= parse lives here, so a parse
-- fix is a view change and never needs a re-scrape.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.source_code_resolution`
OPTIONS(
  description="One row per recruitment source code, reconciling what was ISSUED to a partner (engagement form) against what is IDENTIFIED (ep_dashboards.source_code_map) and what is CAPTURED (ep.volunteer_sheet_targets), with a needs_action verdict. Drives the recurring human review of ambiguous and uncaptured codes. Ordered so the highest-volume unresolved codes sort first."
)
AS
WITH issued AS (
  SELECT
    LOWER(TRIM(COALESCE(
      NULLIF(TRIM(source_code_raw), ''),
      REGEXP_EXTRACT(source_url, r'[?&][Ss]ource=([^&#[:space:]]+)')
    ))) AS source_code,
    -- Prefer the curated summary tab's org label; it is the deduped one.
    ANY_VALUE(org_label)        AS issued_to_org,
    ANY_VALUE(source_url)       AS issued_url,
    ANY_VALUE(spreadsheet_link) AS issued_spreadsheet_link,
    MAX(submitted_at)           AS form_submitted_at,
    STRING_AGG(DISTINCT source_tab ORDER BY source_tab) AS issued_seen_on
  FROM `proj-tmc-mem-com.ep_2026_raw.partner_source_codes`
  WHERE as_of_date = (
    SELECT MAX(as_of_date) FROM `proj-tmc-mem-com.ep_2026_raw.partner_source_codes`
  )
  GROUP BY source_code
),
live AS (
  SELECT
    LOWER(TRIM(source_code)) AS source_code,
    COUNT(*)                                        AS volunteers,
    STRING_AGG(DISTINCT state ORDER BY state LIMIT 8) AS states,
    MIN(DATE(join_date))                            AS first_signup,
    MAX(DATE(join_date))                            AS last_signup
  FROM `proj-tmc-mem-com.ptv_raw_2026.v_users_current`
  WHERE source_code IS NOT NULL AND TRIM(source_code) != ''
  GROUP BY source_code
),
identified AS (
  -- The map carries case variants as separate rows; fold them and keep a
  -- flag when the variants disagree, rather than silently picking one.
  SELECT
    LOWER(TRIM(source_code))     AS source_code,
    ANY_VALUE(org)               AS map_org,
    ANY_VALUE(channel)           AS map_channel,
    ANY_VALUE(is_external)       AS map_is_external,
    ANY_VALUE(review_status)     AS map_review_status,
    COUNT(DISTINCT org) > 1      AS map_variants_disagree
  FROM `proj-tmc-mem-com.ep_dashboards.source_code_map`
  GROUP BY source_code
),
captured AS (
  SELECT
    LOWER(TRIM(code))      AS source_code,
    ANY_VALUE(target_key)  AS sheet_target_key,
    ANY_VALUE(sheet_title) AS sheet_title
  FROM `proj-tmc-mem-com.ep.volunteer_sheet_targets`, UNNEST(source_codes) AS code
  WHERE target_type = 'source_code' AND enabled
  GROUP BY source_code
),
all_codes AS (
  SELECT source_code FROM issued     WHERE source_code IS NOT NULL AND source_code != ''
  UNION DISTINCT
  SELECT source_code FROM live
  UNION DISTINCT
  SELECT source_code FROM captured
)
SELECT
  a.source_code,
  IFNULL(l.volunteers, 0)          AS volunteers,
  l.states,
  l.first_signup,
  l.last_signup,

  i.issued_to_org,
  i.issued_url,
  i.issued_spreadsheet_link,
  i.form_submitted_at,
  i.issued_seen_on,

  d.map_org,
  d.map_channel,
  d.map_is_external,
  d.map_review_status,
  d.map_variants_disagree,

  c.sheet_target_key,
  c.sheet_title,

  CASE
    -- Not a partner recruitment code at all: CC's own codes, email blasts,
    -- paid acquisition, the previous-years pool marker, test rows.
    WHEN d.map_channel IN ('email_campaign', 'cc_direct', 'paid', 'pool_marker', 'test')
      THEN 'none - not a partner code'

    -- Free win: the engagement form names the org the triage pass could not.
    -- No judgment required, just evidence that has never been joined up.
    --
    -- The equality test is load-bearing, not defensive. Where a code's org is
    -- unknown, the honest placeholder is the code itself -- rows were added to
    -- the Source Codes tab on 2026-08-26 reading `imc`, `aatbfl`, `corazonaz`,
    -- `poder` in the org column for exactly that reason. Without this test the
    -- view reads those placeholders back as partner identities and reports the
    -- unknown codes as solved, which is worse than not knowing: the queue would
    -- launder its own ignorance into an answer. A label that merely restates
    -- the code tells a human nothing, so it is not identity.
    WHEN IFNULL(d.map_org, 'unknown') = 'unknown'
         AND i.issued_to_org IS NOT NULL
         AND REGEXP_REPLACE(LOWER(TRIM(i.issued_to_org)), r'[^a-z0-9]', '')
             != REGEXP_REPLACE(a.source_code, r'[^a-z0-9]', '')
      THEN 'identity available from the engagement form'

    -- The actual human queue: real volunteers, nobody knows whose they are.
    WHEN IFNULL(d.map_org, 'unknown') = 'unknown' AND IFNULL(l.volunteers, 0) > 0
      THEN 'org unidentified - needs a human'

    -- Issued to a partner but no export sheet covers it. The pbvrc shape:
    -- signups land nowhere and nothing errors.
    WHEN c.sheet_target_key IS NULL AND i.source_code IS NOT NULL
      THEN 'issued but not captured - no sheet'

    -- Identified partner, real volunteers, still no sheet to hand them.
    WHEN c.sheet_target_key IS NULL AND IFNULL(l.volunteers, 0) > 0
      THEN 'live partner with no sheet'

    WHEN d.map_variants_disagree
      THEN 'case variants classified differently - needs a human'

    ELSE 'none'
  END AS needs_action

FROM all_codes a
LEFT JOIN issued     i USING (source_code)
LEFT JOIN live       l USING (source_code)
LEFT JOIN identified d USING (source_code)
LEFT JOIN captured   c USING (source_code)
ORDER BY
  -- Unresolved first, then by how many volunteers are affected.
  CASE WHEN needs_action LIKE 'none%' THEN 1 ELSE 0 END,
  volunteers DESC,
  a.source_code;

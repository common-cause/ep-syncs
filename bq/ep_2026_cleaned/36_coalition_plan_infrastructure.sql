-- ep_2026_cleaned.coalition_plan_infrastructure -- one row per tracked cell of
-- the 50-State EP Coalition Plan Infrastructure tabs, latest snapshot, with the
-- messy human cell parsed into machine-readable parts.
--
-- Grain: (phase, state, col_name) from the latest as_of_date PER PHASE.
--
-- The tracker is hand-maintained by EP program staff, so a cell can be:
--   verdict + note   "YES - reset 2026-08-18 (470 -> 1). NOTE: ..."
--   not-applicable   "N/A (checklist only - 0 field reports vs 18 checklists)"
--   labelled links   "Poll Monitor quiz: https://...\nRoving Monitor: https://..."
--   a bare link      "https://airtable.com/app..."
--   prose, no link   "Google Forms, not Airtable; form IDs live in Apps Script"
--   blank            nothing recorded yet
-- This view splits each into verdict_token + links[] + note WITHOUT throwing
-- anything away (cell_text keeps the verbatim text). That parse is source-shape
-- work -- it is determined by how the sheet is WRITTEN, not by what EP wants to
-- measure -- which is why it belongs here rather than in a consumer.
--
-- What deliberately does NOT live here: status vocabulary, readiness scoring and
-- plain-language labels. Those are dashboard interpretation, they change when
-- the program's reading changes, and they stay in ep-dashboards.
--
-- Two quirks of the source this view absorbs so consumers never see them:
--   - DC's abbrev cell is empty and its name cell reads "DC"; row 22 is
--     misspelled "Lousiana". `state` resolves through norm_state(), abbrev
--     first, then name (which handles both).
--   - Internal joins key on (phase, sheet_row, col_index), NOT on col_name or
--     state: col_name is not guaranteed unique on a hand-edited row of headers,
--     and a state that failed to resolve would drop its own links/notes out of
--     a NULL-valued join.
--
-- IMPORTANT for anyone aggregating this: the two tabs are structurally
-- identical but their CONTENTS do not carry over. New quizzes, new report forms,
-- new PTV role resets are needed for November, so a filled `primary` cell is NOT
-- evidence the general is ready. Score each phase on its own.
--
-- Source: ep_2026_raw.coalition_plan_infrastructure (nightly melt, written by
-- misc_jobs/infrastructure_sheet.py). PII: cell_text carries staff names where
-- col_name = 'State Lead'.

CREATE OR REPLACE VIEW `proj-tmc-mem-com.ep_2026_cleaned.coalition_plan_infrastructure`
OPTIONS(description="Latest snapshot of the 50-State EP Coalition Plan Infrastructure tabs, one row per (phase, state, tracker column), with each hand-written cell parsed into verdict_token + links[] + note while keeping the verbatim cell_text. phase = 'general' | 'primary' (the two tabs); state is the USPS code via norm_state (the source's blank DC abbrev and 'Lousiana' misspelling are absorbed here). Status vocabulary, readiness scoring and plain-language labels are deliberately NOT here -- that is consumer interpretation. The tabs' contents do NOT carry over: a filled primary cell is not evidence the general is ready, so score each phase separately. Source: ep_2026_raw.coalition_plan_infrastructure. CONTAINS STAFF NAMES in cell_text where col_name='State Lead'.")
AS
WITH latest AS (

  SELECT phase, MAX(as_of_date) AS as_of_date
  FROM `proj-tmc-mem-com.ep_2026_raw.coalition_plan_infrastructure`
  GROUP BY phase

),

-- DISTINCT guards against an append that ran twice (a retried load job); the
-- sync's normal path pre-deletes the day's partition, so this is belt-and-braces.
snapshot AS (

  SELECT DISTINCT r.*
  FROM `proj-tmc-mem-com.ep_2026_raw.coalition_plan_infrastructure` r
  JOIN latest USING (phase, as_of_date)

),

-- The sheet carries non-breaking and narrow-no-break spaces, zero-width spaces
-- and BOM from pasted text; normalize them to plain spaces BEFORE anything is
-- trimmed or compared, or labels and joins break in ways that are painful to
-- see (an invisible character looks exactly like nothing at all).
cleaned AS (

  SELECT
    phase,
    as_of_date,
    sheet_tab,
    sheet_row,
    col_index,
    col_group,
    TRIM(col_name) AS col_name,
    COALESCE(
      `proj-tmc-mem-com.ep_2026_cleaned.norm_state`(state_abbrev_raw),
      `proj-tmc-mem-com.ep_2026_cleaned.norm_state`(state_name_raw)
    ) AS state,
    TRIM(REGEXP_REPLACE(state_name_raw,
                        r'[\x{00a0}\x{202f}\x{200b}\x{feff}]', ' '))
      AS state_name_raw,
    NULLIF(TRIM(COALESCE(state_abbrev_raw, '')), '') AS state_abbrev_raw,
    NULLIF(
      TRIM(REGEXP_REPLACE(COALESCE(cell_value, ''),
                          r'[\x{00a0}\x{202f}\x{200b}\x{feff}]', ' ')),
      ''
    ) AS cell_text
  FROM snapshot

),

-- Leading verdict token, e.g. "YES - reset ...", "N/A (checklist only)".
-- The trailing separator is REQUIRED so prose like "No polling-place data yet"
-- is not misread as a NO verdict.
tokenized AS (

  SELECT
    *,
    UPPER(REGEXP_EXTRACT(
      cell_text,
      r'(?i)^\s*(YES|NO|N/?A|PARTIAL|UNCLEAR|TBD)\s*(?:[-\x{2013}\x{2014}:(]|$)'
    )) AS verdict_token
  FROM cleaned

),

-- Split on newlines: the tracker uses one line per link, usually
-- "Label: https://...". A line with a URL becomes a link (label = the text
-- around it); a line without one is note prose.
lines AS (

  SELECT
    t.phase,
    t.sheet_row,
    t.col_index,
    line_ord,
    line,
    REGEXP_REPLACE(
      REGEXP_EXTRACT(line, r'https?://[^\s]+'),
      r'[).,;\x{201d}]+$', ''
    ) AS url
  FROM tokenized t,
    UNNEST(SPLIT(t.cell_text, '\n')) AS line WITH OFFSET AS line_ord
  WHERE t.cell_text IS NOT NULL

),

links AS (

  SELECT
    phase,
    sheet_row,
    col_index,
    ARRAY_AGG(
      STRUCT(
        NULLIF(
          TRIM(REGEXP_REPLACE(
            TRIM(REGEXP_REPLACE(line, r'https?://[^\s]+', '')),
            r'[:\x{2013}\x{2014}-]+$', ''
          )),
          ''
        ) AS link_label,
        url AS url
      )
      ORDER BY line_ord
    ) AS links
  FROM lines
  WHERE url IS NOT NULL
  GROUP BY phase, sheet_row, col_index

),

notes AS (

  SELECT
    phase,
    sheet_row,
    col_index,
    STRING_AGG(TRIM(line), ' | ' ORDER BY line_ord) AS note
  FROM lines
  WHERE url IS NULL AND TRIM(line) != ''
  GROUP BY phase, sheet_row, col_index

)

SELECT
  t.phase,
  t.state,
  t.col_name,
  t.col_group,
  t.col_index,
  t.cell_text,
  t.verdict_token,
  IFNULL(l.links, ARRAY<STRUCT<link_label STRING, url STRING>>[]) AS links,
  ARRAY_LENGTH(IFNULL(l.links, ARRAY<STRUCT<link_label STRING, url STRING>>[]))
    AS link_count,
  l.links[SAFE_OFFSET(0)].url AS primary_url,
  n.note,
  t.as_of_date,
  t.sheet_tab,
  t.sheet_row,
  t.state_name_raw,
  t.state_abbrev_raw
FROM tokenized t
LEFT JOIN links l USING (phase, sheet_row, col_index)
LEFT JOIN notes n USING (phase, sheet_row, col_index);

-- Sheet target registry for the volunteer sheets sync.
--
-- Owned by ep-syncs (read at the start of each run by
-- sync_volunteer_sheets.py). One row = one Google Sheet in the
-- "2026 EP Volunteer Exports" shared-drive folder. Inserting an enabled
-- row is how a new sheet comes into existence: the next run creates and
-- populates it automatically.
--
-- This is the "middle table" between the historical partner-code flags
-- (ep_archive.source_codes.external) and the export: source_code targets
-- carry an ARRAY of codes so several codes belonging to one group lump
-- into a single sheet (matching is case-insensitive).
--
-- See docs/volunteer_sheets_spec.md for the full design.
--
-- Run once to create. Re-running will fail with "Already Exists" --
-- safe; do not change to CREATE OR REPLACE without checking rows.

CREATE TABLE `proj-tmc-mem-com.ep.volunteer_sheet_targets` (
  target_key    STRING    NOT NULL  OPTIONS(description="Unique within target_type. For state targets: the two-letter state code ('NE'). For source_code targets: a stable partner slug ('aclum'). Used in sync logs and --targets CLI overrides."),
  target_type   STRING    NOT NULL  OPTIONS(description="'state' or 'source_code'. Drives which subfolder the sheet lives in (By State / By Partner) and how rows are selected."),
  sheet_title   STRING    NOT NULL  OPTIONS(description="Spreadsheet title in Drive. The idempotency key: the sync looks the sheet up by this title within the subfolder, creating it if absent. Renaming here orphans the old sheet and creates a new one."),
  source_codes  ARRAY<STRING>       OPTIONS(description="source_code targets only. PTV source codes whose volunteers land in this sheet; more than one lumps a group's codes into a single sheet. Matched with LOWER() against users.source_code."),
  enabled       BOOL      NOT NULL  OPTIONS(description="Sync skips rows where this is FALSE. Lets a target be pre-staged or paused without deleting it."),
  share_with    ARRAY<STRING>       OPTIONS(description="Optional email addresses the sync grants writer access on the sheet (idempotent). Folder/drive membership handles standing access; use this for per-partner external shares."),
  registered_by STRING              OPTIONS(description="Source identifier. Free-form, for debugging. e.g. 'seed script', 'rob manual'."),
  registered_at TIMESTAMP NOT NULL  OPTIONS(description="When the row was first written."),
  updated_at    TIMESTAMP NOT NULL  OPTIONS(description="When any field last changed."),
  notes         STRING              OPTIONS(description="Optional. Free-form context, e.g. the partner org's full name.")
)
OPTIONS(
  description="Registry of Google Sheet destinations for the PTV volunteer sheets sync (by-state and by-partner-source-code exports in the 2026 EP Volunteer Exports shared drive). Read by ep-syncs/sync_volunteer_sheets.py at the start of each run. Insert an enabled row to provision a new sheet."
);

-- ---------------------------------------------------------------------------
-- Seed 1: all 50 states + DC (enabled).
-- ---------------------------------------------------------------------------

INSERT INTO `proj-tmc-mem-com.ep.volunteer_sheet_targets`
  (target_key, target_type, sheet_title, source_codes, enabled, share_with,
   registered_by, registered_at, updated_at, notes)
SELECT
  state,
  'state',
  CONCAT('EP Volunteers 2026 - ', state),
  NULL,
  TRUE,
  NULL,
  'ep-syncs seed 2026-07-08',
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP(),
  NULL
FROM UNNEST([
  'AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN',
  'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH',
  'NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT',
  'VT','VA','WA','WV','WI','WY'
]) AS state;

-- ---------------------------------------------------------------------------
-- Seed 2: partner source codes = historical external flags intersected with
-- codes present in current 2026 data, deduped case-insensitively (canonical
-- casing = the variant with the most current volunteers). Excludes flags that
-- aren't partner orgs. NEEDS HUMAN REVIEW after seeding -- the archive flags
-- are imperfect, and codes new in 2026 (riseup, CivicNE, IndivisibleAZ, ...)
-- are not in the archive and must be added by hand.
-- ---------------------------------------------------------------------------

INSERT INTO `proj-tmc-mem-com.ep.volunteer_sheet_targets`
  (target_key, target_type, sheet_title, source_codes, enabled, share_with,
   registered_by, registered_at, updated_at, notes)
WITH current_codes AS (
  SELECT
    LOWER(source_code)                                        AS code_lower,
    ARRAY_AGG(source_code ORDER BY cnt DESC LIMIT 1)[OFFSET(0)] AS canonical,
    SUM(cnt)                                                  AS n_vols
  FROM (
    SELECT source_code, COUNT(*) AS cnt
    FROM `proj-tmc-mem-com.ptv_raw_2026.v_users_current`
    WHERE source_code IS NOT NULL AND source_code != ''
    GROUP BY source_code
  )
  GROUP BY code_lower
),
external_codes AS (
  SELECT DISTINCT LOWER(source_code) AS code_lower
  FROM `proj-tmc-mem-com.ep_archive.source_codes`
  WHERE external = 'Y'
)
SELECT
  REGEXP_REPLACE(cc.code_lower, r'[^a-z0-9]+', '-'),
  'source_code',
  CONCAT('EP Volunteers 2026 - ', cc.canonical),
  [cc.code_lower],
  TRUE,
  CAST(NULL AS ARRAY<STRING>),  -- bare NULL types as INT64 and fails the insert
  'ep-syncs seed 2026-07-08',
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP(),
  CONCAT('Seeded from ep_archive.source_codes external=Y; ', CAST(cc.n_vols AS STRING), ' current vols at seed time')
FROM current_codes cc
JOIN external_codes ec USING (code_lower)
WHERE cc.code_lower NOT IN ('previous_years', 'quiz', 'actionnetwork');

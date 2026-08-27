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

-- ---------------------------------------------------------------------------
-- Seed 3 (2026-08-24): the codes actually ISSUED to 2026 partners.
--
-- Seeds 1-2 could only see codes with historical volume, so every code handed
-- to a partner for 2026 that nobody has used yet was invisible to them. The
-- issued codes are recorded in the "Source Codes" tab of the 2026 Election
-- Protection Partner Engagement Form responses sheet
-- (1H3p3rzsRdJr4wnj9Pmn6Ck4CBMLv0w44gzHUNb9tb3g) -- column A the partner,
-- column B the protectthevote.net link with the code in ?source=. That tab is
-- the source of truth for what a partner was told to use; this registry is
-- what the sync captures. When they disagree the partner's signups land in no
-- sheet, silently, so they must be reconciled whenever new codes are issued.
--
-- Registering a code with zero signups is deliberate: the sheet is created
-- empty and is ready to hand over the moment the first volunteer arrives.
--
-- Re-runnable (MERGE on target_key). Note Rob's 2026-07-09 curation pass is
-- NOT reflected in this file -- it was applied directly in BQ.
-- ---------------------------------------------------------------------------

MERGE `proj-tmc-mem-com.ep.volunteer_sheet_targets` T
USING (
  SELECT * FROM UNNEST([
    STRUCT<code STRING, title_label STRING, org STRING>
    ('barbarajordanleadershipinstitute', 'BarbaraJordanLeadershipInstitute', 'Barbara Jordan Leadership Institute'),
    ('lwvva',                  'LWVVA',                  'League of Women Voters of Virginia'),
    ('narf',                   'narf',                   'NARF'),
    ('upvoteva',               'UpVoteVA',               'UpVote VA'),
    ('tcrp',                   'TCRP',                   'TCRP'),
    ('organizetn',             'OrganizeTN',             'Organize TN'),
    ('acluoftx',               'ACLUofTX',               'ACLU of TX'),
    ('wicivicpowertable',      'WICivicPowerTable',      'WI Civic Power Table'),
    ('chicagolcforcivilrights','ChicagoLCforCivilRights',"Chicago Lawyers' Committee for Civil Rights"),
    ('blueprintnc',            'BlueprintNC',            'Blueprint North Carolina'),
    ('montanavoices',          'MontanaVoices',          'Montana Voices'),
    ('acluofwifoundation',     'ACLUofWIFoundation',     'ACLU of Wisconsin Foundation'),
    ('acluofalabama',          'ACLUofAlabama',          'ACLU of Alabama'),
    ('lwval',                  'LWVAL',                  'League of Women Voters of Alabama'),
    ('civictn',                'CivicTN',                'Civic TN')
  ])
) S
ON T.target_key = REGEXP_REPLACE(S.code, r'[^a-z0-9]+', '-')
   AND T.target_type = 'source_code'
WHEN NOT MATCHED THEN INSERT (
  target_key, target_type, sheet_title, source_codes, enabled, share_with,
  registered_by, registered_at, updated_at, notes
) VALUES (
  REGEXP_REPLACE(S.code, r'[^a-z0-9]+', '-'),
  'source_code',
  CONCAT('EP Volunteers 2026 - ', S.title_label),
  [S.code],
  TRUE,
  CAST(NULL AS ARRAY<STRING>),
  'rob curation 2026-08-24 (partner engagement form Source Codes tab)',
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP(),
  CONCAT(S.org, '; code issued for 2026, no signups at registration time')
);

-- Two partners were issued a DIFFERENT spelling than the code their existing
-- sheet captures. Lump the issued spelling in rather than creating a second
-- sheet -- one partner, one volunteer list.
--   avlaz  <- avilaz : the form issues ?source=AVILAZ (unused so far: latent).
--   pbcvrc <- pbvrc  : 43 volunteers were already signing up under pbvrc and
--                      landing in NO sheet. This is the live-breakage case.

UPDATE `proj-tmc-mem-com.ep.volunteer_sheet_targets`
SET source_codes = ARRAY_CONCAT(source_codes, ['avilaz']),
    updated_at   = CURRENT_TIMESTAMP(),
    notes        = CONCAT(IFNULL(notes, ''), '; lumped avilaz -- spelling issued on the partner engagement form (2026-08-24)')
WHERE target_key = 'avlaz'
  AND NOT EXISTS (SELECT 1 FROM UNNEST(source_codes) c WHERE c = 'avilaz');

UPDATE `proj-tmc-mem-com.ep.volunteer_sheet_targets`
SET source_codes = ARRAY_CONCAT(source_codes, ['pbvrc']),
    updated_at   = CURRENT_TIMESTAMP(),
    notes        = CONCAT(IFNULL(notes, ''), '; lumped pbvrc -- spelling issued on the partner engagement form, 43 vols were unsheeted (2026-08-24)')
WHERE target_key = 'pbcvrc'
  AND NOT EXISTS (SELECT 1 FROM UNNEST(source_codes) c WHERE c = 'pbvrc');

-- ---------------------------------------------------------------------------
-- Seed 4 (2026-08-26): partner codes with real volume that no target covered.
--
-- These came out of sync_volunteer_sheets.py's own unregistered-code warning
-- (report_unregistered_codes) -- partners whose volunteers were accumulating
-- in BQ with no sheet to hand them. They were NOT on the partner engagement
-- form's Source Codes tab either; they have been added to it alongside these
-- rows, so the tab and this registry stay in step.
--
-- CC-internal codes in that same warning (ccaz, ccfl, ccri, common cause
-- oregon) are excluded on purpose per the 2026-07-09 rule: cc+state codes are
-- internal CC orgs, not partner sheets. Generic acquisition codes (members,
-- engage, adwords, footer, email-*) are not partners at all.
--
-- Only frrc / aclufl / lwvmn have a confidently known org behind them. The
-- rest are registered under the code itself rather than an invented org name
-- -- see notes. Rename sheet_title + notes once identity is confirmed (that
-- orphans the old sheet, so do it before anyone is given the link).
--
-- Re-runnable (MERGE on target_key).
-- ---------------------------------------------------------------------------

MERGE `proj-tmc-mem-com.ep.volunteer_sheet_targets` T
USING (
  SELECT * FROM UNNEST([
    STRUCT<code STRING, org STRING>
    ('frrc',      'Florida Rights Restoration Coalition'),
    ('aclufl',    'ACLU of Florida'),
    ('lwvmn',     'League of Women Voters of Minnesota'),
    ('htff',      'Harriet Tubman Freedom Fighters'),
    ('corazonaz', 'IDENTITY UNCONFIRMED -- registered under the raw code'),
    ('poder',     'IDENTITY UNCONFIRMED -- registered under the raw code'),
    ('imc',       'IDENTITY UNCONFIRMED -- may be the "Integrity Matters" row on the engagement form; merge into one target if so'),
    ('aatbfl',    'IDENTITY UNCONFIRMED -- may be a chapter of the existing aatbf target; merge into one target if so')
  ])
) S
ON T.target_key = REGEXP_REPLACE(S.code, r'[^a-z0-9]+', '-')
   AND T.target_type = 'source_code'
WHEN NOT MATCHED THEN INSERT (
  target_key, target_type, sheet_title, source_codes, enabled, share_with,
  registered_by, registered_at, updated_at, notes
) VALUES (
  REGEXP_REPLACE(S.code, r'[^a-z0-9]+', '-'),
  'source_code',
  CONCAT('EP Volunteers 2026 - ', S.code),
  [S.code],
  TRUE,
  CAST(NULL AS ARRAY<STRING>),
  'rob curation 2026-08-26 (unregistered-code warning backfill)',
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP(),
  S.org
);

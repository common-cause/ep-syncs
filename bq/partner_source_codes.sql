-- Landing table for the 2026 EP Partner Engagement Form: the record of which
-- source codes were ISSUED, to which partner, and when they asked.
--
-- Owned by ep-syncs (written nightly by misc_jobs/partner_source_codes.py).
-- READ-ONLY toward the spreadsheet -- EP program staff own it.
--
-- WHY THIS EXISTS
-- Every other view of a source code is downstream of a volunteer using it:
-- ptv_raw_2026.users only knows codes people signed up under, and
-- ep_dashboards.source_code_map's detection surface is that same live traffic.
-- So a code that has been handed to a partner but not yet used is invisible
-- everywhere, and a code whose spelling drifted between issuance and capture
-- (pbvrc vs pbcvrc, AVILAZ vs avlaz) silently collects volunteers that land in
-- no partner sheet at all. This table is the only record of the ISSUING side,
-- which makes that gap queryable instead of anecdotal.
--
-- TWO TABS, ONE TABLE (`source_tab` distinguishes them):
--   'Form Responses 1' -- the live intake. One row per partner submission,
--       carrying the org, whether they asked for a code, the code minted for
--       them, its link, and the spreadsheet column staff fill in by hand.
--       This is the authoritative record of who minted what and for whom.
--   'Source Codes'     -- a hand-curated summary tab: org in column A, the
--       issued link in column B, and (since 2026-08-26) the volunteer
--       spreadsheet link in column C. Rows here have no submission timestamp.
-- They overlap and disagree; both are captured verbatim rather than merged,
-- because which one is right is exactly the question a human has to answer.
--
-- INTERPRETATION LIVES DOWNSTREAM (same rule as coalition_plan_infrastructure):
-- the ?source= parse, the join to live volume, and the "does this need a human"
-- verdict are all in ep_2026_cleaned.source_code_resolution, so a parse fix is
-- a view change and never needs a re-scrape.
--
-- PII: DELIBERATELY NARROW. The form also collects the submitting person's
-- name, email, social handles and free-text comments. None of that is ingested
-- -- it is not needed to reconcile a code, and the policy is to point at
-- access-controlled systems rather than copy people-data into new ones. If you
-- need to chase who requested a code, open the form. `org_label` is an
-- organization, not a person.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, so a fresh environment self-heals.
-- The loader replaces the day's partition before appending.

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.ep_2026_raw.partner_source_codes` (
  as_of_date       DATE      NOT NULL OPTIONS(description="Snapshot date (US/Eastern, matching run_misc_jobs.py's weekday convention). Partition key. One full snapshot of both tabs per day."),
  source_tab       STRING    NOT NULL OPTIONS(description="Which tab the row came from: 'Form Responses 1' (live partner intake) or 'Source Codes' (hand-curated summary). They overlap and sometimes disagree; both are kept verbatim."),
  sheet_row        INT64              OPTIONS(description="1-based row number on that tab at scrape time. Evidence only -- these are human sheets and rows move."),
  org_label        STRING             OPTIONS(description="Organization name exactly as typed on the tab. An organization, never a person. Free text: expect trailing spaces, inconsistent casing, and the same partner spelled several ways."),
  source_code_raw  STRING             OPTIONS(description="The 'Source Code' column, verbatim. Populated only for 'Form Responses 1' -- the Source Codes tab carries no code column, only the link, so the code must be parsed from source_url downstream."),
  source_url       STRING             OPTIONS(description="The protectthevote.net recruitment link issued to the partner, verbatim including any trailing whitespace. The ?source= parameter is the code that PTV will actually stamp on a signup. NULL where a row names an org but no code was ever issued -- that absence is itself signal."),
  spreadsheet_link STRING             OPTIONS(description="Link to the volunteer export spreadsheet the partner was (or should be) given. Hand-maintained by staff on the form tab; written by ep-syncs on the Source Codes tab. Empty means nobody has handed this partner their data."),
  wants_code       STRING             OPTIONS(description="The 'Do you want a source code for recruitment?' answer, verbatim (the form asks it in more than one branch; the first non-empty answer wins). Free text -- expect 'Yes', 'yes', 'no', and blank."),
  submitted_at     STRING             OPTIONS(description="Form submission timestamp as displayed, verbatim and unparsed (the sheet renders US-style m/d/yyyy). NULL for Source Codes tab rows. Cast downstream."),
  synced_at        TIMESTAMP NOT NULL OPTIONS(description="When this snapshot was captured (UTC).")
)
PARTITION BY as_of_date
CLUSTER BY source_tab
OPTIONS(
  description="Daily snapshot of the 2026 EP Partner Engagement Form (Responses) spreadsheet -- the record of which recruitment source codes were ISSUED to which partner organization. The only capture of the issuing side of a source code; everything else in the warehouse only sees codes after a volunteer uses one. Written by ep-syncs misc_jobs/partner_source_codes.py; interpreted by ep_2026_cleaned.source_code_resolution. Contains organization names, deliberately no personal contact data."
);

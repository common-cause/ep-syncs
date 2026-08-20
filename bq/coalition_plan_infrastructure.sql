-- proj-tmc-mem-com.ep_2026_raw.coalition_plan_infrastructure
--
-- Long-format ("melted") nightly snapshots of the two Infrastructure tabs of
-- the 50-State EP Coalition Plan spreadsheet -- the EP program team's
-- hand-maintained tracker of per-state setup (training + quiz links, reporting
-- forms, PTV role hygiene, polling-place selection, post-election collection).
--
-- Written by misc_jobs/infrastructure_sheet.py (nightly, via run_misc_jobs.py).
-- Read by ep_2026_cleaned.coalition_plan_infrastructure, which does the parse.
--
-- WHY LONG FORMAT: the program team adds and renames columns without telling
-- anyone. One row per (snapshot, phase, state, sheet column) turns a schema
-- change into NEW ROWS instead of a broken load. Nothing here interprets a
-- cell -- verdicts, links and notes are parsed in the cleaned view, so a parse
-- fix never requires a re-scrape (and the raw text stays available forever).
--
-- WHY ep_2026_raw AND NOT A DEDICATED DATASET: `com-dbt@` has no
-- bigquery.datasets.create in this project, and BigQuery checks that permission
-- even for `CREATE SCHEMA IF NOT EXISTS` on a dataset that already exists -- so
-- a per-source dataset (`coalition_plan_raw_2026`) would need a one-time admin
-- step before the sync could ever self-heal (see docs/asana_nm_sync_spec.md §8
-- for what that cost last time). ep_2026_raw is already the EP-2026 raw landing
-- zone with com-dbt@ write access; a single table doesn't warrant a dataset.
-- The table name carries the source, so nothing is ambiguous.
--
-- PII: `cell_value` carries staff names where col_name = 'State Lead' (col C of
-- both tabs). BigQuery is access-controlled, so that's fine here -- but do not
-- commit row-level extracts of this table, and don't paste real cell text into
-- docs or commit messages.
--
-- CREATE TABLE IF NOT EXISTS so a fresh environment self-heals; the sync
-- applies this file before every load.

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.ep_2026_raw.coalition_plan_infrastructure` (
  as_of_date        DATE      NOT NULL OPTIONS(description="Snapshot date this row was read on, stamped in US/Eastern to match the nightly runner's weekday convention. Partition key."),
  phase             STRING    NOT NULL OPTIONS(description="Election the source tab tracks: 'general' | 'primary'. The two tabs are structurally identical but their contents do NOT carry over -- a filled primary cell is not evidence the general is ready."),
  sheet_tab         STRING    OPTIONS(description="Source worksheet name, verbatim ('Infrastructure (General)' | 'Infrastructure (Primary)')"),
  sheet_row         INT64     OPTIONS(description="1-based row number in the worksheet (data starts at row 4)"),
  state_name_raw    STRING    OPTIONS(description="Column A verbatim. May be misspelled (row 22 reads 'Lousiana'); DC's cell reads 'DC' rather than a full name."),
  state_abbrev_raw  STRING    OPTIONS(description="Column B verbatim; NULL for DC, whose abbrev cell (B11) is empty. Resolve state from this first, then fall back to state_name_raw -- ep_2026_cleaned.norm_state() does both."),
  col_index         INT64     OPTIONS(description="0-based column position in the worksheet (2 = column C, the first melted column)"),
  col_group         STRING    OPTIONS(description="Row-2 column-group label ('Training', 'Reporting', 'PTV Setup', 'Polling Places'), forward-filled rightward. NULL for columns left of the first group label."),
  col_name          STRING    OPTIONS(description="Row-3 column name, verbatim. This is the tracked item's identity; unlabelled columns are not melted."),
  cell_value        STRING    OPTIONS(description="Cell contents verbatim, NULL when blank. Prose as often as data: verdict-plus-note, N/A-with-reason, labelled multi-link lists, bare URLs. CARRIES STAFF NAMES where col_name = 'State Lead'."),
  synced_at         TIMESTAMP OPTIONS(description="When this snapshot was written (UTC)")
)
PARTITION BY as_of_date
CLUSTER BY phase, col_name
OPTIONS(description="Long-format nightly snapshots of the 50-State EP Coalition Plan Infrastructure tabs (spreadsheet 1YdFVBUcqMF5d4Hniy8CKbxEQ_gJG13Xe7Gkb8HXnasY, tabs 'Infrastructure (General)' and 'Infrastructure (Primary)'). One row per (as_of_date, phase, state, sheet column); ~1,632 rows/night (51 states x 16 columns x 2 tabs). Cell text is VERBATIM -- all interpretation happens in ep_2026_cleaned.coalition_plan_infrastructure. Written by ep-syncs misc_jobs/infrastructure_sheet.py. Same-day reruns replace the day's partition rows. Contains staff names in cell_value where col_name='State Lead'.");

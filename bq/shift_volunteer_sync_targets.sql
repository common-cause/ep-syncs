-- Sync target registry for the shift volunteers sync.
--
-- Owned by ep-syncs (read at the start of each Civis run by
-- sync_shift_volunteers.py) and written to by ep-airtable-utilities
-- as the final step of taking a new base live.
--
-- See ep-syncs/CLAUDE.md and the spec sent to ep-airtable-utilities
-- (ep-syncs__shift-volunteer-sync-registration-spec.md) for the
-- registration contract.
--
-- Run once to create. Re-running will fail with "Already Exists" --
-- safe; do not change to CREATE OR REPLACE without coordinating with
-- whoever has rows in this table.

CREATE TABLE `proj-tmc-mem-com.ep.shift_volunteer_sync_targets` (
  name                 STRING    NOT NULL  OPTIONS(description="Human-readable label, unique. Used in sync logs. Convention: '<State> <Role>' e.g. 'NE Primary', 'PA Coalition / Pittsburgh'."),
  state                STRING    NOT NULL  OPTIONS(description="Two-letter US state code. Drives which PTV pull's data this base receives."),
  base_id              STRING    NOT NULL  OPTIONS(description="Airtable base ID (app...)."),
  table_name           STRING    NOT NULL  OPTIONS(description="Airtable table name within the base. Defaults to 'Shifted Volunteers' for canonical CC bases."),
  field_map_overrides  JSON                OPTIONS(description="BQ-col -> Airtable-col overrides merged over DEFAULT_FIELD_MAP in sync_shift_volunteers.py. NULL or {} means use defaults verbatim. A null value for a key removes that key from the merged map."),
  enabled              BOOL      NOT NULL  OPTIONS(description="Sync skips rows where this is FALSE. Lets ep-airtable-utilities pre-stage a target before go-live."),
  registered_by        STRING              OPTIONS(description="Source identifier. Free-form, for debugging. e.g. 'ep-airtable-utilities', script name."),
  registered_at        TIMESTAMP NOT NULL  OPTIONS(description="When the row was first written."),
  updated_at           TIMESTAMP NOT NULL  OPTIONS(description="When any field last changed."),
  notes                STRING              OPTIONS(description="Optional. Free-form context.")
)
OPTIONS(
  description="Registry of Airtable destinations for the PTV->BQ->Airtable shift volunteers sync. Read by ep-syncs/sync_shift_volunteers.py at the start of each Civis run; written by ep-airtable-utilities when a new base goes live."
);

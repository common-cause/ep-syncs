-- Raw landing table for the PTV trainings sync (sync_ptv_trainings.py).
--
-- One row per (training session, attendee) signup, scraped from the PTV admin
-- GUI at /state_admin/trainings/<tid>/attendees/<schedule_id>. PTV exposes NO
-- API for training signups (unlike users_csv / shift_volunteers_csv), so this
-- rides ptv-tools browser automation. READ-ONLY toward PTV.
--
-- Append-only: one full per-state snapshot per as_of_date, mirroring
-- ptv_raw_2026.users / .shift_volunteers. The sync pre-deletes today's
-- partition for the states it is about to (re)write, so a same-day rerun is
-- idempotent EXCEPT when BigQuery's streaming buffer blocks the DELETE (rows
-- streamed in the last ~90 min); in that window a rerun can leave exact-
-- duplicate rows. Consumers must dedupe -- see docs/ptv_trainings_sync_spec.md
-- for the ROW_NUMBER recipe (dedupe key: as_of_date, registration_id).
--
-- PII: `email` is row-level PII. This table lives in access-controlled
-- BigQuery and must never be exported to git or shared corpora un-masked.
--
-- IF NOT EXISTS: the sync runs this at startup so a fresh Civis container
-- self-heals. Editing columns here does NOT alter an existing table -- ALTER
-- deliberately (a schema change is a coordinated migration).

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.ptv_raw_2026.training_signups` (
  as_of_date       DATE     NOT NULL OPTIONS(description="Snapshot date (UTC) this row was captured. Partition key. Append-only, one full per-state snapshot per day."),
  registration_id  INT64             OPTIONS(description="PTV registration record id (the phx-value-id on the roster's cancel_registration control). Stable per signup; the natural dedupe key within a snapshot."),
  training_id      INT64             OPTIONS(description="PTV training id (/state_admin/trainings/<id>). Maps to ep_dashboards.training_event_map.source_event_id where source_system='ptv'."),
  training_name    STRING            OPTIONS(description="Training name as shown on the state trainings list."),
  training_status  STRING            OPTIONS(description="Draft | Published, from the state trainings list."),
  schedule_id      INT64             OPTIONS(description="PTV session (schedule) id within the training. A recurring training has several; each has its own attendee roster."),
  session_date     DATE              OPTIONS(description="Scheduled session date. NULL for on-demand / undated sessions."),
  start_time       TIME              OPTIONS(description="Session start time as shown (no timezone asserted)."),
  end_time         TIME              OPTIONS(description="Session end time as shown (no timezone asserted)."),
  role_id          INT64             OPTIONS(description="PTV role id the training grants (training-level, from the show page). Pairs with users.role / shift role semantics."),
  role             STRING            OPTIONS(description="Role name as shown on the attendee record."),
  email            STRING            OPTIONS(description="Attendee email. Join key to ptv_raw_2026.users and the roster. PII."),
  signup_date      DATETIME          OPTIONS(description="Real signup timestamp from PTV (naive local wall-clock; no timezone asserted). Replaces the snapshot-inferred 'on or before' date the dashboards used previously."),
  status           STRING            OPTIONS(description="Registration status at scrape time: ATTENDING | CANCELLED. Cancelled registrations are retained (signal)."),
  attended         STRING            OPTIONS(description="PTV 'attended' flag as shown (YES / NO / blank). Largely unmarked by hosts in practice; captured for completeness."),
  host_email       STRING            OPTIONS(description="Training host email (show-page metadata)."),
  quiz_link        STRING            OPTIONS(description="Training quiz link if any (show-page metadata)."),
  state            STRING            OPTIONS(description="Two-letter USPS code the session was scoped to at scrape time, verified via the state-switch (never trusted from inherited context).")
)
PARTITION BY as_of_date
CLUSTER BY state, email
OPTIONS(
  description="Raw per-(training session, attendee) signup snapshots scraped from the PTV admin GUI. Append-only, full per-state snapshot per as_of_date. No PTV API exists for this data -- captured via ptv-tools browser automation by ep-syncs/sync_ptv_trainings.py. PII (email); access-controlled. Dedupe on (as_of_date, registration_id). See docs/ptv_trainings_sync_spec.md."
);

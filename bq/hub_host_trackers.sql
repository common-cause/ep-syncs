-- Registry for the EP Hub Host Tracker sheets driven by
-- misc_jobs/hub_host_tracker.py.
--
-- One enabled row = one state's host-tracker spreadsheet gets a refreshed
-- "Volunteer Landing Page" every night. Registering a state is an INSERT here,
-- not a code change -- the same contract as ep.volunteer_sheet_targets and
-- ep.airtable_sync_sources.
--
-- WHY A REGISTRY AND NOT CONSTANTS
-- The thing that varies between states is not the layout, it's *which quiz
-- bases feed the page and what role each one implies*. MI runs two quizzes
-- (Poll Monitor, Rover) whose only difference to this sync is the role label
-- printed on the host's distribution list. OR runs three, PA one, NY one. That
-- mapping is data, so it lives here: adding a state, or a state adding a third
-- quiz mid-cycle, is an INSERT/UPDATE rather than an edit to the extract.
--
-- PREREQUISITE: every base_key in quiz_sources must already be an enabled
-- base_type='quiz' row in ep.airtable_sync_sources, because this sync reads
-- what sync_airtable_bases.py has already landed in ep_2026_raw /
-- ep_2026_cleaned.quiz_responses. It does NOT talk to Airtable itself.
--
-- PREREQUISITE: the service account behind GOOGLE_SHEETS_CREDENTIALS_PASSWORD
-- (sheets-controllers@sheets-controllers) must be a writer on spreadsheet_id.
-- Unlike the volunteer-export sheets, the job does not create these files --
-- program staff own them and hand us the id.

CREATE TABLE IF NOT EXISTS `proj-tmc-mem-com.ep.hub_host_trackers` (
  target_key STRING NOT NULL
    OPTIONS(description="Stable key for this tracker, used by --targets. Convention: the state abbreviation (e.g. 'MI')."),
  state STRING NOT NULL
    OPTIONS(description="Two-letter state, matched against ep_2026_cleaned.quiz_responses.state and ptv_raw_2026.shift_volunteers.state."),
  spreadsheet_id STRING NOT NULL
    OPTIONS(description="Google Sheets file id of the host tracker. Program staff own the file; the job only writes the tabs named below."),

  quiz_sources ARRAY<STRUCT<
    base_key STRING,
    role_label STRING,
    quiz_label STRING
  >> OPTIONS(description="Quiz bases feeding this tracker. base_key matches ep_2026_cleaned.quiz_responses.base_key (= bq_table_prefix in ep.airtable_sync_sources). role_label is what prints in the host distribution list's Role(s) column; quiz_label is the friendlier name shown on the landing page. A volunteer who passed two quizzes gets ONE row with both role_labels joined."),

  landing_tab STRING
    OPTIONS(description="Visible tab the sync maintains as a mirror of data_tab. Default 'Volunteer Landing Page'."),
  hosts_tab STRING
    OPTIONS(description="Tab holding the valid-host list that feeds the assignment dropdown. Column A = host name; B..E = city / email / phone / Mobilize link. Hand-maintained by program staff; the sync only READS it."),
  template_tab STRING
    OPTIONS(description="Tab cloned per host. The sync installs its formulas only in --install-scaffolding mode, never on the nightly path."),
  data_tab STRING
    OPTIONS(description="Hidden, job-owned tab holding the extract. Rewritten every run; the append-only row ledger lives in its column A."),

  enabled BOOL NOT NULL
    OPTIONS(description="FALSE parks a tracker without deleting its registration (e.g. a state between elections)."),
  registered_by STRING
    OPTIONS(description="Who/what added this row."),
  registered_at TIMESTAMP
    OPTIONS(description="When this row was first added."),
  updated_at TIMESTAMP
    OPTIONS(description="Last change to this row."),
  notes STRING
    OPTIONS(description="Free-text: quirks, the program contact, why a row is disabled.")
)
OPTIONS(
  description="Registry for misc_jobs/hub_host_tracker.py -- per-state EP materials-distribution host trackers. One enabled row = one spreadsheet whose Volunteer Landing Page is refreshed nightly from that state's quiz bases. See docs/hub_host_tracker_spec.md."
);


-- MI seed -------------------------------------------------------------------
-- "EP Hub Host Tracker" (owner: sabbott@commoncause.org). MI distributes
-- volunteer materials through regional hub hosts; each host gets a cloned
-- TEMPLATE tab whose distribution list is a FILTER over the landing page.
--
-- MERGE (not INSERT) so re-running this file is idempotent.
MERGE `proj-tmc-mem-com.ep.hub_host_trackers` T
USING (
  SELECT
    'MI' AS target_key,
    'MI' AS state,
    '16ifgJHiL3s3VmUdAzM7JVZlvVjlSIQ138H89KDRP2UQ' AS spreadsheet_id,
    [
      STRUCT('mi_poll_monitor_quiz' AS base_key,
             'Poll Monitor'         AS role_label,
             'Poll Monitor Quiz'    AS quiz_label),
      STRUCT('mi_rover_quiz', 'Rover', 'Rover Quiz')
    ] AS quiz_sources,
    'Volunteer Landing Page' AS landing_tab,
    'Hosts'                  AS hosts_tab,
    'TEMPLATE'               AS template_tab,
    '_data'                  AS data_tab,
    TRUE AS enabled,
    'ep-syncs seed 2026-08-21' AS registered_by,
    'MI materials distribution via regional hub hosts. Sheet is owned by Shan Abbott and writer-shared to all of commoncause.org, so the landing page carries volunteer PII (name, full mailing address, email, phone) to everyone in the domain -- that exposure is the program team\'s call, made knowingly. Kevin Fisher was the first host; his tab predates the TEMPLATE that has a Role(s) column and was retrofitted 2026-08-21.' AS notes
) S
ON T.target_key = S.target_key
WHEN MATCHED THEN UPDATE SET
  state = S.state,
  spreadsheet_id = S.spreadsheet_id,
  quiz_sources = S.quiz_sources,
  landing_tab = S.landing_tab,
  hosts_tab = S.hosts_tab,
  template_tab = S.template_tab,
  data_tab = S.data_tab,
  enabled = S.enabled,
  updated_at = CURRENT_TIMESTAMP(),
  notes = S.notes
WHEN NOT MATCHED THEN INSERT (
  target_key, state, spreadsheet_id, quiz_sources,
  landing_tab, hosts_tab, template_tab, data_tab,
  enabled, registered_by, registered_at, updated_at, notes
) VALUES (
  S.target_key, S.state, S.spreadsheet_id, S.quiz_sources,
  S.landing_tab, S.hosts_tab, S.template_tab, S.data_tab,
  S.enabled, S.registered_by, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), S.notes
);

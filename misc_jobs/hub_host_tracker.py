"""
EP Hub Host Trackers -- quiz completers -> a state's materials-distribution
spreadsheet (BigQuery -> Google Sheets).

Some states distribute EP volunteer materials (lanyards, vests, car magnets,
guides, swag) through regional "hub hosts" rather than mailing every kit from
the state office. Each host needs a list of the volunteers assigned to them,
with the contact details and the specific items each one asked for. MI is the
2026 case; the registry (`ep.hub_host_trackers`) makes adding another state an
INSERT rather than a code change.

WHAT THIS OWNS, AND WHAT IT DELIBERATELY DOES NOT
Nightly, per enabled registry row, this job rewrites exactly ONE tab: the
hidden `_data` extract. Everything a human touches is either downstream of it
through a formula or in a column the job never writes:

    _data      (hidden) job-owned. Rewritten every run. Column A is the row
                        ledger (see below); B..R are the display columns.
    Volunteer   (visible) A1 holds one array formula, `={_data!B:R}`, so
    Landing Page          columns A..Q are a live mirror. Column R
                          ("Assigned Host") is a validated dropdown fed by the
                          Hosts tab and is HUMAN-owned -- the job never writes
                          a value into it. S onward is free space for notes.
    Hosts      (visible) hand-maintained by program staff; READ-ONLY here. It
                         is the dropdown's source of truth for "valid host".
    TEMPLATE   (visible) cloned by hand, one tab per host. Its formulas are
                         installed by --install-scaffolding, NOT nightly.

The mirror is why there is no keyed update of any single column. Because the
whole A..Q block is one array formula over a job-owned tab, every field --
including Shifted? -- refreshes on its own every night with nothing to clobber
and no email-matching step that could silently fail to match. The only thing a
human owns on that tab is column R, and it is outside the block.

ROW LEDGER (why column A exists)
Host assignments live in column R, positionally aligned to the mirrored rows.
So a row that disappears from the middle of the extract would shift every row
below it up by one and silently reassign those volunteers to the wrong hosts.
Sorting the extract is not enough to prevent that -- an Airtable quiz record
can be *deleted*, and the typed `ep_2026_raw` tables are rebuilt from scratch
each night, so a deletion propagates within a day.

Column A of `_data` therefore holds a stable per-volunteer key (their earliest
quiz record id), and the job reads the previous `_data` back before writing:
rows keep the order they already had, genuinely new volunteers append at the
bottom, and a volunteer whose quiz record vanished is CARRIED FORWARD with
their last-known values and flagged "No -- record deleted" in the "In Quiz
Base?" column. Rows are never removed and never reordered. The sheet is its own
append-only ledger, which also means the contract survives a registry change,
a re-registered quiz base, or a rebuild of the cleaned views.

GRAIN: one row per volunteer, not per quiz submission. A retake, or someone who
passed both the Poll Monitor and Rover quizzes, is one row (one kit) with both
role labels joined -- duplicating them would duplicate a physical kit.

"SHIFTED?" AND THE PRIMARY -> GENERAL WIPE
Shifted? reflects CURRENT PTV shift signups (ep_2026_cleaned.volunteers). When
a state wipes its primary shifts to build the general, that column legitimately
drops to No for everyone and refills as general shifts are claimed. So the page
also carries "Ever Shifted?", latched from the all-time daily snapshots in
`ptv_raw_2026.shift_volunteers`, which survives the wipe. Only Shifted? flows to
the host tabs; Ever Shifted? is context for whoever is assigning.

READ-ONLY toward Airtable and toward the Hosts tab. The quiz data arrives via
sync_airtable_bases.py; this job only reads what that already landed.

PII: the landing page carries volunteer names, full mailing addresses, emails
and phones. Registered trackers are program-owned files that may be broadly
shared inside the org (the MI sheet is writer-shared to all of
commoncause.org), so treat every registration as a deliberate PII-exposure
decision by the program team and record it in the registry's `notes`. The
dry-run report here is counts-only, on purpose -- nothing row-level should ever
reach a terminal, a log, a ticket or a spec doc.

Credentials come from the environment (BIGQUERY_CREDENTIALS_PASSWORD,
GOOGLE_SHEETS_CREDENTIALS_PASSWORD); run_misc_jobs.py loads .env before calling
run().

Design + the go-live checklist: docs/hub_host_tracker_spec.md
Registry contract: bq/hub_host_trackers.sql
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gspread

from ccef_connections import BigQueryConnector, SheetsWriterConnector
from ccef_connections.core.retry import retry_google_operation

logger = logging.getLogger(__name__)

BQ_PROJECT = "proj-tmc-mem-com"
REGISTRY_TABLE = f"{BQ_PROJECT}.ep.hub_host_trackers"

# -- Sheet layout -----------------------------------------------------------
#
# (header, extract key). Column A is the row ledger key and is NOT mirrored to
# the landing page -- it is bookkeeping, not something staff should read. The
# seven columns from "Volunteer Name" through "Requested items?" are the block
# that flows to host tabs, and their order MUST match the TEMPLATE's
# Distribution List headers (G6:M6) because the host-tab formula is a single
# FILTER over the contiguous range.
DATA_COLUMNS: List[Tuple[str, str]] = [
    ("_key",             "anchor_key"),        # A  ledger key, not mirrored
    ("Volunteer Name",   "full_name"),         # B  -\
    ("Location",         "location"),          # C   |
    ("Contact email",    "contact_email"),     # D   |
    ("Contact Phone",    "contact_phone"),     # E   |- flows to host tabs
    ("Role(s)",          "roles"),             # F   |  (TEMPLATE G:M)
    ("Shifted?",         "shifted"),           # G   |
    ("Requested items?", "requested_items"),   # H  -/
    ("Quiz",             "quiz"),              # I
    ("Score",            "score"),             # J
    ("Submitted",        "submitted"),         # K
    ("Shifts",           "shifts"),            # L
    ("First Shift",      "first_shift"),       # M
    ("Latest Shift",     "latest_shift"),      # N
    ("County",           "county"),            # O
    ("Zip",              "zip_code"),          # P
    ("Ever Shifted?",    "ever_shifted"),      # Q
    ("In Quiz Base?",    "in_quiz_base"),      # R
]

HEADERS = [h for h, _ in DATA_COLUMNS]
KEY_INDEX = 0                      # _data column A
IN_QUIZ_BASE_INDEX = len(DATA_COLUMNS) - 1

# _data!B:R is what the landing page mirrors; B..H is what host tabs filter.
_MIRROR_FIRST, _MIRROR_LAST = "B", chr(ord("A") + len(DATA_COLUMNS) - 1)
DISTRO_WIDTH = 7                   # Volunteer Name .. Requested items?

# Landing-page geometry, derived from the mirror width so the two can't drift.
MIRROR_WIDTH = len(DATA_COLUMNS) - 1                     # A..Q  (17)
HOST_COL_A1 = chr(ord("A") + MIRROR_WIDTH)               # R
HOST_COL_INDEX0 = MIRROR_WIDTH                           # 0-based, for the API
NOTES_COL_A1 = chr(ord("A") + MIRROR_WIDTH + 1)          # S
DISTRO_LAST_A1 = chr(ord("A") + DISTRO_WIDTH - 1)        # G
ROW_BUFFER = 200                   # spill room + room to grow between runs
LANDING_MIN_COLS = MIRROR_WIDTH + 6

# TEMPLATE geometry (verified against the MI sheet 2026-08-21).
TPL_HOST_CELL = "A1"               # host name; a dropdown after scaffolding
TPL_PHONE_CELL = "A2"
TPL_LINK_CELL = "A3"
TPL_CITY_CELL = "D1"
TPL_EMAIL_CELL = "D2"
TPL_DISTRO_HEADER_ROW = 6          # "Volunteer Name | Location | ..." in G6:O6
TPL_DISTRO_FIRST_ROW = 7           # the FILTER lands in G7
TPL_DISTRO_FIRST_COL_A1 = "G"
TPL_MIN_ROWS = 250                 # spill room; cloned tabs inherit this

# Hosts tab: column A is the dropdown source, B..E feed the TEMPLATE header.
HOSTS_HEADERS = ["Name", "City", "Email", "Phone", "Mobilize Link"]
HOSTS_COL_CITY, HOSTS_COL_EMAIL, HOSTS_COL_PHONE, HOSTS_COL_LINK = 2, 3, 4, 5

README_TAB = "README (sync)"

READMES = [
    ["EP Hub Host Tracker -- how the automated parts work"],
    [""],
    ["The 'Volunteer Landing Page' tab is refreshed every night from Common "
     "Cause's volunteer data sync. It lists everyone who has submitted one of "
     "this state's EP quizzes."],
    [""],
    [f"- Columns A through {chr(ord('A') + MIRROR_WIDTH - 1)} are ONE array "
     f"formula in A1. Do not type in them -- the refresh owns them, and an "
     f"edit there will either be rejected or wiped overnight."],
    [f"- Column {HOST_COL_A1} ('Assigned Host') is yours. Pick a host from the "
     f"dropdown; the list comes from the 'Hosts' tab. The sync never writes "
     f"this column."],
    ["- Do NOT sort, insert or delete rows on the landing page. Assignments "
     "line up with the data by ROW POSITION, so sorting reassigns volunteers "
     "to the wrong hosts. Use a filter view instead."],
    ["- Rows are never removed and never reordered. New quiz completers "
     "append at the bottom. If someone's quiz record is deleted in Airtable, "
     "their row stays put and 'In Quiz Base?' flips to No -- so your "
     "assignments below them don't shift."],
    [""],
    ["To add a host: add their name (and city / email / phone / Mobilize "
     "link) to the 'Hosts' tab, then duplicate the TEMPLATE tab and pick that "
     "name from the dropdown in A1. The header details and the whole "
     "Distribution List fill in automatically."],
    [""],
    ["'Shifted?' means the volunteer has a CURRENT shift signed up in PTV. It "
     "drops to No for everyone when the state wipes its primary shifts to "
     "build the general -- that is expected, not a bug. 'Ever Shifted?' is "
     "latched from all-time shift history and survives the wipe."],
    [""],
    ["Questions: rkerth@commoncause.org"],
]


# -- Stage 1: BigQuery ------------------------------------------------------


def _quote_list(values: Sequence[str]) -> str:
    """SQL string list, with quotes escaped -- registry values reach the SQL."""
    return ", ".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
                     for v in values)


def load_targets(bq: BigQueryConnector) -> List[Dict[str, Any]]:
    """Read enabled trackers from the registry, applying tab-name defaults."""
    sql = f"""
        SELECT target_key, state, spreadsheet_id, quiz_sources,
               landing_tab, hosts_tab, template_tab, data_tab, notes
        FROM `{REGISTRY_TABLE}`
        WHERE enabled
        ORDER BY target_key
    """
    targets = []
    for row in bq.query(sql):
        t = dict(row)
        t["quiz_sources"] = [dict(s) for s in (t.get("quiz_sources") or [])]
        if not t["quiz_sources"]:
            raise ValueError(
                f"tracker '{t['target_key']}' has no quiz_sources -- an "
                f"enabled row with nothing to read is a registration mistake, "
                f"not an empty page"
            )
        t["landing_tab"] = t.get("landing_tab") or "Volunteer Landing Page"
        t["hosts_tab"] = t.get("hosts_tab") or "Hosts"
        t["template_tab"] = t.get("template_tab") or "TEMPLATE"
        t["data_tab"] = t.get("data_tab") or "_data"
        targets.append(t)
    return targets


# Quiz-specific fields that are NOT part of the ep_2026_cleaned.quiz_responses
# contract, so they come from the per-base typed tables. Presence is checked
# per base: a state's quiz that never asked for a vest size simply yields NULL
# rather than breaking the extract, so registering a new base stays an INSERT.
EXTRA_FIELDS = ["address", "vest_size", "toolkit_identifier_request", "submitted"]


def _extras_union_sql(bq: BigQueryConnector, base_keys: List[str]) -> str:
    """One SELECT per quiz base, substituting NULL for columns it lacks."""
    sql = f"""
        SELECT table_name, column_name
        FROM `{BQ_PROJECT}.ep_2026_raw.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name IN ({_quote_list(
            [f'{b}__quiz_responses' for b in base_keys])})
    """
    present: Dict[str, set] = {b: set() for b in base_keys}
    for row in bq.query(sql):
        base = row["table_name"].replace("__quiz_responses", "")
        if base in present:
            present[base].add(row["column_name"])

    selects = []
    for base in base_keys:
        cols = present[base]
        if not cols:
            raise ValueError(
                f"quiz base '{base}' has no table "
                f"{BQ_PROJECT}.ep_2026_raw.{base}__quiz_responses -- register "
                f"it in ep.airtable_sync_sources and let "
                f"sync_airtable_bases.py land it first"
            )
        picks = []
        for field in EXTRA_FIELDS:
            if field in cols:
                picks.append(f"{field}")
            else:
                logger.warning(
                    "[hubhost] quiz base '%s' has no '%s' column -- treating "
                    "as NULL", base, field,
                )
                cast = "TIMESTAMP" if field == "submitted" else "STRING"
                picks.append(f"CAST(NULL AS {cast}) AS {field}")
        selects.append(
            f"SELECT '{base}' AS base_key, _airtable_record_id AS record_id, "
            f"{', '.join(picks)} "
            f"FROM `{BQ_PROJECT}.ep_2026_raw.{base}__quiz_responses`"
        )
    return "\n      UNION ALL\n      ".join(selects)


def _extract_sql(target: Dict[str, Any], extras_union: str) -> str:
    """
    One row per volunteer who has submitted any of the tracker's quizzes.

    Deliberately NOT filtered to passing scores: MI wants everyone who took a
    quiz on the page (the score is shown so staff can see who has not passed).
    """
    state = target["state"].replace("'", "\\'")
    base_keys = [s["base_key"] for s in target["quiz_sources"]]
    return f"""
WITH q AS (
  SELECT record_id, base_key, created_at, email, email_raw, full_name,
         score, score_max
  FROM `{BQ_PROJECT}.ep_2026_cleaned.quiz_responses`
  WHERE state = '{state}' AND base_key IN ({_quote_list(base_keys)})
),
x AS (
      {extras_union}
),
j AS (
  SELECT q.*, x.address, x.vest_size, x.toolkit_identifier_request AS toolkit,
         COALESCE(x.submitted, q.created_at) AS submitted_at,
         -- A blank email can't identify a person, so such rows stay
         -- per-record rather than collapsing into one another.
         COALESCE(NULLIF(q.email, ''), CONCAT('rec:', q.record_id)) AS person_key
  FROM q LEFT JOIN x USING (base_key, record_id)
),
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY person_key
                       ORDER BY submitted_at DESC, record_id DESC) AS rn_new,
    ROW_NUMBER() OVER (PARTITION BY person_key
                       ORDER BY submitted_at ASC,  record_id ASC)  AS rn_old
  FROM j
),
person AS (
  -- Details from the LATEST submission (a retake supersedes); the ledger key
  -- and sort position from the FIRST, so a retake can never move a row.
  SELECT
    person_key,
    MAX(IF(rn_old = 1, record_id, NULL))    AS anchor_key,
    MAX(IF(rn_old = 1, submitted_at, NULL)) AS first_submitted,
    MAX(IF(rn_new = 1, full_name, NULL))    AS full_name,
    MAX(IF(rn_new = 1, email_raw, NULL))    AS email_raw,
    MAX(IF(rn_new = 1, email, NULL))        AS email,
    MAX(IF(rn_new = 1, address, NULL))      AS address,
    MAX(IF(rn_new = 1, vest_size, NULL))    AS vest_size,
    MAX(IF(rn_new = 1, toolkit, NULL))      AS toolkit,
    MAX(IF(rn_new = 1, submitted_at, NULL)) AS latest_submitted,
    MAX(IF(rn_new = 1, score, NULL))        AS score,
    MAX(IF(rn_new = 1, score_max, NULL))    AS score_max,
    STRING_AGG(DISTINCT base_key, '|' ORDER BY base_key) AS base_keys,
    COUNT(*) AS submissions
  FROM ranked
  GROUP BY person_key
),
vols AS (
  -- Grouped defensively: the cleaned view can carry more than one row per
  -- (state, email) and a fan-out here would duplicate a kit.
  SELECT email,
         ANY_VALUE(phone) AS phone, ANY_VALUE(county) AS county,
         ANY_VALUE(zip_code) AS zip_code,
         MAX(shift_count) AS shift_count,
         MIN(first_shift_date) AS first_shift_date,
         MAX(latest_shift_date) AS latest_shift_date
  FROM `{BQ_PROJECT}.ep_2026_cleaned.volunteers`
  WHERE state = '{state}' AND email IS NOT NULL AND email != ''
  GROUP BY email
),
ever AS (
  -- All-time daily snapshots, so this survives a primary -> general wipe.
  SELECT `{BQ_PROJECT}.ep_2026_cleaned`.norm_email(email) AS email,
         COUNT(DISTINCT shift_id) AS shifts_ever
  FROM `{BQ_PROJECT}.ptv_raw_2026.shift_volunteers`
  WHERE UPPER(state) = '{state}' AND email IS NOT NULL AND email != ''
  GROUP BY 1
)
SELECT
  p.anchor_key,
  p.full_name,
  p.address                                      AS location,
  p.email_raw                                    AS contact_email,
  v.phone                                        AS contact_phone,
  p.base_keys,
  IF(IFNULL(v.shift_count, 0) > 0, 'Yes', 'No')  AS shifted,
  -- Vest size is inlined onto the Vest line ("L/XL Vest") and the pick-up-only
  -- marker is shortened, so a host reads one packing string per volunteer.
  ARRAY_TO_STRING(ARRAY(
    SELECT CASE
      WHEN item = 'Vest' AND p.vest_size IS NOT NULL AND p.vest_size != ''
        THEN CONCAT(p.vest_size, ' Vest')
      ELSE REGEXP_REPLACE(item, r'\\(s\\)\\s*-\\s*pick up only', ' (pickup)')
    END
    FROM UNNEST(JSON_EXTRACT_STRING_ARRAY(p.toolkit)) item WITH OFFSET o
    ORDER BY o
  ), '; ')                                       AS requested_items,
  CONCAT(CAST(p.score AS STRING), '/', CAST(p.score_max AS STRING)) AS score,
  CAST(DATE(p.latest_submitted) AS STRING)       AS submitted,
  IFNULL(v.shift_count, 0)                       AS shifts,
  CAST(v.first_shift_date AS STRING)             AS first_shift,
  CAST(v.latest_shift_date AS STRING)            AS latest_shift,
  v.county,
  v.zip_code,
  IF(IFNULL(e.shifts_ever, 0) > 0, 'Yes', 'No')  AS ever_shifted,
  p.submissions
FROM person p
LEFT JOIN vols v ON v.email = p.email
LEFT JOIN ever e ON e.email = p.email
ORDER BY p.first_submitted, p.anchor_key
"""


def fetch_rows(bq: BigQueryConnector, target: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull the tracker's volunteers, already in append order."""
    base_keys = [s["base_key"] for s in target["quiz_sources"]]
    sql = _extract_sql(target, _extras_union_sql(bq, base_keys))
    rows = [dict(r) for r in bq.query(sql)]
    logger.info(
        "[hubhost] %s: %d volunteer(s) across %d quiz base(s)",
        target["target_key"], len(rows), len(base_keys),
    )
    return rows


# -- Stage 2: rows -> sheet values ------------------------------------------


def render_rows(
    rows: List[Dict[str, Any]], target: Dict[str, Any]
) -> List[List[str]]:
    """Extract dicts -> _data rows (no header), in DATA_COLUMNS order."""
    role_by_base = {s["base_key"]: s.get("role_label") or s["base_key"]
                    for s in target["quiz_sources"]}
    quiz_by_base = {s["base_key"]: s.get("quiz_label") or s["base_key"]
                    for s in target["quiz_sources"]}

    out = []
    for r in rows:
        bases = [b for b in (r.get("base_keys") or "").split("|") if b]
        enriched = dict(r)
        enriched["roles"] = "; ".join(role_by_base.get(b, b) for b in bases)
        enriched["quiz"] = "; ".join(quiz_by_base.get(b, b) for b in bases)
        enriched["in_quiz_base"] = "Yes"
        out.append([
            "" if enriched.get(key) is None else str(enriched.get(key))
            for _, key in DATA_COLUMNS
        ])
    return out


def _pad(row: Sequence[Any], width: int) -> List[str]:
    """Sheets truncates trailing empties on read; restore the rectangle."""
    vals = ["" if v is None else str(v) for v in row[:width]]
    return vals + [""] * (width - len(vals))


def merge_with_ledger(
    fresh: List[List[str]], prior: List[List[str]], target_key: str
) -> List[List[str]]:
    """
    Preserve the existing row order, append what's new, carry forward what
    vanished. See the module docstring -- this is what keeps the human-owned
    Assigned Host column aligned to the right volunteer.

    `prior` is the previous _data tab WITHOUT its header row.
    """
    width = len(DATA_COLUMNS)
    fresh_by_key = {row[KEY_INDEX]: row for row in fresh if row[KEY_INDEX]}

    # First occurrence wins, so a key duplicated by a hand-edit of the hidden
    # tab collapses instead of emitting the volunteer twice.
    prior_keys: List[str] = []
    prior_by_key: Dict[str, List[str]] = {}
    for raw in prior:
        row = _pad(raw, width)
        key = row[KEY_INDEX]
        if key and key not in prior_by_key:
            prior_keys.append(key)
            prior_by_key[key] = row

    merged: List[List[str]] = []
    carried = 0
    for key in prior_keys:
        if key in fresh_by_key:
            merged.append(fresh_by_key[key])
        else:
            row = list(prior_by_key[key])
            row[IN_QUIZ_BASE_INDEX] = "No -- record deleted"
            merged.append(row)
            carried += 1

    seen = set(prior_keys)
    appended = [row for row in fresh if row[KEY_INDEX] not in seen]
    merged.extend(appended)

    if carried:
        logger.warning(
            "[hubhost] %s: %d row(s) no longer in the quiz base(s) -- carried "
            "forward with last-known values and flagged, so host assignments "
            "below them stay aligned",
            target_key, carried,
        )
    logger.info(
        "[hubhost] %s: ledger = %d kept + %d new = %d row(s)",
        target_key, len(prior_keys), len(appended), len(merged),
    )
    return merged


# -- Stage 3: Google Sheets -------------------------------------------------


def _a1_tab(name: str) -> str:
    """Tab name as an A1 reference ('Volunteer Landing Page' -> quoted)."""
    return "'" + name.replace("'", "''") + "'"


def _mirror_formula(data_tab: str) -> str:
    return f"={{{_a1_tab(data_tab)}!{_MIRROR_FIRST}:{_MIRROR_LAST}}}"


def distro_formula(landing_tab: str) -> str:
    """
    The host-tab Distribution List: one FILTER returning the seven columns a
    host needs for everyone assigned to the name in A1.

    Matched on LOWER(TRIM(...)) so a stray space or a case difference between
    the Hosts tab and a hand-typed A1 doesn't silently return nothing. Guarded
    on a blank A1, which would otherwise match every unassigned row.
    """
    lp = _a1_tab(landing_tab)
    host = f"$A${TPL_HOST_CELL[1:]}"        # A1 -> $A$1, absolute per clone
    return (
        f'=IF({TPL_HOST_CELL}="","",IFERROR(FILTER('
        f"{lp}!$A$2:${DISTRO_LAST_A1},"
        f"LOWER(TRIM({lp}!${HOST_COL_A1}$2:${HOST_COL_A1}))"
        f'=LOWER(TRIM({host}))),""))'
    )


def _host_lookup(col: int) -> str:
    """A TEMPLATE header cell: look one field up from the Hosts tab by A1."""
    return (f'=IF({TPL_HOST_CELL}="","",IFERROR(VLOOKUP({TPL_HOST_CELL},'
            f"Hosts!$A:$E,{col},FALSE),\"\"))")


def _host_link_formula() -> str:
    """
    Mobilize link as a hyperlink. HYPERLINK("") renders as plain label text,
    which reads as a link that goes nowhere, so a blank link yields nothing
    at all until staff fill it in on the Hosts tab.
    """
    return (f'=IF({TPL_HOST_CELL}="","",IFERROR(LET(u,VLOOKUP('
            f"{TPL_HOST_CELL},Hosts!$A:$E,{HOSTS_COL_LINK},FALSE),"
            f'IF(u="","",HYPERLINK(u,"Host Events - Mobilize page"))),""))')


def _host_dropdown_request(
    sheet_id: int, hosts_tab: str,
    start_row: int, end_row: int, start_col: int, end_col: int,
) -> Dict[str, Any]:
    """setDataValidation limiting a range to names on the Hosts tab."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row, "endRowIndex": end_row,
                "startColumnIndex": start_col, "endColumnIndex": end_col,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_RANGE",
                    "values": [{
                        "userEnteredValue": f"={_a1_tab(hosts_tab)}!$A$2:$A"
                    }],
                },
                "showCustomUi": True,
                "strict": True,
                "inputMessage": f"Pick a host from the '{hosts_tab}' tab.",
            },
        }
    }


@retry_google_operation
def _ensure_landing_page(
    ss: gspread.Spreadsheet, target: Dict[str, Any], data_rows: int
) -> None:
    """
    Create/repair the visible landing page without touching human content.

    Grow-only resize; the mirror formula and the two human column headers are
    seeded ONLY when empty, so a staff edit is never overwritten. The dropdown
    is re-applied every run because its range has to keep up with the grid.
    """
    landing = target["landing_tab"]
    try:
        ws = ss.worksheet(landing)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(
            title=landing, rows=data_rows + ROW_BUFFER, cols=LANDING_MIN_COLS
        )
        ws.update_index(0)
        logger.info("[hubhost] created '%s' in '%s'", landing, ss.title)

    rows_needed = data_rows + ROW_BUFFER
    if ws.row_count < rows_needed or ws.col_count < LANDING_MIN_COLS:
        ws.resize(rows=max(ws.row_count, rows_needed),
                  cols=max(ws.col_count, LANDING_MIN_COLS))

    if not ws.acell("A1").value:
        ws.update(range_name="A1",
                  values=[[_mirror_formula(target["data_tab"])]],
                  value_input_option="USER_ENTERED")
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        logger.info("[hubhost] seeded mirror formula in '%s'", landing)

    for cell, label in ((f"{HOST_COL_A1}1", "Assigned Host"),
                        (f"{NOTES_COL_A1}1", "Notes")):
        if not ws.acell(cell).value:
            ws.update(range_name=cell, values=[[label]],
                      value_input_option="RAW")

    ss.batch_update({"requests": [_host_dropdown_request(
        ws.id, target["hosts_tab"],
        start_row=1, end_row=ws.row_count,
        start_col=HOST_COL_INDEX0, end_col=HOST_COL_INDEX0 + 1,
    )]})


@retry_google_operation
def _read_prior_data(ss: gspread.Spreadsheet, data_tab: str) -> List[List[str]]:
    """Previous _data rows (header dropped). Empty list on a first-ever run."""
    try:
        ws = ss.worksheet(data_tab)
    except gspread.WorksheetNotFound:
        return []
    values = ws.get_all_values()
    if not values:
        return []
    if values[0][:1] != [HEADERS[0]]:
        raise RuntimeError(
            f"'{data_tab}' does not start with the expected ledger header "
            f"'{HEADERS[0]}' -- refusing to rewrite it, because without the "
            f"ledger the row order (and every host assignment aligned to it) "
            f"cannot be preserved"
        )
    return values[1:]


@retry_google_operation
def _hide_tab(ss: gspread.Spreadsheet, title: str) -> None:
    ws = ss.worksheet(title)
    if not ws.isSheetHidden:
        ws.hide()


def sync_target(
    writer: SheetsWriterConnector,
    bq: BigQueryConnector,
    target: Dict[str, Any],
) -> int:
    """Refresh one tracker's landing page. Returns the row count written."""
    ss = writer.open_spreadsheet(target["spreadsheet_id"])
    fresh = render_rows(fetch_rows(bq, target), target)

    # Read the ledger BEFORE write_worksheet clears the tab.
    prior = _read_prior_data(ss, target["data_tab"])
    merged = merge_with_ledger(fresh, prior, target["target_key"])

    writer.write_worksheet(ss, target["data_tab"], [HEADERS] + merged)
    writer.format_header_row(ss, target["data_tab"])
    _ensure_landing_page(ss, target, data_rows=len(merged) + 1)
    writer.write_worksheet(ss, README_TAB, READMES)
    _hide_tab(ss, target["data_tab"])
    logger.info(
        "[hubhost] %s: %d row(s) -> '%s' / '%s'",
        target["target_key"], len(merged), ss.title, target["landing_tab"],
    )
    return len(merged)


# -- Scaffolding (hand-run, not nightly) ------------------------------------


@retry_google_operation
def _harvest_host_details(
    ss: gspread.Spreadsheet, ws: gspread.Worksheet, hosts_tab: str
) -> None:
    """
    Before overwriting a host tab's header cells with lookups, promote what is
    already typed there into that host's Hosts row -- but only into cells that
    are still blank, so the Hosts tab stays the staff-owned source of truth.
    """
    name = (ws.acell(TPL_HOST_CELL).value or "").strip()
    if not name:
        return
    typed = {
        HOSTS_COL_CITY: (ws.acell(TPL_CITY_CELL).value or "").strip(),
        HOSTS_COL_EMAIL: (ws.acell(TPL_EMAIL_CELL).value or "").strip(),
        HOSTS_COL_PHONE: (ws.acell(TPL_PHONE_CELL).value or "").strip(),
    }
    # Obvious placeholders from the pre-automation template, not real values.
    typed = {c: v for c, v in typed.items()
             if v and not v.isupper() and "REPLACE" not in v.upper()}
    if not typed:
        return

    hosts = ss.worksheet(hosts_tab)
    names = hosts.col_values(1)
    try:
        row = next(i for i, n in enumerate(names, start=1)
                   if (n or "").strip().lower() == name.lower())
    except StopIteration:
        row = len(names) + 1
        hosts.update_cell(row, 1, name)
        logger.info("[hubhost] added host '%s' to '%s' from tab '%s'",
                    name, hosts_tab, ws.title)

    existing = _pad(hosts.row_values(row), len(HOSTS_HEADERS))
    for col, value in typed.items():
        if not existing[col - 1]:
            hosts.update_cell(row, col, value)
            logger.info("[hubhost] promoted %s for '%s' into '%s'",
                        HOSTS_HEADERS[col - 1], name, hosts_tab)


TPL_HOST_NOTE = (
    "Pick the host from this dropdown.\n\n"
    "Their phone, city, email and Mobilize link fill in around it, and the "
    "Distribution List to the right fills with every volunteer assigned to "
    "them on the Volunteer Landing Page.\n\n"
    "To assign volunteers, use the 'Assigned Host' dropdown on the Volunteer "
    "Landing Page."
)


@retry_google_operation
def _install_host_tab_formulas(
    ss: gspread.Spreadsheet, ws: gspread.Worksheet, target: Dict[str, Any],
    is_template: bool = False,
) -> None:
    """Install the header lookups, the A1 dropdown and the distro FILTER."""
    if ws.row_count < TPL_MIN_ROWS or ws.col_count < 16:
        ws.resize(rows=max(ws.row_count, TPL_MIN_ROWS),
                  cols=max(ws.col_count, 16))

    if is_template:
        # A placeholder like "REPLACE WITH HOST NAME" is not a valid host, so
        # strict validation would red-flag the template's own A1. Leave it
        # empty -- the dropdown arrow and the cell note are the instruction.
        ws.batch_clear([TPL_HOST_CELL])
        ss.batch_update({"requests": [{
            "updateCells": {
                "range": {"sheetId": ws.id,
                          "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rows": [{"values": [{"note": TPL_HOST_NOTE}]}],
                "fields": "note",
            }
        }]})

    # Clear the hand-typed example row, then let the FILTER own G7 down.
    ws.batch_clear([
        f"{TPL_DISTRO_FIRST_COL_A1}{TPL_DISTRO_FIRST_ROW}:"
        f"O{TPL_DISTRO_FIRST_ROW}"
    ])
    ws.batch_update([
        {"range": TPL_PHONE_CELL, "values": [[_host_lookup(HOSTS_COL_PHONE)]]},
        {"range": TPL_LINK_CELL,  "values": [[_host_link_formula()]]},
        {"range": TPL_CITY_CELL,  "values": [[_host_lookup(HOSTS_COL_CITY)]]},
        {"range": TPL_EMAIL_CELL, "values": [[_host_lookup(HOSTS_COL_EMAIL)]]},
        {"range": f"{TPL_DISTRO_FIRST_COL_A1}{TPL_DISTRO_FIRST_ROW}",
         "values": [[distro_formula(target["landing_tab"])]]},
    ], value_input_option="USER_ENTERED")

    ss.batch_update({"requests": [_host_dropdown_request(
        ws.id, target["hosts_tab"],
        start_row=0, end_row=1, start_col=0, end_col=1,
    )]})
    logger.info("[hubhost] installed formulas on tab '%s'", ws.title)


@retry_google_operation
def _retrofit_legacy_header(ss: gspread.Spreadsheet, ws: gspread.Worksheet) -> bool:
    """
    Bring a host tab cloned from the pre-Role(s) TEMPLATE up to date.

    The old Distribution List ran Name | Location | email | Phone | Shifted? ...
    which is one column short, so the FILTER's seven columns would land on the
    wrong headers. Insert a column before K and label it.
    """
    header = _pad(ws.row_values(TPL_DISTRO_HEADER_ROW), 15)
    if header[6] == HEADERS[1] and header[10] == HEADERS[5]:
        return False  # already the current layout
    if header[6] != "Name" or header[10] != HEADERS[6]:
        logger.warning(
            "[hubhost] tab '%s' row %d matches neither the current nor the "
            "known legacy Distribution List layout -- skipping, fix by hand",
            ws.title, TPL_DISTRO_HEADER_ROW,
        )
        return False

    ss.batch_update({"requests": [{
        "insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": 10, "endIndex": 11},
            "inheritFromBefore": False,
        }
    }]})
    ws.batch_update([
        {"range": f"G{TPL_DISTRO_HEADER_ROW}", "values": [[HEADERS[1]]]},
        {"range": f"K{TPL_DISTRO_HEADER_ROW}", "values": [[HEADERS[5]]]},
    ], value_input_option="RAW")
    logger.info("[hubhost] retrofitted legacy Distribution List on '%s' "
                "(inserted the Role(s) column)", ws.title)
    return True


def _is_host_tab(ws: gspread.Worksheet, target: Dict[str, Any]) -> bool:
    """A TEMPLATE clone: not one of the job/registry tabs, but shaped like one."""
    reserved = {target["landing_tab"], target["hosts_tab"],
                target["template_tab"], target["data_tab"], README_TAB}
    if ws.title in reserved:
        return False
    header = _pad(ws.row_values(TPL_DISTRO_HEADER_ROW), 15)
    return header[6] in (HEADERS[1], "Name") and header[7] == HEADERS[2]


def install_scaffolding(
    writer: SheetsWriterConnector, target: Dict[str, Any]
) -> None:
    """
    One-time (idempotent) structural setup for one tracker: the Hosts tab
    headers, the TEMPLATE formulas, and the same treatment for host tabs that
    already exist.

    Deliberately NOT on the nightly path. TEMPLATE's kit checklist in A:E and
    its instructions cell are program-staff content; a job that rewrote that
    tab every night would eventually fight whoever is editing it.
    """
    ss = writer.open_spreadsheet(target["spreadsheet_id"])

    hosts = writer.get_or_add_worksheet(ss, target["hosts_tab"])
    if hosts.col_count < len(HOSTS_HEADERS):
        hosts.resize(rows=max(hosts.row_count, 2), cols=len(HOSTS_HEADERS))
    current = _pad(hosts.row_values(1), len(HOSTS_HEADERS))
    for i, label in enumerate(HOSTS_HEADERS):
        if not current[i]:
            hosts.update_cell(1, i + 1, label)
    hosts.freeze(rows=1)
    hosts.format("1:1", {"textFormat": {"bold": True}})
    logger.info("[hubhost] %s: Hosts tab headers ensured",
                target["target_key"])

    for ws in ss.worksheets():
        if ws.title == target["template_tab"]:
            _install_host_tab_formulas(ss, ws, target, is_template=True)
        elif _is_host_tab(ws, target):
            _harvest_host_details(ss, ws, target["hosts_tab"])
            _retrofit_legacy_header(ss, ws)
            _install_host_tab_formulas(ss, ws, target)


# -- Entry points ----------------------------------------------------------


def _report_dry_run(rows: List[Dict[str, Any]], target: Dict[str, Any]) -> None:
    """
    Counts only. The extract is volunteer names, mailing addresses, emails and
    phones -- none of that belongs in a terminal, a log or a ticket.
    """
    from collections import Counter

    logger.info("[hubhost] %s DRY RUN -- %d row(s), nothing written",
                target["target_key"], len(rows))
    logger.info("[hubhost]   by quiz base: %s",
                dict(Counter(r["base_keys"] for r in rows)))
    logger.info("[hubhost]   currently shifted: %d / ever shifted: %d",
                sum(1 for r in rows if r["shifted"] == "Yes"),
                sum(1 for r in rows if r["ever_shifted"] == "Yes"))
    logger.info("[hubhost]   scores: %s",
                dict(Counter(r["score"] for r in rows)))
    for field, label in (("contact_phone", "phone"),
                         ("location", "address"),
                         ("county", "county")):
        logger.info("[hubhost]   missing %s: %d",
                    label, sum(1 for r in rows if not r.get(field)))
    logger.info("[hubhost]   requested items present: %d / repeat submitters: %d",
                sum(1 for r in rows if r["requested_items"]),
                sum(1 for r in rows if (r.get("submissions") or 1) > 1))


def run(
    dry_run: bool = False,
    only: Optional[List[str]] = None,
    scaffolding: bool = False,
) -> None:
    """
    Refresh every enabled tracker. The scheduled path calls run() with no args.

    Per-target failures are isolated so one state's broken sheet can't stop the
    others; the raise at the end is what makes the runner (and Civis) notice.
    """
    with BigQueryConnector(project_id=BQ_PROJECT) as bq:
        targets = load_targets(bq)
        if only:
            wanted = {k.strip().lower() for k in only if k.strip()}
            targets = [t for t in targets
                       if t["target_key"].lower() in wanted]
            missing = wanted - {t["target_key"].lower() for t in targets}
            if missing:
                raise ValueError(
                    f"unknown or disabled tracker key(s): {sorted(missing)}"
                )
        if not targets:
            logger.warning("[hubhost] no enabled trackers in %s -- nothing to do",
                           REGISTRY_TABLE)
            return
        logger.info("[hubhost] === %d tracker(s) ===", len(targets))

        if dry_run:
            for target in targets:
                _report_dry_run(fetch_rows(bq, target), target)
            return

        failed: List[str] = []
        with SheetsWriterConnector() as writer:
            for target in targets:
                try:
                    if scaffolding:
                        install_scaffolding(writer, target)
                    sync_target(writer, bq, target)
                except Exception as e:
                    logger.exception("[hubhost] %s: FAILED -- %s",
                                     target["target_key"], e)
                    failed.append(target["target_key"])

    if failed:
        raise RuntimeError(f"hub host tracker sync failed for: {failed}")


if __name__ == "__main__":
    # Standalone dev entrypoint; the scheduled path is run_misc_jobs.py.
    import argparse
    import logging as _logging
    import sys

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the extract and report counts only; write nothing.",
    )
    parser.add_argument(
        "--targets",
        help="Comma-separated tracker keys instead of all enabled "
             "(ops / testing). e.g. --targets MI",
    )
    parser.add_argument(
        "--install-scaffolding", action="store_true",
        help="Also install the structural pieces the nightly path leaves "
             "alone: Hosts tab headers, TEMPLATE formulas + host dropdown, "
             "and the same treatment (including the legacy Role(s) column "
             "retrofit) for host tabs that already exist. Idempotent.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        run(
            dry_run=args.dry_run,
            only=args.targets.split(",") if args.targets else None,
            scaffolding=args.install_scaffolding,
        )
    except Exception as e:
        _logging.getLogger(__name__).error("%s", e)
        sys.exit(1)

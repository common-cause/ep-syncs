"""
Sync the PTV volunteer roster -> Google Sheets exports for states and
coalition partners.

For each enabled row in proj-tmc-mem-com.ep.volunteer_sheet_targets, maintain
one spreadsheet in the "2026 EP Volunteer Exports" shared-drive folder
(state targets under By State/, source_code targets under By Partner/), with
three job-managed tabs:

  Volunteers  (visible) -- mirrors _data via one array formula in A1;
                           partners annotate in columns to the right
  README      (visible) -- usage instructions; rewritten every run
  _data       (hidden)  -- the extract; cleared and rewritten every run

Partner edits are never overwritten: the job only rewrites _data and README,
and re-seeds Volunteers!A1 only when it is empty. Row alignment for partner
annotations is preserved by an append-only contract: the extract is the
all-time roster (rows never disappear -- an Active column marks currency)
in stable PTV-id order, so new registrations always append at the bottom.
Partner-sheet membership is by *any* snapshot's source_code, not just the
latest, so a code edit in PTV can't silently pull rows out of a sheet.

See docs/volunteer_sheets_spec.md for the design and
bq/volunteer_sheet_targets.sql for the registry contract (inserting an
enabled registry row is how a new sheet comes into existence).

Per-target failures are isolated. Exit code is non-zero if any selected
target failed.

Usage:
    python sync_volunteer_sheets.py                    # all enabled targets
    python sync_volunteer_sheets.py --targets NE,aclum # subset (ops/testing)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from typing import Any, Dict, List

import gspread
from dotenv import load_dotenv

from ccef_connections import BigQueryConnector, SheetsWriterConnector
from ccef_connections.core.retry import retry_google_operation


# -- Constants --------------------------------------------------------------

PROJECT = "proj-tmc-mem-com"
TARGETS_TABLE = f"{PROJECT}.ep.volunteer_sheet_targets"
USERS_TABLE = f"{PROJECT}.ptv_raw_2026.users"
SHIFT_VIEW = f"{PROJECT}.ptv_raw_2026.v_shift_volunteers_current"

# "2026 EP Volunteer Exports" shared-drive folder and its subfolders
# (created once, 2026-07-08; the SA is a member of the shared drive).
ROOT_FOLDER_ID = "18oLarQuNErA7qMMz0YBRKEZyP-3eQDMS"
FOLDER_BY_TYPE = {
    "state": "1KkTrm93PmNNEVH_drxdD8b_hYHlLVKcC",        # By State/
    "source_code": "1apbIxjIQ2e5DRHYtvSCGFyMGpVaG-eKJ",  # By Partner/
}

DATA_TAB = "_data"
VISIBLE_TAB = "Volunteers"
README_TAB = "README"

# (header, roster-row key) in sheet column order. The mirror formula range
# is derived from this list's length -- keep it <= 26 columns.
COLUMNS = [
    ("PTV ID", "id"),
    ("First Name", "first_name"),
    ("Last Name", "last_name"),
    ("Email", "email"),
    ("Phone", "phone_number"),
    ("County", "county"),
    ("Zip", "zip_code"),
    ("State", "state"),
    ("Join Date", "join_date"),
    ("Source Code", "source_code"),
    ("Role", "role"),
    ("Training", "training"),
    ("Shifts Claimed", "shift_count"),
    ("Upcoming Shifts", "upcoming_shift_count"),
    ("First Shift", "first_shift_date"),
    ("Latest Shift", "latest_shift_date"),
    ("Active", "active"),
    ("Last Seen", "last_seen"),
]
assert len(COLUMNS) <= 26
_LAST_COL = chr(ord("A") + len(COLUMNS) - 1)
MIRROR_FORMULA = f"={{{DATA_TAB}!A:{_LAST_COL}}}"

# Spare grid space on the Volunteers tab so the array formula has room to
# spill as the roster grows between runs, plus columns for partner notes.
# The tab is only ever grown, never shrunk (partner content may be there).
ROW_BUFFER = 200
ANNOTATION_COLS = 6

# Pacing against the Sheets 60-write-requests/min/user quota. Refreshing an
# existing sheet is ~5 writes; creating a new one is ~9. Per-call 429s are
# retried with backoff by @retry_google_operation, but its cumulative wait
# (~15s) is shorter than the quota window, so a failed target also gets one
# full-quota-window cooldown retry before being marked failed.
TARGET_PAUSE_SECONDS = 2.0
QUOTA_COOLDOWN_SECONDS = 65

# Codes that should never be flagged as "unregistered partner code" in the
# end-of-run report: CC's own channels, the returning-volunteer bucket, and
# codes reviewed and deliberately left out of the registry.
UNREGISTERED_REPORT_IGNORE = {
    "previous_years", "cc", "adwords", "actionnetwork", "quiz",
}
UNREGISTERED_REPORT_MIN_VOLS = 25

README_ROWS = [
    ["EP Volunteer Export -- how this sheet works"],
    [""],
    ["This sheet is refreshed automatically every morning from Protect the "
     "Vote by Common Cause's volunteer data sync."],
    [""],
    ["- The 'Volunteers' tab mirrors the latest data. Add your own notes in "
     "the empty columns to the RIGHT of the data block, or on new tabs."],
    ["- Do NOT sort, insert, or delete rows on the Volunteers tab -- that "
     "breaks the alignment between the data and your notes. To sort or "
     "filter, use a filter view or copy data to another tab."],
    ["- Do NOT type inside the data block (columns A through "
     f"{_LAST_COL}); the refresh owns those columns."],
    ["- Rows are never removed. If a volunteer leaves PTV, their row stays "
     "and the Active column flips to N. New volunteers appear at the bottom."],
    ["- 'Shifts Claimed' counts actual shift signups and is authoritative; "
     "it comes from PTV's shift data, not the registration record."],
    [""],
    ["Questions: rkerth@commoncause.org"],
]

logger = logging.getLogger(__name__)


# -- Stage 1: BigQuery -> memory --------------------------------------------

ROSTER_SQL = f"""
WITH ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY state, LOWER(email)
      ORDER BY as_of_date DESC
    ) AS rn
  FROM `{USERS_TABLE}`
  WHERE email IS NOT NULL AND email != ''
),
codes_ever AS (
  SELECT
    state,
    LOWER(email) AS email_lower,
    ARRAY_AGG(DISTINCT LOWER(source_code) IGNORE NULLS) AS codes
  FROM `{USERS_TABLE}`
  WHERE email IS NOT NULL AND email != ''
  GROUP BY state, email_lower
),
latest AS (
  SELECT state, MAX(as_of_date) AS latest_date
  FROM `{USERS_TABLE}`
  GROUP BY state
)
SELECT
  r.id,
  r.first_name,
  r.last_name,
  r.email,
  r.phone_number,
  r.county,
  r.zip_code,
  r.state,
  CAST(DATE(SAFE_CAST(r.join_date AS TIMESTAMP)) AS STRING) AS join_date,
  r.source_code,
  r.role,
  r.training,
  IFNULL(s.shift_count, 0) AS shift_count,
  IFNULL(s.upcoming_shift_count, 0) AS upcoming_shift_count,
  CAST(s.first_shift_date AS STRING) AS first_shift_date,
  CAST(s.latest_shift_date AS STRING) AS latest_shift_date,
  IF(r.as_of_date = l.latest_date, 'Y', 'N') AS active,
  CAST(r.as_of_date AS STRING) AS last_seen,
  ce.codes AS codes_ever
FROM ranked r
JOIN latest l USING (state)
JOIN codes_ever ce
  ON ce.state = r.state AND ce.email_lower = LOWER(r.email)
LEFT JOIN `{SHIFT_VIEW}` s
  ON s.state = r.state AND LOWER(s.email) = LOWER(r.email)
WHERE r.rn = 1
-- Append-only contract: stable order so partner annotations to the right of
-- the mirror block never misalign. PTV ids increase with registration, so
-- new volunteers always land at the bottom of every sheet.
ORDER BY (r.id IS NULL), r.id, r.state, LOWER(r.email)
"""


def load_targets(bq: BigQueryConnector) -> List[Dict[str, Any]]:
    """Read enabled sheet targets from the registry."""
    sql = f"""
        SELECT target_key, target_type, sheet_title, source_codes, share_with
        FROM `{TARGETS_TABLE}`
        WHERE enabled
        ORDER BY target_type, target_key
    """
    targets = [dict(row) for row in bq.query(sql)]
    for t in targets:
        t["source_codes"] = set(t.get("source_codes") or [])
        t["share_with"] = list(t.get("share_with") or [])
    return targets


def fetch_roster(bq: BigQueryConnector) -> List[Dict[str, Any]]:
    """Pull the all-time volunteer roster, already in sheet row order."""
    roster = [dict(row) for row in bq.query(ROSTER_SQL)]
    logger.info(f"[BQ] roster: {len(roster)} all-time volunteer rows")
    return roster


def select_rows(
    roster: List[Dict[str, Any]], target: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Rows belonging to a target, preserving roster (stable) order."""
    if target["target_type"] == "state":
        return [r for r in roster if r["state"] == target["target_key"]]
    codes = target["source_codes"]
    return [r for r in roster if codes.intersection(r["codes_ever"] or [])]


# -- Stage 2: memory -> Google Sheets ----------------------------------------


def render_data(rows: List[Dict[str, Any]]) -> List[List[Any]]:
    """Roster dicts -> 2D values for the _data tab (row 0 = header)."""
    data = [[header for header, _ in COLUMNS]]
    for r in rows:
        data.append(
            ["" if r[key] is None else r[key] for _, key in COLUMNS]
        )
    return data


@retry_google_operation
def _ensure_visible_tab(ss: gspread.Spreadsheet, data_rows: int) -> None:
    """
    Create/repair the partner-facing Volunteers tab without ever touching
    partner content: grow-only resize, and (re)seed the mirror formula only
    when A1 is empty.
    """
    created = False
    try:
        ws = ss.worksheet(VISIBLE_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(
            title=VISIBLE_TAB,
            rows=data_rows + ROW_BUFFER,
            cols=len(COLUMNS) + ANNOTATION_COLS,
        )
        created = True

    rows_needed = data_rows + ROW_BUFFER
    if ws.row_count < rows_needed or ws.col_count < len(COLUMNS):
        ws.resize(
            rows=max(ws.row_count, rows_needed),
            cols=max(ws.col_count, len(COLUMNS)),
        )

    if not ws.acell("A1").value:
        ws.update(
            range_name="A1",
            values=[[MIRROR_FORMULA]],
            value_input_option="USER_ENTERED",
        )
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        logger.info(f"[sheets] seeded mirror formula in '{ss.title}'")

    if created:
        ws.update_index(0)  # partner-facing tab first


@retry_google_operation
def _hide_data_tab(ss: gspread.Spreadsheet) -> None:
    ws = ss.worksheet(DATA_TAB)
    if not ws.isSheetHidden:
        ws.hide()


@retry_google_operation
def _ensure_shares(ss: gspread.Spreadsheet, emails: List[str]) -> None:
    """Grant writer access to any share_with emails not already on the file."""
    if not emails:
        return
    existing = {
        (p.get("emailAddress") or "").lower() for p in ss.list_permissions()
    }
    for email in emails:
        if email.lower() not in existing:
            ss.share(email, perm_type="user", role="writer")
            logger.info(f"[sheets] shared '{ss.title}' with {email}")


def sync_target(
    writer: SheetsWriterConnector,
    target: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> None:
    """Create/refresh one target's spreadsheet."""
    key = target["target_key"]
    folder_id = FOLDER_BY_TYPE[target["target_type"]]

    ss = writer.get_or_create_spreadsheet(
        target["sheet_title"], folder_id=folder_id
    )
    data = render_data(rows)
    writer.write_worksheet(ss, DATA_TAB, data)
    writer.format_header_row(ss, DATA_TAB)
    _ensure_visible_tab(ss, data_rows=len(data))
    writer.write_worksheet(ss, README_TAB, README_ROWS)
    _hide_data_tab(ss)
    writer.delete_worksheet_if_exists(ss, "Sheet1")
    _ensure_shares(ss, target["share_with"])
    logger.info(f"[sheets] {key}: {len(rows)} volunteers -> '{ss.title}'")


# -- Unregistered-code report -------------------------------------------------


def report_unregistered_codes(
    roster: List[Dict[str, Any]], targets: List[Dict[str, Any]]
) -> None:
    """
    Log active-volunteer source codes that no source_code target covers, so
    new partner codes (which must be registered by hand) don't go unnoticed.
    """
    registered = set()
    for t in targets:
        registered.update(t["source_codes"])

    counts: Counter = Counter()
    for r in roster:
        code = (r["source_code"] or "").strip().lower()
        if r["active"] != "Y" or not code:
            continue
        if code in registered or code in UNREGISTERED_REPORT_IGNORE:
            continue
        if code.startswith("email-") or "commoncause" in code:
            continue
        counts[code] += 1

    flagged = [
        (code, n) for code, n in counts.most_common()
        if n >= UNREGISTERED_REPORT_MIN_VOLS
    ]
    for code, n in flagged:
        logger.warning(
            f"[registry] unregistered source code '{code}' has {n} active "
            f"volunteers -- add to {TARGETS_TABLE} if it's a partner"
        )
    if not flagged:
        logger.info("[registry] no unregistered partner-sized codes")


# -- Main -------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync PTV volunteer roster -> Google Sheets exports."
    )
    p.add_argument(
        "--targets",
        help="Comma-separated target_keys to sync instead of all enabled "
             "targets (ops / testing). e.g. --targets NE,PA,aclum",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()

    args = _parse_args(argv)

    with BigQueryConnector() as bq:
        all_targets = load_targets(bq)
        roster = fetch_roster(bq)

    targets = all_targets
    if args.targets:
        wanted = {t.strip().lower() for t in args.targets.split(",") if t.strip()}
        targets = [t for t in targets if t["target_key"].lower() in wanted]
        missing = wanted - {t["target_key"].lower() for t in targets}
        if missing:
            logger.error(f"unknown/disabled target keys: {sorted(missing)}")
            return 1

    logger.info(f"=== Volunteer sheets sync -- targets={len(targets)} ===")

    failed: List[str] = []
    with SheetsWriterConnector() as writer:
        for target in targets:
            rows = select_rows(roster, target)
            for attempt in (1, 2):
                try:
                    sync_target(writer, target, rows)
                    break
                except Exception as e:
                    if attempt == 1:
                        logger.warning(
                            f"[sheets] {target['target_key']}: failed "
                            f"({e}); retrying after "
                            f"{QUOTA_COOLDOWN_SECONDS}s cooldown"
                        )
                        time.sleep(QUOTA_COOLDOWN_SECONDS)
                    else:
                        logger.exception(
                            f"[sheets] {target['target_key']}: failed -- {e}"
                        )
                        failed.append(target["target_key"])
            time.sleep(TARGET_PAUSE_SECONDS)

    # Report against every enabled target (not just the ones selected this
    # run) so a --targets subset doesn't flag registered codes as missing.
    report_unregistered_codes(roster, all_targets)

    logger.info(f"=== Done. targets_ok={len(targets) - len(failed)} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

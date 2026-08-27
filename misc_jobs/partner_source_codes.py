"""
2026 EP Partner Engagement Form -> ep_2026_raw.partner_source_codes (BigQuery).

Captures the ISSUING side of a recruitment source code: which code was minted
for which partner organization, and whether anyone has handed them their
volunteer spreadsheet yet.

Why it matters: every other view of a source code in the warehouse is
downstream of a volunteer using one. `ptv_raw_2026.users` only knows codes
people signed up under, and `ep_dashboards.source_code_map`'s detection surface
is that same live traffic. So a code issued but not yet used is invisible, and
a code whose spelling drifted between issuance and capture collects volunteers
that land in no partner sheet, with no error anywhere -- `pbvrc` had 43 of them
before 2026-08-26. This module makes the issuing side queryable.

Two tabs, one table (`source_tab` tells them apart):
  'Form Responses 1'  the live intake -- one row per partner submission
  'Source Codes'      a hand-curated summary tab, org / link / spreadsheet
They overlap and sometimes disagree. Both are captured verbatim rather than
reconciled here, because which one is right is exactly the question a human has
to answer -- see ep_2026_cleaned.source_code_resolution.

Deliberately dumb, like misc_jobs/infrastructure_sheet.py: cells land verbatim
and every bit of interpretation (the ?source= parse, the join to live volume,
the needs-a-human verdict) lives in the cleaned view, so a parse fix is a view
change and never needs a re-scrape.

PII: NARROW BY DESIGN. The form also collects the submitter's name, email,
social handles and free-text comments; none of it is ingested. It is not needed
to reconcile a code, and the policy is to point at access-controlled systems
rather than copy people-data into new ones. `org_label` is an organization.
The dry-run report is counts-only.

READ-ONLY toward the spreadsheet. EP program staff own it; nothing here writes
to it.

Failure modes it treats as loud, on purpose:
  - a missing/renamed tab raises, listing the worksheets it did find
  - a missing required Form Responses header raises, listing what it did see
    (the form is edited by staff; a renamed column must not become a silent gap)
  - an empty scrape refuses to write

Idempotency: DELETE the day's partition rows, then append via a LOAD JOB (not a
streaming insert), so there is no streaming buffer to block a same-day rerun.

Credentials come from the environment (BIGQUERY_CREDENTIALS_PASSWORD,
GOOGLE_SHEETS_CREDENTIALS_PASSWORD); run_misc_jobs.py loads .env before calling
run().
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from ccef_connections import BigQueryConnector, SheetsConnector

logger = logging.getLogger(__name__)

SPREADSHEET_ID = "1H3p3rzsRdJr4wnj9Pmn6Ck4CBMLv0w44gzHUNb9tb3g"

RESPONSES_TAB = "Form Responses 1"
SUMMARY_TAB = "Source Codes"

BQ_PROJECT = "proj-tmc-mem-com"
DATASET = "ep_2026_raw"
TARGET_TABLE = f"{BQ_PROJECT}.{DATASET}.partner_source_codes"
DDL_FILE = "partner_source_codes.sql"

# Matches the nightly runner's ET weekday convention -- see infrastructure_sheet.
SNAPSHOT_TZ = ZoneInfo("America/New_York")

# Form Responses columns, matched by exact header text. The form repeats several
# header names across its branches (Name, Email, Logo, ...), so every match is
# collected and the first non-empty value wins.
COL_TIMESTAMP = "Timestamp"
COL_ORG = "Organization Name"
COL_CODE = "Source Code"
COL_LINK = "Link"
COL_SPREADSHEET = "Spreadsheet"
# The "do you want a code" question appears in more than one branch of the form,
# once with a trailing asterisk. Prefix-matched rather than listed exactly.
WANTS_CODE_PREFIX = "Do you want a source code for recruitment?"

# Raising on these protects the reconciliation: without a code or a link there
# is nothing to reconcile, and a renamed org column would orphan every row.
REQUIRED_RESPONSE_HEADERS = (COL_TIMESTAMP, COL_ORG, COL_CODE, COL_LINK)

# Source Codes tab geometry (1-based, as seen in the UI).
SUMMARY_HEADER_ROW = 1
SUMMARY_FIRST_DATA_ROW = 2
SUMMARY_COL_ORG = 0
SUMMARY_COL_URL = 1
SUMMARY_COL_SHEET = 2


def _ddl_path(filename: str) -> Path:
    return Path(__file__).resolve().parent.parent / "bq" / filename


def _cell(row: List[str], idx: int) -> str:
    return (row[idx] if idx < len(row) else "") or ""


def _first_non_empty(row: List[str], indices: List[int]) -> str:
    """First non-blank value across repeated columns of the same header."""
    for idx in indices:
        value = _cell(row, idx).strip()
        if value:
            return value
    return ""


def parse_responses(
    rows: List[List[str]], as_of: datetime.date, synced_at: datetime.datetime
) -> List[Dict[str, Any]]:
    """One record per form submission. Raises if a required header is missing."""
    if len(rows) < 2:
        raise ValueError(
            f"{RESPONSES_TAB!r}: {len(rows)} rows -- expected a header plus "
            f"submissions; the tab is empty or its layout changed"
        )

    header = [h.strip() for h in rows[0]]

    def indices_for(name: str) -> List[int]:
        return [i for i, h in enumerate(header) if h == name]

    missing = [name for name in REQUIRED_RESPONSE_HEADERS if not indices_for(name)]
    if missing:
        raise ValueError(
            f"{RESPONSES_TAB!r}: required column(s) {missing} not found. "
            f"Headers present: {[h for h in header if h]}"
        )

    idx_ts = indices_for(COL_TIMESTAMP)
    idx_org = indices_for(COL_ORG)
    idx_code = indices_for(COL_CODE)
    idx_link = indices_for(COL_LINK)
    idx_sheet = indices_for(COL_SPREADSHEET)
    idx_wants = [i for i, h in enumerate(header) if h.startswith(WANTS_CODE_PREFIX)]

    if not idx_sheet:
        logger.warning(
            "[codes] %s: no %r column -- capturing without it; the form may have "
            "been restructured", RESPONSES_TAB, COL_SPREADSHEET,
        )

    records = []
    for offset, row in enumerate(rows[1:]):
        org = _first_non_empty(row, idx_org)
        code = _first_non_empty(row, idx_code)
        link = _first_non_empty(row, idx_link)
        # A submission with no org AND no code is a blank trailing row.
        if not org and not code and not link:
            continue
        records.append({
            "as_of_date": as_of,
            "source_tab": RESPONSES_TAB,
            "sheet_row": offset + 2,
            "org_label": org or None,
            "source_code_raw": code or None,
            "source_url": link or None,
            "spreadsheet_link": _first_non_empty(row, idx_sheet) or None,
            "wants_code": _first_non_empty(row, idx_wants) or None,
            "submitted_at": _first_non_empty(row, idx_ts) or None,
            "synced_at": synced_at,
        })
    return records


def parse_summary(
    rows: List[List[str]], as_of: datetime.date, synced_at: datetime.datetime
) -> List[Dict[str, Any]]:
    """One record per row of the hand-curated Source Codes tab.

    Positional rather than header-matched: the tab's header row is effectively
    unlabelled (column A has no header at all), so there is nothing to match on.
    A layout change here shows up as org_label holding a URL, which the cleaned
    view surfaces rather than hiding.
    """
    records = []
    for offset, row in enumerate(rows[SUMMARY_FIRST_DATA_ROW - 1:]):
        org = _cell(row, SUMMARY_COL_ORG).strip()
        url = _cell(row, SUMMARY_COL_URL).strip()
        if not org and not url:
            continue
        records.append({
            "as_of_date": as_of,
            "source_tab": SUMMARY_TAB,
            "sheet_row": SUMMARY_FIRST_DATA_ROW + offset,
            "org_label": org or None,
            "source_code_raw": None,  # this tab carries only the link
            "source_url": url or None,
            "spreadsheet_link": _cell(row, SUMMARY_COL_SHEET).strip() or None,
            "wants_code": None,
            "submitted_at": None,
            "synced_at": synced_at,
        })
    return records


def read_sheet(as_of: datetime.date, synced_at: datetime.datetime) -> List[Dict[str, Any]]:
    """Scrape both tabs. Raises if either is missing or renamed."""
    with SheetsConnector() as sheets:
        spreadsheet = sheets.get_spreadsheet(SPREADSHEET_ID)
        available = {ws.title for ws in spreadsheet.worksheets()}
        missing = {RESPONSES_TAB, SUMMARY_TAB} - available
        if missing:
            raise ValueError(
                f"Partner engagement tab(s) not found: {sorted(missing)}. "
                f"Worksheets present: {sorted(available)}"
            )
        records = parse_responses(
            spreadsheet.worksheet(RESPONSES_TAB).get_all_values(), as_of, synced_at
        )
        records += parse_summary(
            spreadsheet.worksheet(SUMMARY_TAB).get_all_values(), as_of, synced_at
        )

    for tab in (RESPONSES_TAB, SUMMARY_TAB):
        rows = [r for r in records if r["source_tab"] == tab]
        logger.info(
            "[codes] %s: %d rows, %d with a code or link, %d with a spreadsheet",
            tab, len(rows),
            sum(1 for r in rows if r["source_code_raw"] or r["source_url"]),
            sum(1 for r in rows if r["spreadsheet_link"]),
        )
    return records


def load(records: List[Dict[str, Any]], as_of: datetime.date) -> None:
    """Replace the day's partition rows with `records`.

    pandas is imported HERE, not at module scope: run_misc_jobs.py imports every
    registered task module at startup, so a module-level import of a dependency
    the container might not have would take down every other task too (that is
    exactly how the 2026-07-30..08-17 outage worked -- see
    civis/SCHEDULED_SCRIPTS.md).
    """
    import pandas as pd

    df = pd.DataFrame.from_records(records)
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date

    with BigQueryConnector(project_id=BQ_PROJECT) as bq:
        with open(_ddl_path(DDL_FILE), "r", encoding="utf-8") as fh:
            bq.query(fh.read())  # CREATE TABLE IF NOT EXISTS -- idempotent

        deleted = bq.execute_dml(
            f"DELETE FROM `{TARGET_TABLE}` "
            f"WHERE as_of_date = DATE '{as_of.isoformat()}'"
        )
        if deleted:
            logger.info(
                "[codes] replaced %s row(s) already snapshotted for %s",
                deleted, as_of.isoformat(),
            )
        bq.load_dataframe(df, TARGET_TABLE, if_exists="append")
    logger.info(
        "[codes] loaded %d rows into %s for %s",
        len(df), TARGET_TABLE, as_of.isoformat(),
    )


def _report_dry_run(records: List[Dict[str, Any]]) -> None:
    """Print what WOULD be written: counts only, never org or code values."""
    from collections import Counter

    logger.info("[codes] DRY RUN -- %d rows, nothing written", len(records))
    per_tab = Counter(r["source_tab"] for r in records)
    for tab in sorted(per_tab):
        rows = [r for r in records if r["source_tab"] == tab]
        logger.info(
            "[codes]   %s: %d rows | %d with a link | %d with a spreadsheet "
            "| %d naming an org with no link",
            tab, len(rows),
            sum(1 for r in rows if r["source_url"]),
            sum(1 for r in rows if r["spreadsheet_link"]),
            sum(1 for r in rows if r["org_label"] and not r["source_url"]),
        )


def run(dry_run: bool = False) -> None:
    """Snapshot both partner-engagement tabs. The scheduled path calls run()
    with no args; dry_run=True is the ops/testing path (scrape and report,
    write nothing) reached via
    `python misc_jobs/partner_source_codes.py --dry-run`."""
    as_of = datetime.datetime.now(SNAPSHOT_TZ).date()
    synced_at = datetime.datetime.now(datetime.timezone.utc)

    records = read_sheet(as_of, synced_at)
    if not records:
        raise RuntimeError(
            "no rows scraped from either partner-engagement tab -- refusing to "
            "write an empty snapshot"
        )

    if dry_run:
        _report_dry_run(records)
        return

    load(records, as_of)


if __name__ == "__main__":
    # Standalone dev entrypoint; the scheduled path is run_misc_jobs.py.
    import argparse
    import logging as _logging

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape both tabs and report counts; write nothing to BigQuery.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    run(dry_run=args.dry_run)

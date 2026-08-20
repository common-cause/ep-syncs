"""
50-State EP Coalition Plan "Infrastructure" tabs -> ep_2026_raw (BigQuery).

The two Infrastructure tabs (General / Primary) are the EP program team's live
tracker for per-state setup: training + quiz links, reporting forms, PTV role
hygiene, polling-place selection, post-election collection. They are
hand-maintained by program staff, so cells are prose as often as data
("YES - reset ... (470 -> 1). NOTE: ...", "N/A (checklist only)", multi-line
"Label: https://..." lists, or nothing at all).

This module is deliberately dumb: it MELTS each tab to long format (one row per
state x sheet column) and replaces the day's partition in
`ep_2026_raw.coalition_plan_infrastructure`. Every bit of interpretation --
verdict tokens, link extraction, notes -- lives downstream in
`ep_2026_cleaned.coalition_plan_infrastructure`, so a parse fix is a view
change and never needs a re-scrape.

Long format is the load-bearing choice: the program team adds and renames
columns without telling anyone, and a melt turns a schema change into new rows
instead of a broken load. Do not pivot it, do not filter blanks, do not
interpret cells.

Provenance: ported 2026-08-19 from ep-dashboards
`scripts/sync_infrastructure_sheet.py` (which landed the same melt in
`ep_dashboards.infrastructure_raw` from a laptop Task Scheduler job). Deterministic
pipelines belong on Civis and raw capture belongs to ep-syncs, so the ingest
moved here and ep-dashboards models it. Two deliberate changes on the way in:
`as_of_date` is stamped in US/Eastern (was the container's local date) to match
the runner's weekday convention, and `run()` replaced the argparse/main wrapper.

Tab layout (both tabs, verified 2026-08-19):
    row 1  vestigial super-group ("Primary" / "General" -- left over from when
           one tab held both; ignored)
    row 2  column group ("Training", "Reporting", "PTV Setup",
           "Polling Places", "Reporting") -- sparse, forward-filled
    row 3  column names ("State", "Abbrev", "State Lead", "Training Link", ...)
    row 4+ one row per state (50 + DC, alphabetical by full name, DC between
           Connecticut and Delaware)

Failure modes it treats as loud, on purpose:
  - a missing/renamed tab raises, listing the worksheets it did find. Half a
    snapshot is worse than none.
  - a row-3 layout change (column A no longer "State") raises.
  - an empty melt refuses to write.
Row-3 headers are logged every run, so a column rename shows up in the Civis
log the morning it happens rather than as a silent gap in the marts.

READ-ONLY toward the spreadsheet. Program staff own it; nothing here writes.

Idempotency: DELETE the day's partition rows, then append via a LOAD JOB (not a
streaming insert), so there is no streaming buffer to block a same-day rerun --
a Civis retry is harmless.

PII: the melted cells include column C, "State Lead", which is a person's name.
That lands in BigQuery (access-controlled) and must not leave it -- the dry-run
report is deliberately counts-only.

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

SPREADSHEET_ID = "1YdFVBUcqMF5d4Hniy8CKbxEQ_gJG13Xe7Gkb8HXnasY"

# Tab -> phase key. The phase is the election the tab tracks; `general` is the
# one the dashboards lead with (Rob, 2026-08-19).
TABS = {
    "Infrastructure (General)": "general",
    "Infrastructure (Primary)": "primary",
}

BQ_PROJECT = "proj-tmc-mem-com"
DATASET = "ep_2026_raw"
TARGET_TABLE = f"{BQ_PROJECT}.{DATASET}.coalition_plan_infrastructure"
DDL_FILE = "coalition_plan_infrastructure.sql"

# The nightly runner fires at ~3 AM ET and resolves "tonight" in this zone; the
# snapshot date has to agree with it, or a schedule shift (or DST) silently
# labels a snapshot with the wrong day.
SNAPSHOT_TZ = ZoneInfo("America/New_York")

# Header geometry (1-based rows as seen in the sheet UI).
ROW_GROUP = 2       # column-group labels
ROW_HEADER = 3      # column names
FIRST_DATA_ROW = 4

# Columns A and B key each row; everything from C on is melted.
COL_STATE = 0
COL_ABBREV = 1
FIRST_MELT_COL = 2

# 50 states + DC. Fewer means the program team deleted rows (or the tab changed
# shape) -- worth a warning, not a failure.
EXPECTED_STATES = 51


def _ddl_path(filename: str) -> Path:
    return Path(__file__).resolve().parent.parent / "bq" / filename


def forward_fill(labels: List[str]) -> List[str]:
    """Carry each group label rightward until the next non-empty one."""
    out, current = [], ""
    for label in labels:
        label = (label or "").strip()
        if label:
            current = label
        out.append(current)
    return out


def melt_tab(
    rows: List[List[str]],
    tab: str,
    phase: str,
    as_of: datetime.date,
    synced_at: datetime.datetime,
) -> List[Dict[str, Any]]:
    """Turn one worksheet's grid into long-format records."""
    if len(rows) < FIRST_DATA_ROW:
        raise ValueError(f"{tab!r}: only {len(rows)} rows -- header layout changed?")

    width = max(len(r) for r in rows)

    def cell(row: List[str], idx: int) -> str:
        return row[idx] if idx < len(row) else ""

    header = [cell(rows[ROW_HEADER - 1], i).strip() for i in range(width)]
    groups = forward_fill([cell(rows[ROW_GROUP - 1], i) for i in range(width)])

    if header[COL_STATE].lower() != "state":
        raise ValueError(
            f"{tab!r}: expected 'State' in row {ROW_HEADER} column A, "
            f"got {header[COL_STATE]!r} -- header layout changed"
        )

    # Cheap drift evidence in the nightly log: a renamed or added tracker column
    # is a new col_name downstream, which otherwise only shows up as a gap.
    logger.info(
        "[infra] %s: row-%d headers = %s",
        tab, ROW_HEADER, [h for h in header[FIRST_MELT_COL:] if h],
    )

    records = []
    for offset, row in enumerate(rows[FIRST_DATA_ROW - 1:]):
        state_name = cell(row, COL_STATE).strip()
        if not state_name:
            continue  # trailing blank rows
        for idx in range(FIRST_MELT_COL, width):
            if not header[idx]:
                continue  # unlabelled scratch column
            value = cell(row, idx).strip()
            records.append({
                "as_of_date": as_of,
                "phase": phase,
                "sheet_tab": tab,
                "sheet_row": FIRST_DATA_ROW + offset,
                "state_name_raw": state_name,
                "state_abbrev_raw": cell(row, COL_ABBREV).strip() or None,
                "col_index": idx,
                # Column C ("State Lead") sits left of the first group label.
                "col_group": groups[idx] or None,
                "col_name": header[idx],
                "cell_value": value or None,
                "synced_at": synced_at,
            })
    return records


def read_sheet(as_of: datetime.date, synced_at: datetime.datetime) -> List[Dict[str, Any]]:
    """Melt both Infrastructure tabs. Raises if a tab is missing or renamed."""
    records: List[Dict[str, Any]] = []
    with SheetsConnector() as sheets:
        spreadsheet = sheets.get_spreadsheet(SPREADSHEET_ID)
        available = {ws.title for ws in spreadsheet.worksheets()}
        missing = set(TABS) - available
        if missing:
            raise ValueError(
                f"Infrastructure tab(s) not found: {sorted(missing)}. "
                f"Worksheets present: {sorted(available)}"
            )
        for tab, phase in TABS.items():
            rows = spreadsheet.worksheet(tab).get_all_values()
            tab_records = melt_tab(rows, tab, phase, as_of, synced_at)
            states = {r["state_name_raw"] for r in tab_records}
            logger.info(
                "[infra] %s -> phase=%s: %d states x %d columns = %d cells "
                "(%d non-blank)",
                tab, phase, len(states),
                len({r["col_name"] for r in tab_records}),
                len(tab_records),
                sum(1 for r in tab_records if r["cell_value"]),
            )
            if len(states) != EXPECTED_STATES:
                logger.warning(
                    "[infra] %s: %d state rows, expected %d -- rows added or "
                    "removed on the tab; check before trusting per-state marts",
                    tab, len(states), EXPECTED_STATES,
                )
            records.extend(tab_records)
    return records


def load(records: List[Dict[str, Any]], as_of: datetime.date) -> None:
    """Replace the day's partition rows with `records`.

    pandas is imported HERE, not at module scope: run_misc_jobs.py imports every
    registered task module at startup, so a module-level import of a dependency
    the container might not have would take down every other task too (that is
    exactly how the 2026-07-30..08-17 outage worked -- see
    civis/SCHEDULED_SCRIPTS.md). A load job also needs no streaming buffer, so
    the pre-delete above always succeeds.
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
                "[infra] replaced %s row(s) already snapshotted for %s",
                deleted, as_of.isoformat(),
            )
        bq.load_dataframe(df, TARGET_TABLE, if_exists="append")
    logger.info(
        "[infra] loaded %d rows into %s for %s",
        len(df), TARGET_TABLE, as_of.isoformat(),
    )


def _report_dry_run(records: List[Dict[str, Any]]) -> None:
    """Print what WOULD be written: counts only.

    Deliberately no cell text -- column C is 'State Lead' (a person's name), so
    a value-level dump would put PII in a terminal, a ticket or a spec doc.
    """
    from collections import Counter

    logger.info("[infra] DRY RUN -- %d rows, nothing written", len(records))
    per_phase = Counter(r["phase"] for r in records)
    logger.info("[infra]   rows by phase: %s", dict(per_phase))
    for phase in sorted(per_phase):
        filled = Counter(
            r["col_name"] for r in records
            if r["phase"] == phase and r["cell_value"]
        )
        logger.info("[infra]   %s non-blank cells by column: %s", phase, dict(filled))


def run(dry_run: bool = False) -> None:
    """Snapshot both Infrastructure tabs. The scheduled path calls run() with no
    args; dry_run=True is the ops/testing path (melt and report, write nothing)
    reached via `python misc_jobs/infrastructure_sheet.py --dry-run`."""
    as_of = datetime.datetime.now(SNAPSHOT_TZ).date()
    synced_at = datetime.datetime.now(datetime.timezone.utc)

    records = read_sheet(as_of, synced_at)
    if not records:
        raise RuntimeError(
            "no records melted from either Infrastructure tab -- refusing to "
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
        help="Read and melt both tabs and report per-column fill counts; "
             "write nothing to BigQuery.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    run(dry_run=args.dry_run)

"""Airtable base visibility sweep -> ep.airtable_base_visibility (BigQuery).

Records every base the sync-operations PAT can currently see, so that a base
APPEARING is a detectable event rather than a thing someone happens to notice.

Why it exists, in one paragraph: registration at go-live is handled --
ep-airtable-utilities writes ep.airtable_sync_sources from its specs and audits
that every spec has a row. But that audit walks SPECS -> REGISTRY and cannot
see a base with no spec, which is most of them: hand-cloned quiz bases,
prior-cycle bases, and bases a staffer spun up off-process. A one-off sweep on
2026-09-02 found 40 unregistered quiz bases, several collecting live. Worse,
PAT access is GRANTED by people who have no idea BigQuery capture is a separate
step and will never mention it, so a grant is a silent event. Amy's FL base
became readable in the week of 2026-09-01 and nothing noticed.

WHAT THIS IS NOT: discovery. `list_bases()` returns what the PAT has been
granted; a base nobody has shared with us is invisible here however often this
runs. Finding those is the human canvass ("what are you using?"), which is
being run much harder for the general than the primary. This sweep's job is to
make sure that when a grant lands, it does not sit unnoticed -- the canvass
finds the bases, this catches them.

The judgment half is NOT here. Deciding whether a newly-visible base is EP work
worth capturing is a rubric call, and it lives in the airtable-base-triage
dispatch task type (.claude/dispatch.yaml, runbook
.claude/skills/airtable-base-triage/SKILL.md). This module only observes and
records; it never registers anything and never mutes anything.

READ-ONLY toward Airtable -- one metadata call, no record reads.

PII: base NAMES only, which are program/org labels, not people-data. No records
are read, so no volunteer data passes through here. The dry-run report prints
base names and counts.

Idempotency: a MERGE on base_id, so a same-day rerun re-stamps last_seen_date
to the same value and changes nothing else. DML, not a streaming insert, so
there is no streaming buffer to block a rerun. The MERGE deliberately touches
ONLY observation columns -- triage_status/notes/by/at are written by the triage
pass and must survive every sweep, or a nightly run would silently un-mute
every base someone already ruled on.

Credentials come from the environment (BIGQUERY_CREDENTIALS_PASSWORD,
AIRTABLE_API_KEY_PASSWORD); run_misc_jobs.py loads .env before calling run().
NOTE: the Civis misc-jobs job needs the AIRTABLE_API_KEY credential attached
and the `airtable` extra installed -- see civis/run_misc_jobs.sh.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BQ_PROJECT = "proj-tmc-mem-com"
TARGET_TABLE = f"{BQ_PROJECT}.ep.airtable_base_visibility"
QUEUE_VIEW = f"{BQ_PROJECT}.ep.v_airtable_base_triage_queue"
DDL_FILE = "airtable_base_visibility.sql"

# The sweep belongs to the ET calendar day the nightly job fires on, matching
# every other task in this runner.
SWEEP_TZ = ZoneInfo("America/New_York")


def _ddl_path(filename: str) -> Path:
    return Path(__file__).resolve().parent.parent / "bq" / filename


def fetch_visible_bases() -> List[Dict[str, Any]]:
    """Every base the sync-operations PAT can see, via the Airtable meta API.

    AirtableConnector is imported HERE, not at module scope: run_misc_jobs.py
    imports every registered task module at startup, so a module-level import
    of a dependency the container might lack would take down every other task
    too. That is exactly how the 2026-07-30..08-17 outage worked -- see
    civis/SCHEDULED_SCRIPTS.md.
    """
    from ccef_connections.connectors.airtable import AirtableConnector

    with AirtableConnector() as conn:
        bases = conn.list_bases()

    if not bases:
        raise RuntimeError(
            "list_bases() returned nothing. The PAT has never legitimately seen "
            "zero bases, so this is a credential or scope failure, not an empty "
            "result -- refusing to write a sweep that would mark every known "
            "base as access_lost."
        )
    return bases


def load(bases: List[Dict[str, Any]], sweep_date: datetime.date) -> None:
    """MERGE this sweep's sighting into the ledger.

    Observation columns only. A base already present keeps its triage verdict.
    """
    from ccef_connections.connectors.bigquery import BigQueryConnector
    from google.cloud import bigquery as gbq

    rows = [
        {
            "base_id": b["id"],
            "base_name": b.get("name"),
            "permission_level": b.get("permission_level"),
        }
        for b in bases
        if b.get("id")
    ]

    sql = f"""
    MERGE `{TARGET_TABLE}` T
    USING (
      SELECT * FROM UNNEST(@rows)
    ) S
    ON T.base_id = S.base_id
    WHEN MATCHED THEN UPDATE SET
      base_name        = S.base_name,
      permission_level = S.permission_level,
      last_seen_date   = @sweep_date,
      updated_at       = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
      (base_id, base_name, permission_level, first_seen_date, last_seen_date,
       triage_status, triage_notes, triaged_by, triaged_at,
       created_at, updated_at)
    VALUES
      (S.base_id, S.base_name, S.permission_level, @sweep_date, @sweep_date,
       'pending', NULL, NULL, NULL,
       CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
    """

    struct_type = gbq.StructQueryParameterType(
        gbq.ScalarQueryParameterType("STRING", name="base_id"),
        gbq.ScalarQueryParameterType("STRING", name="base_name"),
        gbq.ScalarQueryParameterType("STRING", name="permission_level"),
    )
    params = [
        gbq.ArrayQueryParameter(
            "rows",
            struct_type,
            [
                gbq.StructQueryParameter(
                    None,
                    gbq.ScalarQueryParameter("base_id", "STRING", r["base_id"]),
                    gbq.ScalarQueryParameter("base_name", "STRING", r["base_name"]),
                    gbq.ScalarQueryParameter(
                        "permission_level", "STRING", r["permission_level"]
                    ),
                )
                for r in rows
            ],
        ),
        gbq.ScalarQueryParameter("sweep_date", "DATE", sweep_date),
    ]

    with BigQueryConnector(project_id=BQ_PROJECT) as bq:
        with open(_ddl_path(DDL_FILE), "r", encoding="utf-8") as fh:
            ddl = fh.read()
        # The file also carries the CREATE OR REPLACE VIEW for the queue. The
        # CREATE TABLE is not IF NOT EXISTS (it is a hand-applied registry-style
        # DDL), so self-healing here would mask a missing table rather than fix
        # it -- deliberately not attempted. Both objects are applied by hand
        # once; see the file header.
        del ddl

        bq.query(sql, params=params)
        _report_after_load(bq, sweep_date)


def _report_after_load(bq, sweep_date: datetime.date) -> None:
    """Log what this sweep changed, from the ledger itself rather than from the
    in-memory list -- the numbers that matter are the ones a reader can requery.
    """
    summary = list(bq.query(f"""
        SELECT
          COUNTIF(last_seen_date = @d)                              AS visible_now,
          COUNTIF(first_seen_date = @d)                             AS first_seen_today,
          COUNTIF(last_seen_date < @d)                              AS not_visible_now,
          COUNT(*)                                                  AS known_ever
        FROM `{TARGET_TABLE}`
    """, params=_date_param(sweep_date)))[0]

    logger.info(
        "[bases] sweep %s: %d visible, %d NEW this sweep, %d no longer visible "
        "(%d known ever)",
        sweep_date.isoformat(), summary["visible_now"],
        summary["first_seen_today"], summary["not_visible_now"],
        summary["known_ever"],
    )

    queue = list(bq.query(
        f"SELECT reason, COUNT(*) n FROM `{QUEUE_VIEW}` GROUP BY reason ORDER BY reason"
    ))
    if not queue:
        logger.info("[bases] triage queue empty -- nothing awaiting a decision")
        return
    for r in queue:
        level = logger.warning if r["reason"] == "access_lost" else logger.info
        level("[bases] triage queue: %s = %d", r["reason"], r["n"])


def _date_param(d: datetime.date):
    from google.cloud import bigquery as gbq
    return [gbq.ScalarQueryParameter("d", "DATE", d)]


def _report_dry_run(bases: List[Dict[str, Any]]) -> None:
    levels: Dict[str, int] = {}
    for b in bases:
        key = str(b.get("permission_level"))
        levels[key] = levels.get(key, 0) + 1
    logger.info("[bases] DRY RUN -- %d bases visible to the PAT", len(bases))
    logger.info("[bases] permission_level distribution: %s", levels)
    logger.info("[bases] writing nothing; run without --dry-run to record the sweep")


def run(dry_run: bool = False) -> None:
    """Record tonight's PAT-visible base list.

    The scheduled path calls run() with no args; dry_run=True is the
    ops/testing path reached via
    `python misc_jobs/airtable_base_visibility.py --dry-run`.
    """
    sweep_date = datetime.datetime.now(SWEEP_TZ).date()
    bases = fetch_visible_bases()

    if dry_run:
        _report_dry_run(bases)
        return

    load(bases, sweep_date)


if __name__ == "__main__":
    # Standalone dev entrypoint; the scheduled path is run_misc_jobs.py.
    import argparse
    import logging as _logging

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List visible bases and report counts; write nothing to BigQuery.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    run(dry_run=args.dry_run)

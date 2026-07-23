"""
Apply / drift-check the committed ep_2026_cleaned SQL (views + UDFs).

The interface layer's DDL lives in bq/ep_2026_cleaned/*.sql, applied in
filename order (numeric prefixes encode dependency order: 00 functions ->
1x static views [10_shift_signups before 11_volunteers, which reads it] ->
2x/3x Airtable views -> 90 sync_health). Each file runs
as a single BigQuery multi-statement script, so files are idempotent
CREATE OR REPLACE statements.

File conventions (relied on by --check):
  - One view per file (00_functions.sql, holding UDFs, is the exception).
  - The view body's opening `AS` sits ALONE on its own line; everything
    after that line is the body compared against INFORMATION_SCHEMA.VIEWS.

Usage:
    python apply_bq_views.py                        # apply all, in order
    python apply_bq_views.py --only 11_volunteers.sql
    python apply_bq_views.py --check                # committed vs deployed
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from ccef_connections import BigQueryConnector

PROJECT = "proj-tmc-mem-com"
DATASET = "ep_2026_cleaned"
SQL_DIR = Path(__file__).parent / "bq" / DATASET

VIEW_NAME_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`" + re.escape(f"{PROJECT}.{DATASET}.")
    + r"(\w+)`",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def iter_sql_files(only: Optional[str] = None) -> List[Path]:
    files = sorted(SQL_DIR.glob("*.sql"))
    if only:
        files = [f for f in files if f.name == only]
        if not files:
            raise FileNotFoundError(
                f"{only} not found in {SQL_DIR} "
                f"(have: {[f.name for f in sorted(SQL_DIR.glob('*.sql'))]})"
            )
    return files


def extract_view_name(sql_text: str) -> Optional[str]:
    m = VIEW_NAME_RE.search(sql_text)
    return m.group(1) if m else None


def extract_view_body(sql_text: str) -> Optional[str]:
    """Return everything after the standalone `AS` line, or None."""
    lines = sql_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "AS":
            return "\n".join(lines[i + 1:])
    return None


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()


def apply_files(bq: BigQueryConnector, files: List[Path]) -> int:
    failed: List[str] = []
    for f in files:
        sql = f.read_text(encoding="utf-8")
        try:
            bq.query(sql)
            logger.info(f"[apply] {f.name}: OK")
        except Exception as e:
            logger.exception(f"[apply] {f.name}: FAILED -- {e}")
            failed.append(f.name)
    if failed:
        logger.error(f"[apply] failed files: {failed}")
        return 1
    return 0


def check_drift(bq: BigQueryConnector, files: List[Path]) -> int:
    deployed: Dict[str, str] = {}
    sql = (
        f"SELECT table_name, view_definition "
        f"FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.VIEWS`"
    )
    for row in bq.query(sql):
        deployed[row["table_name"]] = row["view_definition"]

    drift: List[str] = []
    checked = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        view = extract_view_name(text)
        if view is None:
            logger.info(f"[check] {f.name}: no view statement (skipped)")
            continue
        body = extract_view_body(text)
        if body is None:
            logger.error(
                f"[check] {f.name}: no standalone `AS` line -- can't extract body"
            )
            drift.append(f.name)
            continue
        checked += 1
        if view not in deployed:
            logger.error(f"[check] {f.name}: view `{view}` NOT DEPLOYED")
            drift.append(f.name)
        elif _normalize(deployed[view]) != _normalize(body):
            logger.error(f"[check] {f.name}: view `{view}` DRIFTED from committed SQL")
            drift.append(f.name)
        else:
            logger.info(f"[check] {f.name}: `{view}` matches deployed")

    logger.info(f"[check] {checked} views checked, {len(drift)} problems")
    return 1 if drift else 0


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Apply/check bq/{DATASET}/*.sql")
    p.add_argument("--only", help="Apply/check a single file, e.g. 11_volunteers.sql")
    p.add_argument(
        "--check",
        action="store_true",
        help="Compare committed view SQL against deployed definitions; "
             "exit non-zero on drift. Applies nothing.",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    args = _parse_args(argv)
    files = iter_sql_files(args.only)
    with BigQueryConnector() as bq:
        if args.check:
            return check_drift(bq, files)
        return apply_files(bq, files)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

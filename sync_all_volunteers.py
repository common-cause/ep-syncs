"""
Sync PTV registered volunteers (users_csv) -> BigQuery raw.

Pulls all registered volunteers per state from PTV's users_csv endpoint and
appends today's snapshot to ptv_raw_2026.users (partitioned by as_of_date).

BQ-only phase: there is no Airtable leg yet. When one is added, mirror the
Stage-3 pattern in sync_shift_volunteers.py. See
docs/all_volunteers_sync_spec.md for the full design.

Per-state failures are isolated. Exit code is non-zero if any attempted state
failed to land in BigQuery.

Usage:
    python sync_all_volunteers.py                 # all states in PULL_STATES
    python sync_all_volunteers.py --states NE,PA  # override (ops / testing)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from dotenv import load_dotenv

from ccef_connections import BigQueryConnector, PTVConnector


# -- Constants --------------------------------------------------------------

PROJECT = "proj-tmc-mem-com"
RAW_TABLE = f"{PROJECT}.ptv_raw_2026.users"

# All 50 states + DC. users_csv returns [] for states with no data (the
# connector maps PTV's "Not Found" sentinel to an empty list), so pulling the
# full set is zero-config: empty states write no rows and new program states
# light up automatically. Scope at query time against the raw table if needed.
PULL_STATES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]

# users_csv columns we persist (whitelist -> guards against a stray upstream
# column breaking the streaming insert). Everything except `id` lands as-is;
# `id` is coerced to INT. `join_date` stays STRING in raw and is cast in the
# v_users_current view.
USERS_CSV_COLS: Tuple[str, ...] = (
    "id", "email", "join_date", "phone_number", "first_name", "last_name",
    "county", "zip_code", "source_code", "regional_admin", "shifted",
    "training", "role",
)

# insert_rows_json sends one HTTP request with no chunking; users_csv can
# return tens of thousands of rows per run, so we batch to stay under the
# streaming-insert payload limit.
INSERT_CHUNK = 500

logger = logging.getLogger(__name__)


# -- Stage 1: PTV -> memory -------------------------------------------------


def _coerce_row(row: Dict[str, Any], state: str, as_of_date) -> Dict[str, Any]:
    """Convert a PTV users_csv row to a BQ-streamable dict (whitelisted cols)."""
    out: Dict[str, Any] = {c: row.get(c) for c in USERS_CSV_COLS}

    # id -> INT64 (blank -> None)
    v = out.get("id")
    if isinstance(v, str) and v.strip() == "":
        out["id"] = None
    elif v is not None and not isinstance(v, int):
        try:
            out["id"] = int(v)
        except (ValueError, TypeError):
            out["id"] = None

    out["state"] = state
    out["as_of_date"] = as_of_date.isoformat()
    return out


def fetch_ptv_for_states(
    ptv: PTVConnector, states: List[str], as_of_date,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """
    Pull users_csv from PTV per state. Returns (rows_by_state, failed_states).
    A state that returns no data is a success with an empty row list.
    """
    rows_by_state: Dict[str, List[Dict[str, Any]]] = {}
    failed: List[str] = []
    logged_keys = False
    for state in states:
        try:
            rows = ptv.get_users(state)
            # Log the actual CSV columns once so a header change (which would
            # otherwise silently null our whitelisted columns) is visible.
            if rows and not logged_keys:
                logger.info(f"[PTV] users_csv columns: {sorted(rows[0].keys())}")
                logged_keys = True
            rows_by_state[state] = [
                _coerce_row(r, state, as_of_date) for r in rows
            ]
            logger.info(f"[PTV] {state}: pulled {len(rows)} volunteers")
        except Exception as e:
            logger.exception(f"[PTV] {state}: pull failed -- {e}")
            failed.append(state)
    return rows_by_state, failed


# -- Stage 2: memory -> BQ --------------------------------------------------


def _chunks(seq: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def write_to_bq(
    bq: BigQueryConnector,
    rows_by_state: Dict[str, List[Dict[str, Any]]],
    as_of_date,
) -> List[str]:
    """
    Replace today's partition rows for the pulled states (idempotency on
    rerun), then append fresh, per state. Returns states that landed cleanly.
    """
    states = list(rows_by_state.keys())
    if not states:
        return []

    # Single pre-delete for all pulled states. Only touches states we're about
    # to rewrite, so a PTV-failed state's prior snapshot is left intact.
    state_list_sql = ", ".join(f"'{s}'" for s in states)
    delete_sql = (
        f"DELETE FROM `{RAW_TABLE}` "
        f"WHERE as_of_date = DATE '{as_of_date.isoformat()}' "
        f"AND state IN ({state_list_sql})"
    )
    try:
        bq.execute_dml(delete_sql)
    except Exception as e:
        # Streaming buffer can block DML on rows streamed in the last ~90 min.
        # v_users_current does SELECT DISTINCT before aggregating, so an
        # exact-duplicate same-day snapshot collapses harmlessly.
        logger.warning(f"[BQ] pre-delete failed (continuing): {e}")

    successful: List[str] = []
    for state, rows in rows_by_state.items():
        try:
            n = 0
            for chunk in _chunks(rows, INSERT_CHUNK):
                bq.insert_rows(RAW_TABLE, chunk)
                n += len(chunk)
            logger.info(f"[BQ] {state}: inserted {n} rows")
            successful.append(state)
        except Exception as e:
            logger.exception(f"[BQ] {state}: insert failed -- {e}")
    return successful


# -- Main -------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync PTV users_csv -> BigQuery.")
    p.add_argument(
        "--states",
        help="Comma-separated state codes to pull instead of the full set "
             "(ops / testing). e.g. --states NE,PA",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()

    args = _parse_args(argv)
    states = (
        [s.strip().upper() for s in args.states.split(",") if s.strip()]
        if args.states
        else list(PULL_STATES)
    )

    as_of_date = datetime.now(timezone.utc).date()
    logger.info(
        f"=== All-volunteers sync -- as_of_date={as_of_date} "
        f"states={len(states)} ==="
    )

    failed_states: List[str] = []

    with PTVConnector() as ptv, BigQueryConnector() as bq:
        rows_by_state, ptv_failed = fetch_ptv_for_states(ptv, states, as_of_date)
        failed_states.extend(ptv_failed)

        bq_successful = write_to_bq(bq, rows_by_state, as_of_date)
        for state in rows_by_state:
            if state not in bq_successful:
                failed_states.append(state)

        total_rows = sum(len(r) for r in rows_by_state.values())
        logger.info(
            f"[summary] states_ok={len(bq_successful)} "
            f"rows_written={total_rows} failed_states={failed_states}"
        )

    logger.info(f"=== Done. failed_states={failed_states} ===")
    return 1 if failed_states else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

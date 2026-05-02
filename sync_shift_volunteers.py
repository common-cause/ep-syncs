"""
Sync PTV shift volunteers -> BigQuery raw -> Airtable.

For each state listed in config/syncs.yaml:
  1. Pull volunteer signups from PTV's shift_volunteers_csv endpoint
  2. Append today's snapshot to ptv_raw_2026.shift_volunteers (partitioned by as_of_date)
  3. For each Airtable target in the YAML, query the per-volunteer view filtered
     to that state and upsert into the target base/table on email

Per-state and per-sync failures are isolated. Exit code is non-zero if any
state failed to sync to BQ or any Airtable target failed to upsert.
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from dotenv import load_dotenv

from ccef_connections import (
    AirtableConnector,
    BigQueryConnector,
    PTVConnector,
)


# -- Constants --------------------------------------------------------------

PROJECT = "proj-tmc-mem-com"
RAW_TABLE = f"{PROJECT}.ptv_raw_2026.shift_volunteers"
CURRENT_VIEW = f"{PROJECT}.ptv_raw_2026.v_shift_volunteers_current"

CONFIG_PATH = Path(__file__).parent / "config" / "syncs.yaml"

# PTV row fields that need empty-string -> None coercion for BQ DATE/TIME/INT cols
NULLABLE_EMPTY_FIELDS = ("shift_id", "date", "start_time", "end_time")
INT_FIELDS = ("shift_id",)

logger = logging.getLogger(__name__)


# -- Config -----------------------------------------------------------------


@dataclass
class SyncEntry:
    name: str
    state: str
    base_id: str
    table: str
    field_map: Dict[str, str]  # bq_col -> airtable_col


@dataclass
class Config:
    syncs: List[SyncEntry]
    upsert_key_bq: str

    @property
    def unique_states(self) -> List[str]:
        return sorted({s.state for s in self.syncs})


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    default_field_map = raw.get("default_field_map") or {}
    upsert_key_bq = raw.get("upsert_key_bq", "email")

    syncs: List[SyncEntry] = []
    for entry in raw.get("syncs") or []:
        merged = {**default_field_map, **(entry.get("field_map") or {})}
        if upsert_key_bq not in merged:
            raise ValueError(
                f"Sync '{entry['name']}': field_map missing upsert key "
                f"'{upsert_key_bq}'"
            )
        syncs.append(SyncEntry(
            name=entry["name"],
            state=entry["state"],
            base_id=entry["base_id"],
            table=entry["table"],
            field_map=merged,
        ))

    if not syncs:
        raise ValueError(
            f"No active syncs in {path}. Uncomment / add entries under 'syncs:'."
        )

    return Config(syncs=syncs, upsert_key_bq=upsert_key_bq)


# -- Stage 1: PTV -> memory -------------------------------------------------


def _coerce_row(row: Dict[str, Any], state: str, as_of_date) -> Dict[str, Any]:
    """Convert a PTV CSV row to a BQ-streamable dict."""
    out = dict(row)
    for f in NULLABLE_EMPTY_FIELDS:
        v = out.get(f, "")
        if isinstance(v, str) and v.strip() == "":
            out[f] = None
    for f in INT_FIELDS:
        v = out.get(f)
        if v is not None and not isinstance(v, int):
            try:
                out[f] = int(v)
            except (ValueError, TypeError):
                out[f] = None
    out["state"] = state
    out["as_of_date"] = as_of_date.isoformat()
    return out


def fetch_ptv_for_states(
    ptv: PTVConnector, states: List[str], as_of_date,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """
    Pull shift_volunteers from PTV per state. Returns (rows_by_state, failed_states).
    """
    rows_by_state: Dict[str, List[Dict[str, Any]]] = {}
    failed: List[str] = []
    for state in states:
        try:
            rows = ptv.get_shift_volunteers(state)
            rows_by_state[state] = [_coerce_row(r, state, as_of_date) for r in rows]
            logger.info(f"[PTV] {state}: pulled {len(rows)} signups")
        except Exception as e:
            logger.exception(f"[PTV] {state}: pull failed -- {e}")
            failed.append(state)
    return rows_by_state, failed


# -- Stage 2: memory -> BQ --------------------------------------------------


def write_to_bq(
    bq: BigQueryConnector,
    rows_by_state: Dict[str, List[Dict[str, Any]]],
    as_of_date,
) -> List[str]:
    """
    Replace today's partition rows for the given states (idempotency on rerun),
    then append fresh. Returns list of states that wrote successfully.
    """
    states = list(rows_by_state.keys())
    if not states:
        return []

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
        # The view's per-(state, email, as_of_date) GROUP BY collapses any
        # exact-duplicate rows, so this is safe to skip on same-day rerun.
        logger.warning(f"[BQ] pre-delete failed (continuing): {e}")

    all_rows: List[Dict[str, Any]] = []
    for rows in rows_by_state.values():
        all_rows.extend(rows)

    if not all_rows:
        logger.info("[BQ] no rows to insert (states pulled empty)")
        return states

    try:
        bq.insert_rows(RAW_TABLE, all_rows)
        logger.info(
            f"[BQ] inserted {len(all_rows)} rows across {len(states)} states"
        )
        return states
    except Exception as e:
        logger.exception(f"[BQ] insert failed -- {e}")
        return []


# -- Stage 3: BQ view -> Airtable -------------------------------------------


def fetch_view_rows(
    bq: BigQueryConnector, states: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Query v_shift_volunteers_current filtered to the given states."""
    if not states:
        return {}
    state_list_sql = ", ".join(f"'{s}'" for s in states)
    sql = f"SELECT * FROM `{CURRENT_VIEW}` WHERE state IN ({state_list_sql})"
    by_state: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in bq.query(sql):
        d = dict(row)
        by_state[d["state"]].append(d)
    return by_state


def _map_to_airtable_fields(
    row: Dict[str, Any], field_map: Dict[str, str],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for bq_col, at_col in field_map.items():
        v = row.get(bq_col)
        if hasattr(v, "isoformat"):  # date / datetime
            v = v.isoformat()
        out[at_col] = v
    return out


def upsert_to_airtable(
    airtable: AirtableConnector,
    sync: SyncEntry,
    rows: List[Dict[str, Any]],
    upsert_key_bq: str,
) -> int:
    if not rows:
        logger.info(f"[AT][{sync.name}] no rows to upsert")
        return 0
    upsert_key_at = sync.field_map[upsert_key_bq]
    records = [
        {"fields": _map_to_airtable_fields(r, sync.field_map)}
        for r in rows
    ]
    # Drop rows missing the upsert key -- they'd create blank records
    records = [r for r in records if r["fields"].get(upsert_key_at)]
    airtable.batch_upsert(
        sync.base_id, sync.table, records, key_fields=[upsert_key_at],
    )
    logger.info(f"[AT][{sync.name}] upserted {len(records)} records")
    return len(records)


# -- Main -------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()

    config = load_config()
    as_of_date = datetime.now(timezone.utc).date()

    logger.info(f"=== Shift volunteers sync -- as_of_date={as_of_date} ===")
    logger.info(f"States to attempt: {config.unique_states}")
    logger.info(f"Sync targets: {[s.name for s in config.syncs]}")

    failed_states: List[str] = []
    failed_syncs: List[str] = []

    with PTVConnector() as ptv, BigQueryConnector() as bq, AirtableConnector() as at:
        rows_by_state, ptv_failed = fetch_ptv_for_states(
            ptv, config.unique_states, as_of_date,
        )
        failed_states.extend(ptv_failed)

        bq_successful = write_to_bq(bq, rows_by_state, as_of_date)
        for state in rows_by_state:
            if state not in bq_successful:
                failed_states.append(state)

        view_rows = fetch_view_rows(bq, bq_successful)
        for sync in config.syncs:
            if sync.state not in bq_successful:
                logger.warning(
                    f"[AT][{sync.name}] skipped -- state {sync.state} did not sync"
                )
                failed_syncs.append(sync.name)
                continue
            try:
                upsert_to_airtable(
                    at, sync, view_rows.get(sync.state, []), config.upsert_key_bq,
                )
            except Exception as e:
                logger.exception(f"[AT][{sync.name}] upsert failed -- {e}")
                failed_syncs.append(sync.name)

    logger.info(
        f"=== Done. failed_states={failed_states} failed_syncs={failed_syncs} ==="
    )
    return 1 if (failed_states or failed_syncs) else 0


if __name__ == "__main__":
    sys.exit(main())

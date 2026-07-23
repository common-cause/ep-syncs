"""
Sync PTV shift volunteers -> BigQuery raw -> Airtable.

Stages 1-2 (PTV -> BigQuery): pull volunteer signups from PTV's
shift_volunteers_csv endpoint for ALL states in PULL_STATES (plus any
registry state outside that list) and append today's snapshot to
ptv_raw_2026.shift_volunteers (partitioned by as_of_date).

Stage 3 (BigQuery -> Airtable): for each enabled row in
proj-tmc-mem-com.ep.shift_volunteer_sync_targets, query the per-volunteer
view filtered to that state and upsert into the target Airtable base/table
on email. The registry drives ONLY this stage -- the BigQuery landing is
national regardless of which states have Airtable targets.

Sync targets are written by ep-airtable-utilities at base-go-live time.
See bq/shift_volunteer_sync_targets.sql for the registry schema.

Per-state and per-sync failures are isolated. Exit code is non-zero if any
attempted state failed to land in BigQuery or any Airtable target failed
to upsert.

Usage:
    python sync_shift_volunteers.py                 # all states + all targets
    python sync_shift_volunteers.py --states NE,PA  # exact pull-set override
    python sync_shift_volunteers.py --bq-only       # skip the Airtable leg
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
SYNC_TARGETS_TABLE = f"{PROJECT}.ep.shift_volunteer_sync_targets"

# All 50 states + DC -- mirrors PULL_STATES in sync_all_volunteers.py (keep
# in lockstep). shift_volunteers_csv returns [] for states with no data, so
# pulling the full set is zero-config: empty states write no rows and new
# program states light up automatically. The Airtable leg is unaffected --
# it remains gated on the sync-targets registry.
PULL_STATES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]

# insert_rows_json sends one HTTP request with no chunking; a national pull
# can return enough rows to threaten the streaming-insert payload limit, so
# we batch (same pattern as sync_all_volunteers.py).
INSERT_CHUNK = 500

# Default BQ-col -> Airtable-col mapping for the canonical CC "Shifted
# Volunteers" base schema (contact columns only). Per-target overrides
# in shift_volunteer_sync_targets.field_map_overrides merge over this:
# a string value sets/replaces, a null value removes the key.
DEFAULT_FIELD_MAP: Dict[str, str] = {
    "email":        "Email",
    "first_name":   "First Name",
    "last_name":    "Last Name",
    "phone_number": "Phone Number",
    "county":       "County",
    "state":        "State",
}
UPSERT_KEY_BQ = "email"

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


def _merge_field_map(
    default: Dict[str, str], overrides: Optional[Dict[str, Optional[str]]],
) -> Dict[str, str]:
    """
    Merge override map over default map. String value sets/replaces;
    null/None value removes the key entirely.
    """
    merged = dict(default)
    for bq_col, at_col in (overrides or {}).items():
        if at_col is None:
            merged.pop(bq_col, None)
        else:
            merged[bq_col] = at_col
    return merged


def load_config(bq: BigQueryConnector) -> Config:
    """Load enabled sync targets from the BQ registry."""
    sql = f"""
        SELECT
          name,
          state,
          base_id,
          table_name,
          TO_JSON_STRING(field_map_overrides) AS field_map_overrides_json
        FROM `{SYNC_TARGETS_TABLE}`
        WHERE enabled = TRUE
        ORDER BY name
    """
    syncs: List[SyncEntry] = []
    for row in bq.query(sql):
        overrides_json = row["field_map_overrides_json"]
        overrides = (
            json.loads(overrides_json)
            if overrides_json and overrides_json != "null"
            else {}
        )
        merged = _merge_field_map(DEFAULT_FIELD_MAP, overrides)
        if UPSERT_KEY_BQ not in merged:
            raise ValueError(
                f"Sync '{row['name']}': merged field_map missing upsert key "
                f"'{UPSERT_KEY_BQ}'"
            )
        syncs.append(SyncEntry(
            name=row["name"],
            state=row["state"],
            base_id=row["base_id"],
            table=row["table_name"],
            field_map=merged,
        ))

    if not syncs:
        # Not fatal: the BigQuery landing (all PULL_STATES) is independently
        # valuable. The Airtable stage simply has nothing to do.
        logger.warning(
            f"No enabled sync targets in {SYNC_TARGETS_TABLE} -- the "
            "Airtable stage will be skipped. Have ep-airtable-utilities "
            "register a base, or flip an existing target's `enabled` flag."
        )

    return Config(syncs=syncs, upsert_key_bq=UPSERT_KEY_BQ)


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
        # The view's per-(state, email, as_of_date) GROUP BY collapses any
        # exact-duplicate rows, so this is safe to skip on same-day rerun.
        logger.warning(f"[BQ] pre-delete failed (continuing): {e}")

    successful: List[str] = []
    for state, rows in rows_by_state.items():
        try:
            n = 0
            for chunk in _chunks(rows, INSERT_CHUNK):
                bq.insert_rows(RAW_TABLE, chunk)
                n += len(chunk)
            if n:
                logger.info(f"[BQ] {state}: inserted {n} rows")
            successful.append(state)
        except Exception as e:
            logger.exception(f"[BQ] {state}: insert failed -- {e}")
    return successful


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


def _count_existing_keys(
    airtable: AirtableConnector, base_id: str, table: str, key_field: str,
) -> Dict[str, int]:
    """
    Return {normalized_key_value: count} of existing destination records.
    Used to detect rows where the upsert key matches >1 existing record --
    Airtable's batch_upsert 422s the whole batch in that case, so we skip
    those keys per-record instead.
    """
    counts: Dict[str, int] = defaultdict(int)
    for r in airtable.get_records(base_id, table):
        v = r["fields"].get(key_field)
        if isinstance(v, str) and v.strip():
            counts[v.strip().lower()] += 1
    return counts


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

    # Pre-scan the destination for keys that already match multiple
    # records. The "Shifted Volunteers" tables have a second write path
    # (an emergency self-add form) that can produce duplicates when a
    # self-add later also syncs in via PTV. Pushing one of those emails
    # through batch_upsert 422s the entire batch.
    existing_counts = _count_existing_keys(
        airtable, sync.base_id, sync.table, upsert_key_at,
    )

    records: List[Dict[str, Any]] = []
    skipped_dupes: List[str] = []
    for r in rows:
        fields = _map_to_airtable_fields(r, sync.field_map)
        key_value = fields.get(upsert_key_at)
        if not key_value:
            # Drop rows missing the upsert key -- they'd create blank records
            continue
        if existing_counts.get(str(key_value).strip().lower(), 0) > 1:
            skipped_dupes.append(str(key_value))
            continue
        records.append({"fields": fields})

    if skipped_dupes:
        logger.warning(
            f"[AT][{sync.name}] skipped {len(skipped_dupes)} record(s) due to "
            f"duplicate keys already in destination on '{upsert_key_at}': "
            f"{skipped_dupes}"
        )

    if not records:
        logger.info(f"[AT][{sync.name}] no upsertable records this run")
        return 0

    airtable.batch_upsert(
        sync.base_id, sync.table, records, key_fields=[upsert_key_at],
    )
    logger.info(f"[AT][{sync.name}] upserted {len(records)} records")
    return len(records)


# -- Main -------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync PTV shift_volunteers_csv -> BigQuery -> Airtable.",
    )
    p.add_argument(
        "--states",
        help="Comma-separated state codes to pull instead of the full set "
             "(ops / testing). Exact override: registry targets outside the "
             "subset are skipped without failing. e.g. --states NE,PA",
    )
    p.add_argument(
        "--bq-only",
        action="store_true",
        help="Run the PTV -> BigQuery stages only; skip the Airtable leg.",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()

    args = _parse_args(argv)
    as_of_date = datetime.now(timezone.utc).date()

    logger.info(f"=== Shift volunteers sync -- as_of_date={as_of_date} ===")

    failed_states: List[str] = []
    failed_syncs: List[str] = []

    with PTVConnector() as ptv, BigQueryConnector() as bq:
        config = load_config(bq)
        if args.states:
            pull_states = [
                s.strip().upper() for s in args.states.split(",") if s.strip()
            ]
        else:
            # Union so a registered target's state is always pulled, even if
            # it's ever a code outside PULL_STATES (e.g. a territory).
            pull_states = sorted(set(PULL_STATES) | set(config.unique_states))

        logger.info(f"States to pull: {len(pull_states)}")
        logger.info(f"Sync targets: {[s.name for s in config.syncs]}")

        rows_by_state, ptv_failed = fetch_ptv_for_states(
            ptv, pull_states, as_of_date,
        )
        failed_states.extend(ptv_failed)

        bq_successful = write_to_bq(bq, rows_by_state, as_of_date)
        for state in rows_by_state:
            if state not in bq_successful:
                failed_states.append(state)

        if args.bq_only:
            logger.info("[AT] skipped -- --bq-only")
        elif not config.syncs:
            logger.info("[AT] no enabled sync targets -- nothing to upsert")
        else:
            # Only the registry states' rows are read back from the view --
            # the other ~45 states land in BQ but have no Airtable leg.
            at_states = sorted(
                {s.state for s in config.syncs} & set(bq_successful)
            )
            with AirtableConnector() as at:
                view_rows = fetch_view_rows(bq, at_states)
                for sync in config.syncs:
                    if sync.state not in pull_states:
                        # Deliberate --states subset: not a failure.
                        logger.info(
                            f"[AT][{sync.name}] skipped -- state {sync.state} "
                            "not pulled this run (--states subset)"
                        )
                        continue
                    if sync.state not in bq_successful:
                        logger.warning(
                            f"[AT][{sync.name}] skipped -- state {sync.state} "
                            "did not sync"
                        )
                        failed_syncs.append(sync.name)
                        continue
                    try:
                        upsert_to_airtable(
                            at, sync, view_rows.get(sync.state, []),
                            config.upsert_key_bq,
                        )
                    except Exception as e:
                        logger.exception(
                            f"[AT][{sync.name}] upsert failed -- {e}"
                        )
                        failed_syncs.append(sync.name)

    logger.info(
        f"=== Done. failed_states={failed_states} failed_syncs={failed_syncs} ==="
    )
    return 1 if (failed_states or failed_syncs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

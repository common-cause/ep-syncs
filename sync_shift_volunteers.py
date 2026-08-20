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

TELEMETRY: every run appends one row per (stage, scope) to
proj-tmc-mem-com.ep.shift_sync_log -- see bq/shift_sync_log.sql. Two outcomes
here are deliberately NOT failures but must stay visible, because both used to
end in `exit 0` with nothing but an unread log line:

  * a volunteer skipped because their email already matches >1 destination
    record (would 422 the batch) -- they silently stop being updated;
  * a state that pulled zero rows after previously having some ('regressed'),
    which is indistinguishable in isolation from the ~43 states that
    legitimately return none every run.

Usage:
    python sync_shift_volunteers.py                 # all states + all targets
    python sync_shift_volunteers.py --states NE,PA  # exact pull-set override
    python sync_shift_volunteers.py --bq-only       # skip the Airtable leg
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
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
SYNC_LOG_TABLE = f"{PROJECT}.ep.shift_sync_log"

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


@dataclass
class UpsertStats:
    """Outcome of one target's Airtable leg, for the run log."""
    rows_in: int = 0
    rows_written: int = 0
    skipped_blank: int = 0
    skipped_dupe_exact: List[str] = field(default_factory=list)
    case_variants: List[str] = field(default_factory=list)


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


def prior_state_counts(bq: BigQueryConnector, as_of_date) -> Dict[str, int]:
    """
    Per-state row counts from the PREVIOUS SNAPSHOT -- the most recent
    as_of_date before this one that landed any rows at all.

    Used to tell a state that stopped reporting from one that never did.
    shift_volunteers_csv returns [] for states with no program, so ~43 of the
    51 pulled states land zero rows every run and a blanket zero-row warning
    would be pure noise. A state that had rows in the previous snapshot and
    has none now is the signal worth raising.

    Two things this deliberately does NOT do:

    * It does not use each state's own most recent NON-EMPTY snapshot. That
      would make "regressed" permanent: MA's shift data vanished 2026-08-18
      when the '24 roles were wiped in PTV, and since nothing lands for MA
      any more its last non-empty snapshot is pinned at 08-17 forever -- so
      every future run would re-report it. A regression is an EVENT. It
      should fire on the run where the drop happens and then go quiet, or
      it trains everyone to ignore the warning that matters.
    * It does not use "yesterday" literally. Runs are missed (a Civis pin
      outage silently killed the misc-jobs runs for 18 days), and if the
      previous calendar day landed nothing at all, every state would look
      unchanged and a real regression would slip through. Anchoring to the
      last date that actually has rows survives arbitrary gaps.
    """
    sql = f"""
        WITH prev AS (
          SELECT MAX(as_of_date) AS d
          FROM `{RAW_TABLE}`
          WHERE as_of_date < DATE '{as_of_date.isoformat()}'
        )
        SELECT state, COUNT(*) AS c
        FROM `{RAW_TABLE}`
        WHERE as_of_date = (SELECT d FROM prev)
        GROUP BY state
    """
    try:
        return {dict(r)["state"]: dict(r)["c"] for r in bq.query(sql)}
    except Exception as e:
        # Never let telemetry block the sync.
        logger.warning(f"[BQ] prior-count lookup failed (continuing): {e}")
        return {}


def write_to_bq(
    bq: BigQueryConnector,
    rows_by_state: Dict[str, List[Dict[str, Any]]],
    as_of_date,
    prior_counts: Optional[Dict[str, int]] = None,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """
    Replace today's partition rows for the pulled states (idempotency on
    rerun), then append fresh, per state.

    Returns (states that landed cleanly, per-state stats for the run log).
    """
    prior_counts = prior_counts or {}
    states = list(rows_by_state.keys())
    if not states:
        return [], {}

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
    stats: Dict[str, Dict[str, Any]] = {}
    for state, rows in rows_by_state.items():
        prior = prior_counts.get(state, 0)
        try:
            n = 0
            for chunk in _chunks(rows, INSERT_CHUNK):
                bq.insert_rows(RAW_TABLE, chunk)
                n += len(chunk)
            successful.append(state)
            if n:
                status = "ok"
                logger.info(f"[BQ] {state}: inserted {n} rows")
            elif prior:
                # Had data as recently as the last snapshot, has none now.
                status = "regressed"
                logger.warning(
                    f"[BQ] {state}: pulled 0 rows but the prior snapshot had "
                    f"{prior} -- PTV has stopped returning shift data for this "
                    "state. Expected if the election is over; otherwise "
                    "investigate. The view still serves the last good "
                    "snapshot, so downstream looks healthy either way."
                )
            else:
                status = "empty"
                logger.debug(f"[BQ] {state}: 0 rows (no program data)")
            stats[state] = {
                "rows_in": len(rows), "rows_written": n,
                "prior_rows": prior, "status": status, "detail": None,
            }
        except Exception as e:
            logger.exception(f"[BQ] {state}: insert failed -- {e}")
            stats[state] = {
                "rows_in": len(rows), "rows_written": 0,
                "prior_rows": prior, "status": "failed",
                "detail": f"{type(e).__name__}: {e}"[:1000],
            }
    return successful, stats


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
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """
    Return (exact_counts, normalized_counts) over existing destination records.

    Airtable's performUpsert matches `fieldsToMergeOn` case-SENSITIVELY --
    verified 2026-08-19 against a live base: with both `zz.casetest@…` and
    `ZZ.CaseTest@…` present, an upsert keyed on the lowercase spelling patched
    only the exact-case record and left the variant untouched. So the
    EXACT-spelling count is what decides whether a key would 422 the batch,
    and it alone gates the skip.

    normalized_counts is diagnostics only. A key whose normalized count exceeds
    its exact count has a case-variant twin in the destination: it upserts
    fine, but it means one human holds two records, which is worth surfacing
    before their data diverges. Counting normalized here and skipping on it --
    as this function used to -- made the guard stricter than the operation it
    guards, freezing a volunteer who would have patched cleanly.
    """
    exact: Dict[str, int] = defaultdict(int)
    normalized: Dict[str, int] = defaultdict(int)
    for r in airtable.get_records(base_id, table):
        v = r["fields"].get(key_field)
        if isinstance(v, str) and v.strip():
            stripped = v.strip()
            exact[stripped] += 1
            normalized[stripped.lower()] += 1
    return exact, normalized


def upsert_to_airtable(
    airtable: AirtableConnector,
    sync: SyncEntry,
    rows: List[Dict[str, Any]],
    upsert_key_bq: str,
) -> UpsertStats:
    stats = UpsertStats(rows_in=len(rows))
    if not rows:
        logger.info(f"[AT][{sync.name}] no rows to upsert")
        return stats
    upsert_key_at = sync.field_map[upsert_key_bq]

    # Pre-scan the destination for keys that already match multiple
    # records. The "Shifted Volunteers" tables have a second write path
    # (an emergency self-add form, plus hand-loads from
    # ep-airtable-utilities' load_volunteers.py) that can produce duplicates
    # when one of those later also syncs in via PTV. Pushing one of those
    # emails through batch_upsert 422s the entire batch.
    exact_counts, normalized_counts = _count_existing_keys(
        airtable, sync.base_id, sync.table, upsert_key_at,
    )

    records: List[Dict[str, Any]] = []
    for r in rows:
        fields = _map_to_airtable_fields(r, sync.field_map)
        key_value = fields.get(upsert_key_at)
        if not key_value:
            # Drop rows missing the upsert key -- they'd create blank records
            stats.skipped_blank += 1
            continue
        key = str(key_value).strip()
        n_exact = exact_counts.get(key, 0)
        if n_exact > 1:
            # Genuinely ambiguous to Airtable: this would 422 the batch.
            stats.skipped_dupe_exact.append(key)
            continue
        if normalized_counts.get(key.lower(), 0) > n_exact:
            # A case-variant twin exists. Airtable matches case-sensitively, so
            # our key still resolves to exactly one record and patches it --
            # record it and carry on rather than skipping.
            stats.case_variants.append(key)
        records.append({"fields": fields})

    if stats.skipped_dupe_exact:
        logger.warning(
            f"[AT][{sync.name}] SKIPPED {len(stats.skipped_dupe_exact)} "
            f"record(s): '{upsert_key_at}' already matches >1 destination "
            "record with the same exact spelling, which would 422 the whole "
            "batch. These volunteers are NOT being updated and will stay "
            "stale until the duplicates are merged in Airtable: "
            f"{stats.skipped_dupe_exact}"
        )
    if stats.case_variants:
        logger.warning(
            f"[AT][{sync.name}] {len(stats.case_variants)} record(s) have a "
            f"case-variant twin in the destination on '{upsert_key_at}'. "
            "Upserted normally (Airtable matches case-sensitively), but each "
            "one means two records for one human -- worth merging: "
            f"{stats.case_variants}"
        )

    if not records:
        logger.info(f"[AT][{sync.name}] no upsertable records this run")
        return stats

    airtable.batch_upsert(
        sync.base_id, sync.table, records, key_fields=[upsert_key_at],
    )
    stats.rows_written = len(records)
    logger.info(f"[AT][{sync.name}] upserted {len(records)} records")
    return stats


# -- Run log ----------------------------------------------------------------


def _ddl_path(filename: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bq", filename)


def ensure_log_table(bq: BigQueryConnector) -> bool:
    """
    Create ep.shift_sync_log if absent so a fresh container self-heals. The
    `ep` dataset already exists and is NOT created here -- `com-dbt@` has no
    bigquery.datasets.create (see misc_jobs/asana_ep_kanban.py:ensure_tables).

    Returns False if the table couldn't be ensured; the run log is telemetry
    and must never take the sync down with it.
    """
    try:
        existed = bq.table_exists(SYNC_LOG_TABLE)
        with open(_ddl_path("shift_sync_log.sql"), "r", encoding="utf-8") as fh:
            bq.query(fh.read())  # CREATE TABLE IF NOT EXISTS -- idempotent
        if not existed:
            # A just-created table isn't immediately visible to the streaming
            # insert endpoint (BQ eventual consistency -> transient 404).
            logger.info("[LOG] table created; pausing for streaming availability")
            time.sleep(8)
        return True
    except Exception as e:
        logger.warning(f"[LOG] could not ensure {SYNC_LOG_TABLE}: {e}")
        return False


def write_sync_log(
    bq: BigQueryConnector,
    run_at: datetime,
    as_of_date,
    pull_stats: Dict[str, Dict[str, Any]],
    upsert_stats: Dict[str, Tuple[str, UpsertStats]],
    skipped_syncs: Dict[str, Tuple[str, str]],
) -> None:
    """
    Append one row per (stage, scope) for this run. Failures here are logged
    and swallowed -- telemetry never fails the sync.
    """
    rows: List[Dict[str, Any]] = []
    base = {"run_at": run_at.isoformat(), "as_of_date": as_of_date.isoformat()}

    for state, st in sorted(pull_stats.items()):
        rows.append({
            **base, "stage": "ptv_pull", "scope": state, "state": state,
            "status": st["status"], "rows_in": st["rows_in"],
            "rows_written": st["rows_written"], "prior_rows": st["prior_rows"],
            "detail": st["detail"],
        })

    for name, (state, st) in sorted(upsert_stats.items()):
        keys = {
            "skipped_dupe_exact": st.skipped_dupe_exact,
            "case_variants": st.case_variants,
        }
        rows.append({
            **base, "stage": "airtable_upsert", "scope": name, "state": state,
            "status": "ok", "rows_in": st.rows_in,
            "rows_written": st.rows_written,
            "skipped_blank": st.skipped_blank,
            "skipped_dupe_exact": len(st.skipped_dupe_exact),
            "case_variants": len(st.case_variants),
            "skipped_keys": (
                json.dumps(keys)
                if st.skipped_dupe_exact or st.case_variants else None
            ),
        })

    for name, (state, reason) in sorted(skipped_syncs.items()):
        status = "failed" if reason.startswith("failed") else "skipped"
        rows.append({
            **base, "stage": "airtable_upsert", "scope": name, "state": state,
            "status": status, "detail": reason[:1000],
        })

    if not rows:
        return
    try:
        for chunk in _chunks(rows, INSERT_CHUNK):
            bq.insert_rows(SYNC_LOG_TABLE, chunk)
        logger.info(f"[LOG] wrote {len(rows)} row(s) to {SYNC_LOG_TABLE}")
    except Exception as e:
        logger.warning(f"[LOG] failed to write run log (continuing): {e}")


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

    run_at = datetime.now(timezone.utc)
    failed_states: List[str] = []
    failed_syncs: List[str] = []
    # For the run log: {sync name: (state, UpsertStats)} and, for targets that
    # never got as far as an upsert, {sync name: (state, reason)}.
    upsert_stats: Dict[str, Tuple[str, UpsertStats]] = {}
    skipped_syncs: Dict[str, Tuple[str, str]] = {}

    with PTVConnector() as ptv, BigQueryConnector() as bq:
        log_ok = ensure_log_table(bq)
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

        # Read the pre-run state of the table so a state that stopped
        # reporting can be told from one that never reported.
        priors = prior_state_counts(bq, as_of_date)

        bq_successful, pull_stats = write_to_bq(
            bq, rows_by_state, as_of_date, priors,
        )
        for state in rows_by_state:
            if state not in bq_successful:
                failed_states.append(state)

        regressed = [s for s, st in pull_stats.items()
                     if st["status"] == "regressed"]
        if regressed:
            logger.warning(
                f"[BQ] {len(regressed)} state(s) went to zero rows this run "
                f"after having data previously: {sorted(regressed)}"
            )

        if args.bq_only:
            logger.info("[AT] skipped -- --bq-only")
            for sync in config.syncs:
                skipped_syncs[sync.name] = (sync.state, "skipped -- --bq-only")
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
                        skipped_syncs[sync.name] = (
                            sync.state,
                            "skipped -- state not pulled (--states subset)",
                        )
                        continue
                    if sync.state not in bq_successful:
                        logger.warning(
                            f"[AT][{sync.name}] skipped -- state {sync.state} "
                            "did not sync"
                        )
                        failed_syncs.append(sync.name)
                        skipped_syncs[sync.name] = (
                            sync.state,
                            "failed -- state did not land in BigQuery",
                        )
                        continue
                    try:
                        upsert_stats[sync.name] = (
                            sync.state,
                            upsert_to_airtable(
                                at, sync, view_rows.get(sync.state, []),
                                config.upsert_key_bq,
                            ),
                        )
                    except Exception as e:
                        logger.exception(
                            f"[AT][{sync.name}] upsert failed -- {e}"
                        )
                        failed_syncs.append(sync.name)
                        skipped_syncs[sync.name] = (
                            sync.state,
                            f"failed -- {type(e).__name__}: {e}",
                        )

        if log_ok:
            write_sync_log(
                bq, run_at, as_of_date, pull_stats, upsert_stats,
                skipped_syncs,
            )

    stale = sorted(
        name for name, (_, st) in upsert_stats.items() if st.skipped_dupe_exact
    )
    logger.info(
        f"=== Done. failed_states={failed_states} "
        f"failed_syncs={failed_syncs} targets_with_skipped_volunteers={stale} ==="
    )
    return 1 if (failed_states or failed_syncs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

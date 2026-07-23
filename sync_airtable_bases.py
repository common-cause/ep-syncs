"""
Capture registered Airtable bases -> BigQuery (ep_2026_raw).

For each enabled row in proj-tmc-mem-com.ep.airtable_sync_sources:
  1. Read the base's live schema (tables + field types) via the Airtable
     metadata API and discover every table (minus the row's exclude_tables).
  2. For each table, fetch all records and:
       a. Rebuild the TYPED current-state table
          `ep_2026_raw.{bq_table_prefix}__{sanitized_table_name}` via a
          load job (WRITE_TRUNCATE -- full replace, schema-drift-proof:
          columns and types re-derive from Airtable field metadata every
          run). Lists/dicts (linked records, attachments, multi-selects)
          are JSON-serialized strings.
       b. Append one row per record to
          `ep_2026_raw.airtable_records_history` (verbatim JSON payload,
          as_of_date-partitioned) -- the drift-proof audit trail. Today's
          partition is pre-deleted per base for rerun idempotency.

READ-ONLY toward Airtable: this script never writes or deletes records.

Registration contract + seeds: bq/airtable_sync_sources.sql. History DDL
+ reader dedupe recipe: bq/airtable_records_history.sql. Full design:
docs/airtable_bases_sync_spec.md.

Per-base and per-table failures are isolated. Exit code is non-zero if
any enabled base or table failed.

Usage:
    python sync_airtable_bases.py                          # all enabled bases
    python sync_airtable_bases.py --bases ne_field_report,ut_quiz
    python sync_airtable_bases.py --only ne_field_report__incident_reports
    python sync_airtable_bases.py --list                   # show discovery, write nothing
    python sync_airtable_bases.py --check-access           # PAT coverage incl. disabled rows
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

from ccef_connections import AirtableConnector, BigQueryConnector


# -- Constants --------------------------------------------------------------

PROJECT = "proj-tmc-mem-com"
RAW_DATASET = "ep_2026_raw"
HISTORY_TABLE = f"{PROJECT}.{RAW_DATASET}.airtable_records_history"
SOURCES_TABLE = f"{PROJECT}.ep.airtable_sync_sources"

# insert_rows_json sends one HTTP request with no chunking; batch to stay
# under the streaming-insert payload limit (house pattern).
INSERT_CHUNK = 500

logger = logging.getLogger(__name__)


# -- Transform helpers -------------------------------------------------------
# Adapted from airtable-bq-sync/sync.py (separate repo, live Civis job) as of
# 2026-07. Keep divergences deliberate: this copy maps fields through an
# explicit per-table column map (collision-suffixed) instead of sanitizing
# ad hoc, and stamps _synced_at/_airtable_created_time as real timestamps.


def sanitize_column_name(name: str) -> str:
    """Turn an Airtable field/table name into a valid BigQuery identifier."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)   # replace non-alphanumeric
    name = re.sub(r"_+", "_", name)           # collapse runs of underscores
    name = name.strip("_")
    if name and name[0].isdigit():
        name = f"_{name}"
    return name or "_unnamed"


# Airtable field types that map to native BQ types via pandas nullable
# dtypes. Types not listed stay as object (string) -- the safe default;
# lists/dicts are JSON-serialized before they get here.
AIRTABLE_TYPE_MAP: Dict[str, str] = {
    # Numeric
    "number": "Float64",
    "currency": "Float64",
    "percent": "Float64",
    "duration": "Float64",       # seconds
    "autoNumber": "Int64",
    "count": "Int64",
    "rating": "Int64",
    # Boolean
    "checkbox": "boolean",
    # Temporal -- handled specially in coerce_column_types
    "date": "_datetime",
    "dateTime": "_datetime",
    "createdTime": "_datetime",
    "lastModifiedTime": "_datetime",
}


def coerce_column_types(
    df: pd.DataFrame, field_types: Dict[str, Optional[str]],
) -> pd.DataFrame:
    """Cast DataFrame columns per Airtable field types; warn-and-skip on failure."""
    for col, at_type in field_types.items():
        if col not in df.columns or at_type is None:
            continue
        target = AIRTABLE_TYPE_MAP.get(at_type)
        if target is None:
            continue
        try:
            if target == "_datetime":
                df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
            else:
                df[col] = df[col].astype(target)
        except Exception:
            logger.warning(
                f"  could not cast column {col} (AT type {at_type}) to "
                f"{target} -- leaving as-is"
            )
    return df


@dataclass
class TablePlan:
    """One Airtable table's capture plan, derived from the base schema."""
    name: str                                 # Airtable table name, verbatim
    key: str                                  # sanitized -> typed-table suffix
    col_map: Dict[str, str]                   # Airtable field name -> BQ column
    field_types: Dict[str, Optional[str]]     # BQ column -> Airtable type


@dataclass
class BaseEntry:
    name: str
    state: str
    base_id: str
    base_type: str
    bq_table_prefix: str
    exclude_tables: List[str]
    enabled: bool


def plan_tables(schema, exclude_tables: List[str]) -> List[TablePlan]:
    """
    Turn a pyairtable BaseSchema into per-table capture plans.

    Field-name sanitization collisions (two fields sanitizing to the same
    column) get deterministic _2/_3 suffixes by schema order, with an ERROR
    log naming both fields.
    """
    plans: List[TablePlan] = []
    excluded = set(exclude_tables or [])
    for table in schema.tables:
        if table.name in excluded:
            continue
        col_map: Dict[str, str] = {}
        field_types: Dict[str, Optional[str]] = {
            "_airtable_record_id": None,
            "_airtable_created_time": "createdTime",
        }
        seen: Dict[str, str] = {}  # sanitized col -> first field name
        for f in table.fields:
            col = sanitize_column_name(f.name)
            if col in seen:
                n = 2
                while f"{col}_{n}" in seen:
                    n += 1
                suffixed = f"{col}_{n}"
                logger.error(
                    f"  column collision in '{table.name}': field '{f.name}' "
                    f"sanitizes to '{col}' (taken by '{seen[col]}') -- "
                    f"landing as '{suffixed}'"
                )
                col = suffixed
            seen[col] = f.name
            col_map[f.name] = col
            field_types[col] = getattr(f, "type", None)
        field_types["_synced_at"] = None
        plans.append(TablePlan(
            name=table.name,
            key=sanitize_column_name(table.name),
            col_map=col_map,
            field_types=field_types,
        ))
    return plans


def flatten_record(record: Dict[str, Any], col_map: Dict[str, str]) -> Dict[str, Any]:
    """
    Flatten an Airtable record for the typed table, mapping field names
    through the schema-derived column map (fields not in the map -- e.g.
    added between schema fetch and record fetch -- fall back to plain
    sanitization). Lists/dicts are JSON-serialized strings.
    """
    row: Dict[str, Any] = {
        "_airtable_record_id": record["id"],
        "_airtable_created_time": record.get("createdTime"),
    }
    for field_name, value in record.get("fields", {}).items():
        col = col_map.get(field_name) or sanitize_column_name(field_name)
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        row[col] = value
    return row


# -- Registry ----------------------------------------------------------------


def load_sources(
    bq: BigQueryConnector, include_disabled: bool = False,
) -> List[BaseEntry]:
    where = "" if include_disabled else "WHERE enabled = TRUE"
    sql = f"""
        SELECT name, state, base_id, base_type, bq_table_prefix,
               exclude_tables, enabled
        FROM `{SOURCES_TABLE}`
        {where}
        ORDER BY bq_table_prefix
    """
    entries = [
        BaseEntry(
            name=row["name"],
            state=row["state"],
            base_id=row["base_id"],
            base_type=row["base_type"],
            bq_table_prefix=row["bq_table_prefix"],
            exclude_tables=list(row["exclude_tables"] or []),
            enabled=row["enabled"],
        )
        for row in bq.query(sql)
    ]
    return entries


# -- Capture -----------------------------------------------------------------


def _chunks(seq: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def sync_typed_table(
    bq: BigQueryConnector,
    entry: BaseEntry,
    plan: TablePlan,
    records: List[Dict[str, Any]],
    synced_at: datetime,
) -> int:
    """Full-replace the typed table for one (base, table). Returns row count."""
    destination = f"{RAW_DATASET}.{entry.bq_table_prefix}__{plan.key}"
    schema_columns = list(plan.field_types)

    if not records:
        df = pd.DataFrame(columns=schema_columns)
    else:
        df = pd.DataFrame([flatten_record(r, plan.col_map) for r in records])
        for col in schema_columns:
            if col not in df.columns:
                df[col] = None
        df = df[schema_columns]

    df["_synced_at"] = pd.Timestamp(synced_at)
    df = coerce_column_types(df, plan.field_types)
    # Remaining object columns -> StringDtype so None lands as real NULL.
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].astype(pd.StringDtype())

    bq.load_dataframe(df, destination, if_exists="replace")
    logger.info(
        f"[{entry.bq_table_prefix}] {plan.name}: {len(df)} rows -> "
        f"{destination} ({len(df.columns)} cols)"
    )
    return len(df)


def write_history_rows(
    bq: BigQueryConnector,
    entry: BaseEntry,
    plan: TablePlan,
    records: List[Dict[str, Any]],
    as_of_date,
    synced_at: datetime,
) -> int:
    """Append one JSON-payload row per record to the history table."""
    if not records:
        return 0
    synced_at_iso = synced_at.isoformat()
    rows = [
        {
            "as_of_date": as_of_date.isoformat(),
            "base_id": entry.base_id,
            "base_name": entry.name,
            "bq_table_prefix": entry.bq_table_prefix,
            "table_name": plan.name,
            "table_key": plan.key,
            "state": entry.state,
            "base_type": entry.base_type,
            "airtable_record_id": r["id"],
            "airtable_created_time": r.get("createdTime"),
            "fields": json.dumps(r.get("fields", {})),
            "synced_at": synced_at_iso,
        }
        for r in records
    ]
    n = 0
    for chunk in _chunks(rows, INSERT_CHUNK):
        bq.insert_rows(HISTORY_TABLE, chunk)
        n += len(chunk)
    return n


def predelete_history(bq: BigQueryConnector, entry: BaseEntry, as_of_date) -> None:
    """Idempotency: clear today's history rows for this base before re-append."""
    sql = (
        f"DELETE FROM `{HISTORY_TABLE}` "
        f"WHERE as_of_date = DATE '{as_of_date.isoformat()}' "
        f"AND bq_table_prefix = '{entry.bq_table_prefix}'"
    )
    try:
        bq.execute_dml(sql)
    except Exception as e:
        # Streaming buffer blocks DML on rows streamed in the last ~90 min.
        # Readers dedupe via the ROW_NUMBER recipe (see history DDL), so a
        # same-window rerun is benign.
        logger.warning(
            f"[{entry.bq_table_prefix}] history pre-delete failed (continuing): {e}"
        )


def sync_base(
    at: AirtableConnector,
    bq: BigQueryConnector,
    entry: BaseEntry,
    as_of_date,
    only_table: Optional[str] = None,
) -> Tuple[int, int, List[str]]:
    """
    Capture one base. Returns (tables_ok, rows_typed, failed_table_labels).
    Raises on base-level failures (schema fetch) -- caller isolates.
    """
    schema = at.get_base_schema(entry.base_id)
    plans = plan_tables(schema, entry.exclude_tables)
    if only_table:
        plans = [
            p for p in plans
            if f"{entry.bq_table_prefix}__{p.key}" == only_table
        ]
        if not plans:
            logger.warning(
                f"[{entry.bq_table_prefix}] no table matches --only {only_table}"
            )
            return 0, 0, []

    synced_at = datetime.now(timezone.utc)
    predelete_history(bq, entry, as_of_date)

    tables_ok = 0
    rows_typed = 0
    failed: List[str] = []
    for plan in plans:
        label = f"{entry.bq_table_prefix}__{plan.key}"
        try:
            records = at.get_records(entry.base_id, plan.name)
            rows_typed += sync_typed_table(bq, entry, plan, records, synced_at)
            n_hist = write_history_rows(
                bq, entry, plan, records, as_of_date, synced_at,
            )
            logger.info(f"[{entry.bq_table_prefix}] {plan.name}: {n_hist} history rows")
            tables_ok += 1
        except Exception as e:
            logger.exception(f"[{entry.bq_table_prefix}] {plan.name}: FAILED -- {e}")
            failed.append(label)
    return tables_ok, rows_typed, failed


# -- Read-only modes ----------------------------------------------------------


def run_list(at: AirtableConnector, entries: List[BaseEntry]) -> int:
    """Print each registry row + its discovered tables. Writes nothing."""
    for entry in entries:
        flag = "enabled" if entry.enabled else "DISABLED"
        try:
            schema = at.get_base_schema(entry.base_id)
            plans = plan_tables(schema, entry.exclude_tables)
            excluded = set(entry.exclude_tables or [])
            print(f"{entry.bq_table_prefix} [{flag}] {entry.name} ({entry.base_id})")
            for p in plans:
                print(f"    {entry.bq_table_prefix}__{p.key}  <-  '{p.name}'"
                      f"  ({len(p.col_map)} fields)")
            for name in excluded:
                print(f"    (excluded: '{name}')")
        except Exception as e:
            print(f"{entry.bq_table_prefix} [{flag}] {entry.name} "
                  f"({entry.base_id})  SCHEMA FETCH FAILED: {e}")
    return 0


def run_check_access(at: AirtableConnector, entries: List[BaseEntry]) -> int:
    """Verify PAT schema access for every registry row (incl. disabled)."""
    problems = 0
    for entry in entries:
        flag = "enabled" if entry.enabled else "disabled"
        try:
            schema = at.get_base_schema(entry.base_id)
            print(f"OK        {entry.bq_table_prefix} ({entry.base_id}, {flag}) "
                  f"-- {len(schema.tables)} tables")
        except Exception as e:
            problems += 1
            print(f"FORBIDDEN {entry.bq_table_prefix} ({entry.base_id}, {flag}) -- {e}")
    print(f"\n{len(entries) - problems}/{len(entries)} bases accessible")
    return 1 if problems else 0


# -- Main ----------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture registered Airtable bases into BigQuery (ep_2026_raw).",
    )
    p.add_argument(
        "--bases",
        help="Comma-separated bq_table_prefix values to sync (ops/testing). "
             "e.g. --bases ne_field_report,ut_quiz",
    )
    p.add_argument(
        "--only",
        help="Sync a single landed table, e.g. ne_field_report__incident_reports.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Show enabled registry rows + discovered tables; write nothing.",
    )
    p.add_argument(
        "--check-access",
        action="store_true",
        help="Verify PAT schema access for ALL registry rows (incl. disabled); "
             "write nothing.",
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
    logger.info(f"=== Airtable bases capture -- as_of_date={as_of_date} ===")

    with BigQueryConnector() as bq, AirtableConnector() as at:
        entries = load_sources(bq, include_disabled=args.check_access or args.list)

        if args.check_access:
            return run_check_access(at, entries)
        if args.list:
            return run_list(at, entries)

        enabled = [e for e in entries if e.enabled]

        if args.only:
            prefix = args.only.split("__", 1)[0]
            enabled = [e for e in enabled if e.bq_table_prefix == prefix]
        elif args.bases:
            wanted = {b.strip() for b in args.bases.split(",") if b.strip()}
            unknown = wanted - {e.bq_table_prefix for e in enabled}
            if unknown:
                logger.error(f"--bases prefixes not enabled/known: {sorted(unknown)}")
                return 1
            enabled = [e for e in enabled if e.bq_table_prefix in wanted]

        if not enabled:
            logger.error(
                f"No enabled bases selected from {SOURCES_TABLE}. Seed/enable "
                "rows there (see bq/airtable_sync_sources.sql)."
            )
            return 1

        logger.info(f"Bases to capture: {[e.bq_table_prefix for e in enabled]}")

        bases_ok = 0
        total_tables = 0
        total_rows = 0
        failed_bases: List[str] = []
        failed_tables: List[str] = []
        for entry in enabled:
            try:
                t_ok, r_typed, t_failed = sync_base(
                    at, bq, entry, as_of_date, only_table=args.only,
                )
                total_tables += t_ok
                total_rows += r_typed
                failed_tables.extend(t_failed)
                if not t_failed:
                    bases_ok += 1
            except Exception as e:
                logger.exception(f"[{entry.bq_table_prefix}] base FAILED -- {e}")
                failed_bases.append(entry.bq_table_prefix)

        # Regenerate + redeploy the ep_2026_cleaned union views so a base
        # going live (new registry row) is included the same run. Also
        # rewrites the committed bq/ep_2026_cleaned/3x_*.sql snapshots when
        # running locally (no-op churn in Civis's throwaway clone).
        try:
            import airtable_views
            airtable_views.render_write_apply(bq)
        except Exception as e:
            logger.exception(f"[views] union-view regeneration FAILED -- {e}")
            failed_tables.append("ep_2026_cleaned view regeneration")

    logger.info(
        f"=== Done. bases_ok={bases_ok}/{len(enabled)} tables_ok={total_tables} "
        f"rows_typed={total_rows} failed_bases={failed_bases} "
        f"failed_tables={failed_tables} ==="
    )
    return 1 if (failed_bases or failed_tables) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

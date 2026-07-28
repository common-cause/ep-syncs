"""
Asana EP volunteer-pipeline boards -> asana_raw_2026 (BigQuery).

Captures every board registered in `ep.asana_sync_sources` as a daily snapshot:
one row per (task, day) in `asana_raw_2026.ep_kanban_tasks`, plus one row per
(board, day) of board structure in `asana_raw_2026.projects`.

Why this exists: Common Cause makes PTV and the Airtable templates available to
state EP programs but does not require them. NM (CCNM) runs volunteer
onboarding on an Asana Kanban board and uses neither, so its tracked EP
volunteers were invisible to every `ep_2026_cleaned` consumer. Absorbing a
state's own tooling into the canonical contracts is what that layer is for.
Design + the open questions with NM: `docs/asana_nm_sync_spec.md`.

Grain assumption: registry rows with project_kind='volunteer_pipeline' are
ONE TASK PER VOLUNTEER, with the board's sections as pipeline stages. Boards
organised otherwise must not be registered with that kind.

Board conventions live in the REGISTRY, not here (stage_order, and where the
volunteer's email is). These boards are maintained by hand by state staff and
encode meaning in section names and free text, so a state improving its board
is a registry UPDATE rather than a code change. Notably the NM board has no
custom fields at all -- the volunteer's email is the task notes body -- so this
module resolves email in registry-declared priority (custom field, then notes
regex) and records which path won in `email_source`, plus `notes_residual` so
convention drift shows up in data instead of silently breaking the parse.

READ-ONLY toward Asana. Nothing here writes back to a board a state team owns.

Idempotency: pre-delete today's partition for the boards about to be written,
then append. Same-day reruns collapse EXCEPT inside BigQuery's ~90-minute
streaming-buffer window, where the DELETE fails and a rerun can leave
duplicates -- so consumers dedupe on (as_of_date, task_gid). This is the house
pattern (see the knowledge-library entry
`bigquery-streaming-buffer-blocks-dml-for-30-90-minutes`).

Credentials come from the environment (ASANA_API_KEY_PASSWORD,
BIGQUERY_CREDENTIALS_PASSWORD); run_misc_jobs.py loads .env before calling
run().
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ccef_connections import AsanaConnector, BigQueryConnector

logger = logging.getLogger(__name__)

BQ_PROJECT = "proj-tmc-mem-com"
DATASET = "asana_raw_2026"
TASKS_TABLE = f"{BQ_PROJECT}.{DATASET}.ep_kanban_tasks"
PROJECTS_TABLE = f"{BQ_PROJECT}.{DATASET}.projects"
REGISTRY = f"{BQ_PROJECT}.ep.asana_sync_sources"

# Streaming-insert batch size. Boards are tiny (tens of rows); this only
# matters if a state ever stands up a very large board.
INSERT_CHUNK = 500

# Fields requested per task. Wider than AsanaConnector.DEFAULT_TASK_FIELDS:
# we need `projects` to pick the right membership when a task belongs to
# several boards, and `tags.name` for recruitment source.
TASK_FIELDS = (
    "name,notes,completed,completed_at,created_at,modified_at,due_on,start_on,"
    "assignee.name,assignee.email,memberships.section.name,"
    "memberships.section.gid,memberships.project.gid,tags.name,custom_fields,"
    "num_subtasks,permalink_url"
)

PROJECT_FIELDS = (
    "name,archived,created_at,modified_at,notes,owner.name,team.name,"
    "custom_field_settings.custom_field.name,"
    "custom_field_settings.custom_field.type,"
    "custom_field_settings.custom_field.enum_options.name"
)

REGISTRY_SQL = f"""
SELECT name, state, project_gid, workspace_gid, project_kind, stage_order,
       email_field_name, email_from_notes, phone_field_name, phone_from_notes
FROM `{REGISTRY}`
WHERE enabled
ORDER BY state, name
"""

# Deliberately permissive: we are parsing addresses a human pasted into a
# free-text note, so tolerate surrounding punctuation and mailto: wrappers.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")

# A phone-shaped run: 10-15 digits allowing spaces, dashes, dots, parens and a
# leading +. Applied only AFTER the email is removed, so an address containing
# digits can't be misread as a number.
PHONE_RE = re.compile(r"\+?\d[\d\-.()\s]{8,}\d")


def _norm_email(value: Optional[str]) -> Optional[str]:
    """TRIM+LOWER, blank -> None. Mirrors ep_2026_cleaned.norm_email so the
    landed value already satisfies the interface layer's email contract."""
    if not value:
        return None
    out = value.strip().lower()
    return out or None


def _norm_phone(value: Optional[str]) -> Optional[str]:
    """Strip non-digits, keep the last 10, blank -> None. Mirrors
    ep_2026_cleaned.norm_phone exactly, so the landed value already satisfies
    the interface layer's phone contract (and joins to PTV-derived phones)."""
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return digits[-10:] or None


def _ts(value: Optional[str]) -> Optional[str]:
    """Pass an Asana ISO-8601 timestamp through for BQ, or None."""
    return value or None


def _chunks(seq: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _custom_field_value(task: Dict[str, Any], field_name: Optional[str]) -> Optional[str]:
    """Return a task's custom-field display_value by field name, else None.

    `display_value` is Asana's universal string rendering, which is the right
    consumption path for a sync -- it is populated regardless of the field's
    underlying type.
    """
    if not field_name:
        return None
    wanted = field_name.strip().lower()
    for cf in task.get("custom_fields") or []:
        if (cf.get("name") or "").strip().lower() == wanted:
            return cf.get("display_value")
    return None


def _resolve_contact(
    task: Dict[str, Any], src: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str]:
    """Resolve the volunteer's email and phone in registry-declared priority.

    Returns (email, email_source, phone, phone_source, notes_residual).

    A real custom field wins over free text whenever it is populated, so a
    board mid-migration (field added, notes not yet cleaned) reports the
    structured value and stops depending on the note.

    The residual is what's left of the notes body after removing whatever we
    matched there -- a convention-drift signal, computed from the notes text
    regardless of which source actually won. Email is removed first so an
    address containing digits can never be misread as a phone number.
    """
    notes = (task.get("notes") or "").strip()

    em = EMAIL_RE.search(notes)
    residual = (notes[:em.start()] + notes[em.end():]).strip() if em else notes

    ph = PHONE_RE.search(residual)
    if ph:
        residual = (residual[:ph.start()] + residual[ph.end():]).strip()

    email = _norm_email(_custom_field_value(task, src.get("email_field_name")))
    email_source = "custom_field" if email else None
    if not email and src.get("email_from_notes") and em:
        email, email_source = _norm_email(em.group()), "notes"

    phone = _norm_phone(_custom_field_value(task, src.get("phone_field_name")))
    phone_source = "custom_field" if phone else None
    if not phone and src.get("phone_from_notes") and ph:
        phone, phone_source = _norm_phone(ph.group()), "notes"

    return email, email_source, phone, phone_source, residual


def _stage_for_project(task: Dict[str, Any], project_gid: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull (section_name, section_gid) for THIS board.

    A task can belong to several projects, each with its own section, so the
    membership must be selected by project_gid rather than taking the first.
    """
    for m in task.get("memberships") or []:
        if ((m.get("project") or {}).get("gid")) == project_gid:
            section = m.get("section") or {}
            return section.get("name"), section.get("gid")
    return None, None


def flatten_task(
    task: Dict[str, Any],
    src: Dict[str, Any],
    project_name: Optional[str],
    as_of_date: datetime.date,
) -> Dict[str, Any]:
    """Flatten one Asana task into an ep_kanban_tasks row."""
    stage, stage_gid = _stage_for_project(task, src["project_gid"])
    stage_order_list = list(src.get("stage_order") or [])
    stage_order = stage_order_list.index(stage) if stage in stage_order_list else None

    email, email_source, phone, phone_source, residual = _resolve_contact(task, src)

    tags = sorted((t.get("name") or "") for t in (task.get("tags") or []))
    assignee = task.get("assignee") or {}

    return {
        "as_of_date": as_of_date.isoformat(),
        "task_gid": task.get("gid"),
        "project_gid": src["project_gid"],
        "project_name": project_name,
        "state": src["state"],
        "task_name": (task.get("name") or "").strip() or None,
        "stage": stage,
        "stage_gid": stage_gid,
        "stage_order": stage_order,
        "source_tags": ", ".join(t for t in tags if t),
        "parsed_email": email,
        "email_source": email_source,
        "notes_had_email": email is not None,
        "parsed_phone": phone,
        "phone_source": phone_source,
        "notes_residual": residual or None,
        "assignee_name": assignee.get("name"),
        "assignee_email": _norm_email(assignee.get("email")),
        "completed": bool(task.get("completed")),
        "completed_at": _ts(task.get("completed_at")),
        "due_on": task.get("due_on"),
        "start_on": task.get("start_on"),
        "num_subtasks": task.get("num_subtasks"),
        "task_created_at": _ts(task.get("created_at")),
        "task_modified_at": _ts(task.get("modified_at")),
        # JSON column: streaming insert takes a JSON-encoded string.
        "custom_fields_json": json.dumps(task.get("custom_fields") or []),
        "permalink_url": task.get("permalink_url"),
    }


def flatten_project(
    proj: Dict[str, Any],
    sections: List[Dict[str, Any]],
    src: Dict[str, Any],
    as_of_date: datetime.date,
) -> Dict[str, Any]:
    """Flatten board metadata into a projects row."""
    cfs = [
        (s.get("custom_field") or {}) for s in (proj.get("custom_field_settings") or [])
    ]
    return {
        "as_of_date": as_of_date.isoformat(),
        "project_gid": src["project_gid"],
        "project_name": proj.get("name"),
        "state": src["state"],
        "workspace_gid": src.get("workspace_gid"),
        "team_name": (proj.get("team") or {}).get("name"),
        "owner_name": (proj.get("owner") or {}).get("name"),
        "archived": bool(proj.get("archived")),
        "project_created_at": _ts(proj.get("created_at")),
        "project_modified_at": _ts(proj.get("modified_at")),
        "project_notes": (proj.get("notes") or "").strip() or None,
        "section_names": [s.get("name") for s in sections if s.get("name")],
        "sections_json": json.dumps(
            [{"gid": s.get("gid"), "name": s.get("name")} for s in sections]
        ),
        "custom_fields_json": json.dumps(cfs),
        "n_custom_fields": len(cfs),
    }


# -- BigQuery ---------------------------------------------------------------


def _ddl_path(filename: str):
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "bq" / filename


def ensure_tables(bq: BigQueryConnector) -> None:
    """Create the dataset and both landing tables if absent, so a fresh
    container (or a fresh BQ project) self-heals."""
    bq.query(
        f"CREATE SCHEMA IF NOT EXISTS `{BQ_PROJECT}.{DATASET}` "
        "OPTIONS(location='US', description="
        '"Raw daily snapshots of Asana boards used by state EP programs that '
        'deploy volunteers outside PTV/Airtable. Written by ep-syncs '
        'misc_jobs/asana_ep_kanban.py. Contains PII; access-controlled.")'
    )
    created = False
    for filename, table in (
        ("asana_ep_kanban_tasks.sql", TASKS_TABLE),
        ("asana_projects.sql", PROJECTS_TABLE),
    ):
        existed = bq.table_exists(table)
        with open(_ddl_path(filename), "r", encoding="utf-8") as fh:
            bq.query(fh.read())  # CREATE TABLE IF NOT EXISTS -- idempotent
        created = created or not existed
    if created:
        # A just-created table isn't immediately available to the streaming
        # insert endpoint (BQ eventual consistency -> transient 404). Pause so
        # the first-ever run doesn't lean solely on the insert retry.
        logger.info("[BQ] table created; pausing for streaming-insert availability")
        time.sleep(8)
    logger.info(f"[BQ] ensured {TASKS_TABLE} and {PROJECTS_TABLE}")


def _replace_partition(
    bq: BigQueryConnector,
    table: str,
    project_gid: str,
    as_of_date: datetime.date,
    rows: List[Dict[str, Any]],
) -> None:
    """Pre-delete this board's rows in today's partition, then append.

    A failed DELETE is logged and tolerated: inside BQ's ~90-min streaming
    buffer window it cannot succeed, and blocking the insert would lose the
    day's snapshot entirely. Consumers dedupe on (as_of_date, task_gid) /
    (as_of_date, project_gid).
    """
    try:
        bq.execute_dml(
            f"DELETE FROM `{table}` "
            f"WHERE as_of_date = DATE '{as_of_date.isoformat()}' "
            f"AND project_gid = '{project_gid}'"
        )
    except Exception as e:
        logger.warning(f"[BQ] {table}: pre-delete failed (continuing): {e}")

    if not rows:
        return
    for chunk in _chunks(rows, INSERT_CHUNK):
        bq.insert_rows(table, chunk)


def _report_dry_run(
    label: str, rows: List[Dict[str, Any]], prow: Dict[str, Any]
) -> None:
    """Print what WOULD be written: distributions plus one PII-masked sample
    row. Masks names/emails so dry-run output is safe to paste into a ticket
    or a spec doc."""
    from collections import Counter

    logger.info(f"[dry-run] {label}: board={prow['project_name']!r} "
                f"team={prow['team_name']!r} owner={prow['owner_name']!r} "
                f"custom_fields={prow['n_custom_fields']} "
                f"sections={prow['section_names']}")
    logger.info(f"[dry-run] {label}: {len(rows)} task rows would be written")
    for field in ("stage", "stage_order", "email_source", "phone_source",
                  "source_tags"):
        logger.info(f"[dry-run]   {field}: "
                    f"{dict(Counter(r[field] for r in rows))}")
    logger.info(f"[dry-run]   notes_had_email: "
                f"{dict(Counter(r['notes_had_email'] for r in rows))}")
    logger.info(f"[dry-run]   notes_residual non-empty: "
                f"{sum(1 for r in rows if r['notes_residual'])}")
    logger.info(f"[dry-run]   distinct parsed_email: "
                f"{len({r['parsed_email'] for r in rows if r['parsed_email']})}")
    if rows:
        sample = dict(rows[0])
        for key in ("task_name", "assignee_name"):
            if sample.get(key):
                sample[key] = " ".join(
                    p[0] + "." * (len(p) - 1) for p in str(sample[key]).split()
                )
        for key in ("parsed_email", "assignee_email", "notes_residual"):
            if sample.get(key):
                sample[key] = re.sub(
                    r"[\w.+-]+@[\w.-]+", lambda m: m.group()[:2] + "***@***",
                    str(sample[key]),
                )
        if sample.get("parsed_phone"):
            sample["parsed_phone"] = "***" + str(sample["parsed_phone"])[-2:]
        logger.info(f"[dry-run]   sample row (masked): {sample}")


# -- Entry point ------------------------------------------------------------


def run(dry_run: bool = False) -> None:
    """Capture every enabled board. The scheduled path calls run() with no
    args; dry_run=True is the ops/testing path (flatten and report, write
    nothing) reached via `python misc_jobs/asana_ep_kanban.py --dry-run`."""
    as_of_date = datetime.datetime.now(datetime.timezone.utc).date()

    with BigQueryConnector(project_id=BQ_PROJECT) as bq:
        sources = [dict(r) for r in bq.query(REGISTRY_SQL)]
        if not sources:
            logger.warning(
                "[asana] no enabled rows in ep.asana_sync_sources -- nothing to do"
            )
            return
        logger.info(f"[asana] {len(sources)} enabled board(s): "
                    f"{', '.join(s['name'] for s in sources)}")
        if dry_run:
            logger.info("[asana] DRY RUN -- no tables ensured, nothing written")
        else:
            ensure_tables(bq)

        failures: List[str] = []
        with AsanaConnector() as asana:
            for src in sources:
                label = f"{src['state']}/{src['name']}"
                try:
                    proj = asana.get_project(
                        src["project_gid"], opt_fields=PROJECT_FIELDS
                    )
                    sections = asana.get_sections(src["project_gid"])
                    tasks = asana.get_project_tasks(
                        src["project_gid"], opt_fields=TASK_FIELDS
                    )

                    rows = [
                        flatten_task(t, src, proj.get("name"), as_of_date)
                        for t in tasks
                    ]
                    prow = flatten_project(proj, sections, src, as_of_date)
                    if dry_run:
                        _report_dry_run(label, rows, prow)
                    else:
                        _replace_partition(
                            bq, TASKS_TABLE, src["project_gid"], as_of_date, rows
                        )
                        _replace_partition(
                            bq, PROJECTS_TABLE, src["project_gid"], as_of_date,
                            [prow],
                        )

                    # Health signals worth seeing in the Civis log every night.
                    with_email = sum(1 for r in rows if r["notes_had_email"])
                    unranked = sorted(
                        {r["stage"] for r in rows if r["stage_order"] is None}
                    )
                    residual = sum(1 for r in rows if r["notes_residual"])
                    logger.info(
                        f"[asana] {label}: {len(rows)} tasks, "
                        f"{with_email} with email "
                        f"({len(rows) - with_email} invisible to "
                        f"ep_2026_cleaned.volunteers)"
                    )
                    if unranked:
                        logger.warning(
                            f"[asana] {label}: section(s) not in registry "
                            f"stage_order (stage_order=NULL): {unranked} -- "
                            f"update ep.asana_sync_sources.stage_order"
                        )
                    if residual:
                        logger.warning(
                            f"[asana] {label}: {residual} task(s) have leftover "
                            f"notes text beyond the email -- the board's notes "
                            f"convention may be drifting; check notes_residual"
                        )
                    if proj.get("archived"):
                        logger.warning(
                            f"[asana] {label}: board is ARCHIVED in Asana but "
                            f"still enabled in the registry -- confirm with the "
                            f"state program, then disable the registry row"
                        )
                except Exception as e:
                    # One bad board must not cost the others their snapshot.
                    logger.error(f"[asana] {label}: FAILED: {e}", exc_info=True)
                    failures.append(label)

    if failures:
        raise RuntimeError(
            f"asana capture failed for {len(failures)} board(s): "
            f"{', '.join(failures)}"
        )


if __name__ == "__main__":
    # Standalone dev entrypoint; the scheduled path is run_misc_jobs.py.
    import argparse
    import logging as _logging

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pull the boards and report what would be written (distributions "
             "plus a PII-masked sample row); write nothing to BigQuery.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    run(dry_run=args.dry_run)

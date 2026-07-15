"""
Miscellaneous EP sync jobs -- one nightly runner for small, periodic
BigQuery / Sheets / etc. exports that don't each warrant their own Civis job.

Rather than spinning up a new one-off Civis job every time a program needs a
recurring export, register the task here and let the shared runner drive it on
a schedule. The runner and its task modules are version-controlled, so adding
or changing a scheduled export is a git commit, not Civis-console surgery.

How it works:
  - Each task lives in a module under misc_jobs/ and exposes ``run() -> None``.
  - Register it in JOBS below (key + description + the run callable).
  - Decide which nights it runs in misc_jobs_schedule.yaml (per-weekday, in
    US/Eastern), NOT in code.
  - ONE Civis job runs ``python run_misc_jobs.py`` nightly (~3 AM ET). Each
    run executes only the tasks scheduled for tonight's ET weekday. See
    civis/SCHEDULED_SCRIPTS.md.

Per-job failures are isolated: the runner logs each job's outcome and exits
non-zero if any selected job raised, so Civis surfaces the failure without one
bad job blocking the others.

Usage:
    python run_misc_jobs.py                              # tasks scheduled for tonight (ET)
    python run_misc_jobs.py --as-of mon                  # pretend it's Monday night (testing)
    python run_misc_jobs.py --only event_975203_signups  # run regardless of schedule (ops)
    python run_misc_jobs.py --list                       # list tasks + their schedule, run nothing
    python run_misc_jobs.py --config path/to/other.yaml  # alternate schedule file
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from misc_jobs import event_975203_signups

logger = logging.getLogger(__name__)

# The job fires at ~3 AM ET; we resolve "tonight's weekday" in this zone so a
# task's scheduled day stays correct across DST and never straddles the UTC
# date boundary.
SCHEDULE_TZ = ZoneInfo("America/New_York")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CONFIG = Path(__file__).with_name("misc_jobs_schedule.yaml")


@dataclass(frozen=True)
class MiscJob:
    """One registered task: a stable key, a human description, and its run callable.

    *When* it runs lives in misc_jobs_schedule.yaml, keyed by this ``key`` --
    not here. Code says what a task is; the YAML says which nights it runs.
    """

    key: str
    description: str
    run: Callable[[], None]


# The registry. Add a row to make a task runnable, then give it a schedule in
# misc_jobs_schedule.yaml. Remove/retire the row when a time-boxed export is
# done (e.g. after the FL training series ends 2026-08-16).
JOBS: List[MiscJob] = [
    MiscJob(
        key="event_975203_signups",
        description="Mobilize event 975203 (FL trainings Jul-Aug 2026) signup "
                    "roster -> Google Sheet for FL program.",
        run=event_975203_signups.run,
    ),
]

JOBS_BY_KEY: Dict[str, MiscJob] = {j.key: j for j in JOBS}


def load_schedule(path: Path) -> Dict[str, dict]:
    """Read the per-task schedule from YAML, returning {job_key: entry}.

    Validates weekday tokens up front so a typo fails loudly instead of
    silently skipping a task forever.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    jobs = raw.get("jobs") or {}
    for key, entry in jobs.items():
        days = entry.get("days")
        if days == "daily":
            continue
        if not isinstance(days, list) or not days:
            raise ValueError(f"schedule for '{key}': `days` must be a non-empty "
                             f"list of weekdays or the string 'daily'")
        bad = [d for d in days if d not in WEEKDAYS]
        if bad:
            raise ValueError(f"schedule for '{key}': unknown weekday(s) {bad}; "
                             f"use {WEEKDAYS}")
    return jobs


def runs_tonight(entry: dict, weekday: str) -> bool:
    """Whether a schedule entry is enabled and fires on `weekday`."""
    if not entry.get("enabled", False):
        return False
    days = entry.get("days")
    return days == "daily" or weekday in days


def select_scheduled(schedule: Dict[str, dict], weekday: str) -> List[MiscJob]:
    """Registered tasks that should run on `weekday`, warning about mismatches."""
    # Schedule entries that don't correspond to a registered task -> warn.
    for key in schedule:
        if key not in JOBS_BY_KEY:
            logger.warning(f"schedule references unknown job key '{key}' "
                           f"(not in JOBS) -- ignoring")
    selected = []
    for job in JOBS:
        entry = schedule.get(job.key)
        if entry is None:
            logger.warning(f"job '{job.key}' has no schedule entry -- it will "
                           f"never run; add it to the schedule file")
            continue
        if runs_tonight(entry, weekday):
            selected.append(job)
    return selected


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the miscellaneous EP sync jobs scheduled for tonight."
    )
    p.add_argument(
        "--as-of",
        metavar="DOW",
        help="Override tonight's weekday (mon..sun) for testing instead of the "
             "real US/Eastern day.",
    )
    p.add_argument(
        "--only",
        help="Comma-separated job keys to run regardless of the schedule "
             "(ops / testing). e.g. --only event_975203_signups",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to the schedule YAML (default: misc_jobs_schedule.yaml).",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List registered jobs with their schedule and exit.",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()

    args = _parse_args(argv)

    try:
        schedule = load_schedule(args.config)
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.error(f"could not load schedule {args.config}: {e}")
        return 1

    if args.list:
        for j in JOBS:
            entry = schedule.get(j.key)
            if entry is None:
                when = "UNSCHEDULED"
            elif not entry.get("enabled", False):
                when = "disabled"
            else:
                days = entry.get("days")
                when = "daily" if days == "daily" else ",".join(days)
            logger.info(f"{j.key}  [{when}]  {j.description}")
        return 0

    if args.as_of:
        weekday = args.as_of.strip().lower()
        if weekday not in WEEKDAYS:
            logger.error(f"--as-of must be one of {WEEKDAYS}")
            return 1
    else:
        weekday = datetime.now(SCHEDULE_TZ).strftime("%a").lower()

    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        selected = [JOBS_BY_KEY[k] for k in wanted if k in JOBS_BY_KEY]
        missing = wanted - set(JOBS_BY_KEY)
        if missing:
            logger.error(f"unknown job keys: {sorted(missing)}")
            return 1
        logger.info(f"=== Misc EP sync jobs -- --only override, {len(selected)} job(s) ===")
    else:
        selected = select_scheduled(schedule, weekday)
        if not selected:
            logger.info(f"no jobs scheduled for {weekday} (ET) -- nothing to do")
            return 0
        logger.info(f"=== Misc EP sync jobs -- {weekday} (ET), running "
                    f"{len(selected)} job(s) ===")

    failed: List[str] = []
    for j in selected:
        logger.info(f"--- {j.key}: starting ---")
        try:
            j.run()
            logger.info(f"--- {j.key}: done ---")
        except Exception as e:
            logger.exception(f"--- {j.key}: FAILED -- {e} ---")
            failed.append(j.key)

    logger.info(f"=== Done. ok={len(selected) - len(failed)} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

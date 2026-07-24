"""
Sync PTV training signups (admin GUI) -> BigQuery raw.

PTV exposes NO API for training signups (unlike users_csv / shift_volunteers_csv),
so this rides the ptv-tools browser session. For each state it walks:

    /state_admin/trainings                              (list: id, name, status)
      -> /state_admin/trainings/<tid>                   (show: role_id, host, quiz, schedule_ids)
        -> /state_admin/trainings/<tid>/attendees/<sid> (roster: one <section> per signup)

and appends today's per-state snapshot to ptv_raw_2026.training_signups
(partitioned by as_of_date), one row per (session, attendee). This captures
what the flat users_csv `training` field cannot: a REAL signup timestamp, every
training a user signed up for (incl. past cycles), the PTV training id (which
maps to ep_dashboards.training_event_map), plus registration status / attended.

READ-ONLY toward PTV: navigation and DOM reads only. It never clicks
set_state's neighbours, cancel_registration, publish, save, or any mutating
control.

State scoping is the footgun ptv-tools warns about: PTV's "current state" is
server-side session context that every /state_admin/* page silently follows,
and the session is shared machine-wide. This sync therefore switches state and
VERIFIES it (via the Download State User CSV href's state_code=, exactly like
ptv_tools.set_state) before scraping, and refuses to scrape a state it could
not verify. Per-state failures are isolated; exit code is non-zero if any
attempted state failed.

Usage:
    python sync_ptv_trainings.py                 # all states in PULL_STATES
    python sync_ptv_trainings.py --states OH,PA  # subset override (ops / testing)
    python sync_ptv_trainings.py --list          # switch nothing; dump discovered
                                                 # trainings for the CURRENT state
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from ccef_connections import BigQueryConnector
from ptv_tools import ensure_session, open_authenticated_context

# Reuse ptv-tools' state constants + the exact DOM verification helpers so this
# sync's state-scoping cannot silently diverge from the package contract.
# (_scrape_state_links / _current_state_code are the same code ptv_tools.set_state
#  trusts to refuse an unverified switch.)
from ptv_tools.state import (  # noqa: E402
    ADMIN_URL,
    NAME_TO_CODE,
    SELECT_STATE_URL,
    _current_state_code,
    _scrape_state_links,
)


# -- Constants --------------------------------------------------------------

PROJECT = "proj-tmc-mem-com"
RAW_TABLE = f"{PROJECT}.ptv_raw_2026.training_signups"
DDL_PATH = os.path.join(os.path.dirname(__file__), "bq", "ptv_training_signups.sql")

BASE = "https://app.protectthevote.net"

# All 50 states + DC, same set as the API syncs. Trainings only exist in PTV
# program states; the rest scope-switch to an empty list (cheap) and write no
# rows, so a new program state lights up automatically. Codes here; resolved to
# PTV's numeric state_id via the select_state page at runtime.
PULL_STATES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]

INSERT_CHUNK = 500  # streaming-insert payload guard, mirrors sync_all_volunteers

logger = logging.getLogger(__name__)


# -- Coercion ---------------------------------------------------------------


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _to_date(v: Any) -> Optional[str]:
    """PTV 'YYYY-MM-DD' -> ISO date string for BQ, else None."""
    if not v:
        return None
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _to_time(v: Any) -> Optional[str]:
    """PTV 'HH:MM:SS' -> 'HH:MM:SS' for BQ TIME, else None."""
    if not v:
        return None
    s = str(v).strip()
    try:
        datetime.strptime(s, "%H:%M:%S")
        return s
    except ValueError:
        return None


def _to_datetime(v: Any) -> Optional[str]:
    """PTV 'YYYY-MM-DDTHH:MM:SS' (naive) -> ISO for BQ DATETIME, else None."""
    if not v:
        return None
    s = str(v).strip().rstrip("Z")
    try:
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        return None


# -- Scrape: navigation helpers ---------------------------------------------


def _goto(page, url: str) -> None:
    """Navigate tolerantly: LiveView keeps a websocket open, so networkidle can
    be flaky -- fall back to a short settle wait."""
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(700)


def switch_state(page, state_id: str, expected_code: str) -> str:
    """Switch server-side session context to a state and VERIFY it.

    Mirrors ptv_tools.set_state's contract: fire the LiveView set_state event,
    then read the live Download State User CSV href's state_code= and refuse to
    proceed unless it matches. Operates on the caller's long-lived context (no
    browser relaunch, no shared-storage-state churn). Raises on mismatch.
    """
    _goto(page, SELECT_STATE_URL)
    sel = f'a[phx-click="set_state"][phx-value-state_id="{state_id}"]'
    page.eval_on_selector(sel, "el => el.click()")
    page.wait_for_timeout(2500)  # let LiveView process + re-render

    _goto(page, ADMIN_URL)
    code = _current_state_code(page)
    if code is None:
        raise RuntimeError("could not read state_code after switch (unverified)")
    if code != expected_code:
        raise RuntimeError(
            f"state mismatch: asked for {expected_code}, session reports {code}"
        )
    return code


def build_state_index(page) -> Dict[str, Dict[str, str]]:
    """Scrape the select_state page once -> {code: {'state_id':..,'name':..}}."""
    _goto(page, SELECT_STATE_URL)
    links = _scrape_state_links(page)
    index: Dict[str, Dict[str, str]] = {}
    for link in links:
        code = NAME_TO_CODE.get((link["name"] or "").strip().lower())
        if code:
            index[code] = {"state_id": link["state_id"], "name": link["name"]}
    return index


# -- Scrape: page parsers (all read-only page.evaluate) ---------------------


def scrape_training_list(page) -> List[Dict[str, Any]]:
    """[{training_id, name, status, attending}] from /state_admin/trainings.

    Column order on the list: Name and type | Location | Owner | Date |
    Status | Attending | Actions.
    """
    _goto(page, f"{BASE}/state_admin/trainings")
    rows = page.evaluate(
        r"""() => {
            const out = [];
            document.querySelectorAll('table tbody tr').forEach(tr => {
                const cells = [...tr.querySelectorAll('td')];
                const link = tr.querySelector('a[href*="/trainings/"]');
                const href = link ? (link.getAttribute('href') || '') : '';
                const m = href.match(/\/trainings\/(\d+)/);
                if (!m) return;
                out.push({
                    training_id: m[1],
                    name: link ? link.innerText.trim().replace(/\s+/g,' ') : '',
                    status: cells[4] ? cells[4].innerText.trim() : '',
                    attending: cells[5] ? cells[5].innerText.trim() : '',
                });
            });
            return out;
        }"""
    )
    # Surface any pager control so a silently-truncated big state is visible.
    pager = page.evaluate(
        """() => !!document.querySelector('a[href*="page="], [phx-click*="paginate"], [phx-click*="page"]')"""
    )
    if pager:
        logger.warning(
            "[list] a pagination control is present on the trainings list -- "
            "verify no trainings were missed (pagination not yet handled)."
        )
    return rows


def scrape_training_show(page, training_id: str) -> Dict[str, Any]:
    """{role_id, host_email, quiz_link, schedule_ids[]} from the show page.

    schedule_ids come from the Schedule Manager's 'View Attendees' hrefs
    (/attendees/<sid>). dl metadata rows are label:value pairs.
    """
    _goto(page, f"{BASE}/state_admin/trainings/{training_id}")
    data = page.evaluate(
        r"""() => {
            const meta = {};
            document.querySelectorAll('dl dt').forEach(dt => {
                let dd = dt.nextElementSibling;
                while (dd && dd.tagName !== 'DD') dd = dd.nextElementSibling;
                const k = dt.innerText.trim().toLowerCase().replace(/\s+/g,' ');
                meta[k] = dd ? dd.innerText.trim() : '';
            });
            const sids = [...new Set(
                [...document.querySelectorAll('a[href*="/attendees/"]')]
                    .map(a => ((a.getAttribute('href')||'').match(/\/attendees\/(\d+)/)||[])[1])
                    .filter(Boolean)
            )];
            return { meta, schedule_ids: sids };
        }"""
    )
    meta = data.get("meta", {})
    return {
        "role_id": meta.get("role id"),
        "host_email": meta.get("host email") or None,
        "quiz_link": meta.get("quiz link") or None,
        "schedule_ids": data.get("schedule_ids", []),
    }


def scrape_roster(
    page, training_id: str, schedule_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """One dict per attendee <section> on a roster.

    Each attendee is a <section> holding <p>label: value</p> lines plus a
    cancel_registration control whose phx-value-id is the registration id.

    schedule_id set  -> per-session roster (/attendees/<sid>); rows carry a
                        session date + start/end time.
    schedule_id None -> the training-level roster (/attendees), used by
                        ON-DEMAND trainings that have no scheduled sessions;
                        those rows have no session date/time.
    """
    suffix = f"/attendees/{schedule_id}" if schedule_id else "/attendees"
    _goto(page, f"{BASE}/state_admin/trainings/{training_id}{suffix}")
    return page.evaluate(
        r"""() => {
            const secs = [...document.querySelectorAll('section')]
                .filter(s => s.querySelector('[phx-click="cancel_registration"]'));
            return secs.map(s => {
                const f = {};
                s.querySelectorAll('p').forEach(p => {
                    const t = (p.innerText || '').trim();
                    const i = t.indexOf(':');
                    if (i > 0) f[t.slice(0, i).trim().toLowerCase()] = t.slice(i + 1).trim();
                });
                const link = s.querySelector('[phx-click="cancel_registration"]');
                f['registration_id'] = link ? link.getAttribute('phx-value-id') : null;
                return f;
            });
        }"""
    )


# -- Assemble rows ----------------------------------------------------------


def build_rows_for_state(
    page, code: str, as_of_date: date, list_only: bool = False,
) -> List[Dict[str, Any]]:
    """Walk one (already-verified) state and return training_signups rows."""
    trainings = scrape_training_list(page)
    logger.info(f"[{code}] {len(trainings)} trainings on the list")
    if list_only:
        for t in trainings:
            logger.info(
                f"    training {t['training_id']}: {t['name']!r} "
                f"[{t['status']}] attending={t['attending']}"
            )
        return []

    rows: List[Dict[str, Any]] = []
    for t in trainings:
        tid = t["training_id"]
        try:
            show = scrape_training_show(page, tid)
        except Exception as e:
            logger.exception(f"[{code}] training {tid}: show-page scrape failed -- {e}")
            continue

        # Scheduled trainings expose attendees per session (/attendees/<sid>);
        # on-demand trainings have no sessions and list everyone at the
        # training-level roster (/attendees). schedule_id None takes that path.
        session_ids: List[Optional[str]] = list(show["schedule_ids"]) or [None]

        scraped = 0
        for sid in session_ids:
            try:
                roster = scrape_roster(page, tid, sid)
            except Exception as e:
                logger.exception(
                    f"[{code}] training {tid} session {sid}: roster scrape failed -- {e}"
                )
                continue
            for a in roster:
                status = (a.get("status") or "").strip().upper()
                scraped += 1
                rows.append(
                    {
                        "as_of_date": as_of_date.isoformat(),
                        "registration_id": _to_int(a.get("registration_id")),
                        "training_id": _to_int(tid),
                        "training_name": t.get("name") or None,
                        "training_status": t.get("status") or None,
                        "schedule_id": _to_int(sid),
                        "session_date": _to_date(a.get("date")),
                        "start_time": _to_time(a.get("start time")),
                        "end_time": _to_time(a.get("end time")),
                        "role_id": _to_int(show.get("role_id")),
                        "role": (a.get("role") or None),
                        "email": (a.get("email") or None),
                        "signup_date": _to_datetime(a.get("sign up date")),
                        "status": status or None,
                        "attended": (a.get("attended") or "").strip().upper() or None,
                        "host_email": show.get("host_email"),
                        "quiz_link": show.get("quiz_link"),
                        "state": code,
                    }
                )

        # Data-quality signal: the list "Attending" column is the training's
        # TOTAL registration count (ATTENDING + CANCELLED), so it should equal
        # the rows we scraped. A mismatch means a missed session or a truncated
        # (paginated?) roster -- worth surfacing, not silently trusting.
        listed = _to_int(t.get("attending"))
        if listed is not None and listed != scraped:
            logger.warning(
                f"[{code}] training {tid} ({t['name']!r}): list count={listed} "
                f"but scraped {scraped} rows across {len(session_ids)} roster(s) "
                f"-- possible missed/truncated roster."
            )
    return rows


# -- BigQuery ---------------------------------------------------------------


def ensure_table(bq: BigQueryConnector) -> None:
    existed = bq.table_exists(RAW_TABLE)
    with open(DDL_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    bq.query(ddl)  # CREATE TABLE IF NOT EXISTS -- idempotent, self-heals fresh env
    if not existed:
        # A just-created table isn't immediately available to the streaming
        # insert endpoint (BQ eventual consistency -> transient 404). Pause so
        # the first-ever run doesn't lean solely on the insert retry.
        logger.info("[BQ] table created; pausing for streaming-insert availability")
        time.sleep(8)
    logger.info(f"[BQ] ensured {RAW_TABLE}")


def _chunks(seq: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def write_state(
    bq: BigQueryConnector, code: str, rows: List[Dict[str, Any]], as_of_date: date,
) -> None:
    """Replace today's partition for this state, then append. Idempotent per
    state except inside BQ's ~90-min streaming-buffer window (see spec)."""
    delete_sql = (
        f"DELETE FROM `{RAW_TABLE}` "
        f"WHERE as_of_date = DATE '{as_of_date.isoformat()}' AND state = '{code}'"
    )
    try:
        bq.execute_dml(delete_sql)
    except Exception as e:
        logger.warning(f"[BQ] {code}: pre-delete failed (continuing): {e}")

    if not rows:
        logger.info(f"[BQ] {code}: 0 signup rows (nothing to insert)")
        return
    n = 0
    for chunk in _chunks(rows, INSERT_CHUNK):
        bq.insert_rows(RAW_TABLE, chunk)
        n += len(chunk)
    logger.info(f"[BQ] {code}: inserted {n} signup rows")


# -- Main -------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync PTV training signups (GUI) -> BigQuery.")
    p.add_argument(
        "--states",
        help="Comma-separated state codes instead of the full set (ops/testing), "
             "e.g. --states OH,PA",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Do not switch state or write anything; scrape and print the "
             "trainings list for whatever state the session is CURRENTLY on.",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    args = _parse_args(argv)

    # Verify the saved session (silent/headless when it's live), and if it has
    # died, run a HEADED re-login -- PTV's magic-code flow doesn't complete
    # reliably headless. headless=False only affects the login path; the verify
    # is headless internally, so no window appears in the common live-session
    # case. This makes an unattended local run (e.g. a 4 AM scheduled task)
    # self-heal its own auth by reading a fresh code from local Outlook -- the
    # thing a headless/remote job could never do, and why this stays local.
    ensure_session(headless=False)
    as_of_date = datetime.now(timezone.utc).date()
    failed: List[str] = []

    with sync_playwright() as pw:
        browser, context = open_authenticated_context(pw, headless=True)
        page = context.new_page()
        try:
            # --list: inspect the current state without touching session scope.
            if args.list:
                build_rows_for_state(page, "??", as_of_date, list_only=True)
                return 0

            states = (
                [s.strip().upper() for s in args.states.split(",") if s.strip()]
                if args.states
                else list(PULL_STATES)
            )
            logger.info(
                f"=== PTV trainings sync -- as_of_date={as_of_date} "
                f"states={len(states)} ==="
            )

            with BigQueryConnector() as bq:
                ensure_table(bq)

                index = build_state_index(page)
                logger.info(f"[states] {len(index)} states selectable for this admin")

                total_rows = 0
                for code in states:
                    if code not in index:
                        logger.warning(f"[{code}] not selectable for this admin -- skipping")
                        continue
                    try:
                        verified = switch_state(
                            page, index[code]["state_id"], expected_code=code
                        )
                        rows = build_rows_for_state(page, verified, as_of_date)
                        write_state(bq, verified, rows, as_of_date)
                        total_rows += len(rows)
                    except Exception as e:
                        logger.exception(f"[{code}] failed -- {e}")
                        failed.append(code)

                logger.info(
                    f"[summary] states_attempted={len(states)} "
                    f"rows_written={total_rows} failed_states={failed}"
                )
        finally:
            context.close()
            browser.close()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

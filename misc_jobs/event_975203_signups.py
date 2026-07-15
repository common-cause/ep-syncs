"""
Mobilize event 975203 signup roster -> Google Sheet (FL program).

Exports the full signup roster for Mobilize event 975203 (FL training series,
sessions Jul 22 - Aug 16 2026) into a Google Sheet in the shared Drive folder,
rebuilt from BigQuery on each run. Registered as a "weekly" job in
run_misc_jobs.py (Sunday-night Civis run).

Event-specific quirks:
- The event row is ABSENT from cln_mobilize__events (promoted/pulled-down
  pattern) -- the roster survives only as orphaned rows in
  cln_mobilize__participations, so this export is the durable record.
- UTM source codes (referrer__utm_*) are null on every signup: Mobilize masks
  referrer attribution for promoted events. The usable source signals are the
  custom "Organization" signup question (answered on all rows) and the plain
  web referrer URL (populated on a minority of rows).

Credentials come from the environment (BIGQUERY_CREDENTIALS_PASSWORD and
GOOGLE_SHEETS_CREDENTIALS_PASSWORD); run_misc_jobs.py loads .env before calling
run(). The GOOGLE_SHEETS credential is the sheets-controllers@ service account
-- for this export to work, that SA must have write access to FOLDER_ID.

Idempotent: the spreadsheet is looked up by title within FOLDER_ID and its
three job-owned tabs are cleared and rewritten every run. This job fully owns
the sheet's contents -- see the module note on filters/sorts below.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from typing import Any, List

from ccef_connections import BigQueryConnector, SheetsWriterConnector

logger = logging.getLogger(__name__)

BQ_PROJECT = "proj-tmc-mem-com"
# "FL Trainings" folder in the shared Drive (destination for the export).
FOLDER_ID = "1t3Bej7OypcIeKWicoy6oakHUblVx0LPe"
TITLE = "Mobilize Event 975203 Signups — FL Trainings Jul-Aug 2026"
EVENT_ID = 975203

# File-level shares, granted idempotently (never re-notifies on reruns).
# akeith@ is Amy (FL program), the sheet's owner-audience.
SHARE_WITH: List[str] = [
    "akeith@commoncause.org",
]

SQL = f"""
SELECT
  FORMAT_DATETIME('%Y-%m-%d %I:%M %p', DATETIME(p.utc_start_date, 'America/New_York')) AS session_et,
  p.timeslot_id,
  p.status,
  COALESCE(p.user__given_name,  p.given_name_at_signup)  AS first_name,
  COALESCE(p.user__family_name, p.family_name_at_signup) AS last_name,
  LOWER(TRIM(COALESCE(p.user__email_address, p.email_at_signup))) AS email,
  COALESCE(p.user__phone_number, p.phone_number_at_signup) AS phone,
  COALESCE(p.user__postal_code,  p.postal_code_at_signup)  AS zip,
  (SELECT JSON_VALUE(cf, '$.text_value')
   FROM UNNEST(JSON_QUERY_ARRAY(p.custom_field_values)) cf
   WHERE JSON_VALUE(cf, '$.custom_field_name') = 'Organization'
   LIMIT 1) AS organization,
  p.referrer__url AS web_referrer,
  FORMAT_DATETIME('%Y-%m-%d %I:%M %p', DATETIME(p.utc_created_date, 'America/New_York')) AS signed_up_et
FROM `proj-tmc-mem-com.mobilize_cleaned.cln_mobilize__participations` p
WHERE p.event_id = {EVENT_ID}
ORDER BY p.utc_start_date, last_name, first_name
"""

HEADERS = [
    "session_et", "timeslot_id", "status", "first_name", "last_name",
    "email", "phone", "zip", "organization", "web_referrer", "signed_up_et",
]


def _ensure_shares(ss: Any, emails: List[str]) -> None:
    """Grant writer access to any SHARE_WITH email not already on the file.

    Only adds missing permissions, so scheduled reruns never re-notify people
    who already have access.
    """
    if not emails:
        return
    existing = {
        (p.get("emailAddress") or "").lower() for p in ss.list_permissions()
    }
    for email in emails:
        if email.lower() not in existing:
            ss.share(email, perm_type="user", role="writer", notify=True)
            logger.info(f"shared '{ss.title}' with {email}")


def run() -> None:
    with BigQueryConnector(project_id=BQ_PROJECT) as bq:
        rows = list(bq.query(SQL))

    n = len(rows)
    n_cancelled = sum(1 for r in rows if r["status"] == "CANCELLED")
    n_people = len({r["email"] for r in rows if r["email"]})
    today = datetime.date.today().isoformat()

    data: List[List[Any]] = [HEADERS]
    for r in rows:
        data.append([r[h] if r[h] is not None else "" for h in HEADERS])

    # Per-session summary tab.
    sess: Counter = Counter()
    sess_cancelled: Counter = Counter()
    for r in rows:
        sess[r["session_et"]] += 1
        if r["status"] == "CANCELLED":
            sess_cancelled[r["session_et"]] += 1
    summary: List[List[Any]] = [["session (Eastern)", "signups", "cancelled", "active"]]
    for s in sorted(sess):
        summary.append([s, sess[s], sess_cancelled[s], sess[s] - sess_cancelled[s]])
    summary.append(["TOTAL", n, n_cancelled, n - n_cancelled])

    readme = [
        [f"Mobilize event 975203 — signup roster (mobilize.us/commoncause/event/{EVENT_ID}/)"],
        [f"Generated {today}  ·  Source: proj-tmc-mem-com.mobilize_cleaned  ·  Data Systems"],
        [""],
        ["Count", f"{n} signups ({n - n_cancelled} active, {n_cancelled} cancelled) by {n_people} distinct people."],
        ["Grain", "One row per signup (person x session). People registered for multiple sessions appear once per session."],
        ["Times", "All datetimes are US Eastern."],
        [""],
        ["Refresh", "This sheet is rebuilt automatically every Sunday night. Add your own notes on a SEPARATE tab —"],
        ["", "the Read me / Signups / By session tabs are overwritten on every refresh."],
        [""],
        ["Source codes", "UTM source codes are BLANK for every signup on this event — Mobilize withholds"],
        ["", "referrer attribution for promoted events. The available source signals are:"],
        ["  organization", "Answer to the 'Organization' question on the signup form (all rows)."],
        ["  web_referrer", "Referring web page where captured (a minority of rows)."],
        [""],
        ["Column notes"],
        ["  status", "REGISTERED / CONFIRMED = active RSVP; CANCELLED = withdrew."],
        ["  email/phone/zip", "Current Mobilize profile value, falling back to what they typed at signup."],
        ["  signed_up_et", "When the person registered."],
        [""],
        ["Caveat", "BQ mirror refreshes ~daily; roster is as of the generation date above. For day-of"],
        ["", "check-ins, pull the live list from the Mobilize dashboard."],
    ]

    with SheetsWriterConnector() as writer:
        ss = writer.get_or_create_spreadsheet(TITLE, folder_id=FOLDER_ID)

        tabs = [("Read me", readme), ("Signups", data), ("By session", summary)]
        for name, d in tabs:
            writer.write_worksheet(ss, name, d)
            writer.format_header_row(ss, name)
        writer.delete_worksheet_if_exists(ss, "Sheet1")
        ss.reorder_worksheets([ss.worksheet(name) for name, _ in tabs])

        _ensure_shares(ss, SHARE_WITH)

    logger.info(
        f"event {EVENT_ID}: rows={n} active={n - n_cancelled} "
        f"cancelled={n_cancelled} people={n_people}"
    )
    logger.info(f"event {EVENT_ID} sheet: {ss.url}")


if __name__ == "__main__":
    # Standalone dev entrypoint; the scheduled path is run_misc_jobs.py.
    import logging as _logging

    from dotenv import load_dotenv

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    run()

# PTV Trainings Sync — Spec

*Drafted 2026-07-24. Status: **built; full 50+DC run validated (2,603 rows /
19 states / 0 failures / 0 warnings, ~16 min); LIVE as a local Windows Task
Scheduler job** "EP PTV Trainings Sync", daily 4:00 AM ET, first fire
2026-07-25 (see §7). Not Civis.*
*Scope: **PTV admin GUI → BigQuery only.** No Airtable leg.*

A browser-automation sync that captures **training signup records** from the
PTV admin GUI — data PTV exposes through **no API**. It lands one row per
`(training session, attendee)` in `ptv_raw_2026.training_signups`, a daily
per-state snapshot partitioned by `as_of_date`, sibling to
`ptv_raw_2026.users` / `.shift_volunteers`.

## 1. Why this exists — the signal-quality problem

`ep_dashboards`' training layer currently reads PTV's flat `users_csv.training`
field: a single (or comma-joined) **string** per volunteer, **signup-only**,
with signup *dates* **inferred** from daily-snapshot detection ("on or before",
floored at 2026-07-02). That's a weak signal, and much of it is prior-cycle
strings.

The admin GUI's per-training attendee roster carries what the CSV cannot:

| Signal | `users_csv.training` (today) | This sync |
|---|---|---|
| Signup date | inferred "on or before", floored 2026-07-02 | **real PTV timestamp** (e.g. `2024-08-29T16:02:48`) |
| Multiple trainings per user | flattened into one string | **one row per signup** (incl. prior cycles) |
| Training identity | free-text name only | **PTV `training_id`** → keys straight into `ep_dashboards.training_event_map` (`source_system='ptv'`, `source_event_id=training_id`) |
| Registration status | absent | **ATTENDING / CANCELLED** (cancellations retained) |
| Attendance | absent | PTV `attended` flag (YES/NO; mostly unmarked, captured anyway) |
| Role granted | conflated in `users.role` | training-level `role_id` + per-signup role name |

## 2. The PTV GUI surface (reverse-engineered 2026-07-24)

All under the **state-scoped** `/state_admin/*` surface, so the sync iterates
states like the API syncs. Hierarchy: **state → training → session → attendees.**

| URL | Yields |
|---|---|
| `/state_admin/trainings` | training list: `training_id` (from `/trainings/<id>` href), name, status (Draft/Published), and an **"Attending" count = TOTAL registrations incl. cancelled** |
| `/state_admin/trainings/<tid>` | show page: `role_id`, host email, quiz link (a `<dl>`), + a Schedule Manager table whose "View Attendees" hrefs give each session's `schedule_id` |
| `/state_admin/trainings/<tid>/attendees/<sid>` | **per-session roster** — one `<section>` per attendee |
| `/state_admin/trainings/<tid>/attendees` | **training-level roster** — used by ON-DEMAND trainings (no sessions) |
| `/state_admin/trainings/<tid>/add_attendee...`, `.../publish`, `.../edit` | **MUTATING — never touched** |

**Attendee `<section>` shape** (labeled `<p>`s + a `cancel_registration`
control whose `phx-value-id` is the registration id):

```html
<section class="… border …">
  <p>sign up date: 2024-08-29T16:02:48</p>
  <p>status: ATTENDING</p>          <!-- or CANCELLED -->
  <p>attended: NO</p>
  <p>role: Poll Monitor</p>
  <p>email: someone@example.org</p>
  <p>date: 2024-09-18</p>           <!-- session date; ABSENT for on-demand -->
  <p>start time: 12:00:00</p>       <!-- ABSENT for on-demand -->
  <p>end time: 13:00:00</p>
  <a phx-click="cancel_registration" phx-value-id="1620"> cancel registration </a>
</section>
```

**Scheduled vs on-demand** (the one non-obvious branch):
- **Scheduled** training → attendees live per session at `/attendees/<sid>`;
  rows carry `session_date` + start/end time.
- **On-demand** training → **no** Schedule Manager rows; all attendees list at
  the **training-level** `/attendees` (no `sid`); rows have **null** session
  date/time. On-demand is not an edge case: OH's "On Demand Poll Monitor
  Training" had **159** signups (incl. 2026-dated ones).

## 3. Architecture

`sync_ptv_trainings.py` at the project root. Unlike the API syncs (one cheap
HTTP call per state), each state here needs an expensive **browser** state
switch, so the sync drives **one long-lived authenticated context** for the
whole run and switches state within it, rather than relaunching a browser per
state.

```
ptv-tools session (open_authenticated_context, headless)
  └─ for each state:
       switch_state()  ── verified via Download-State-User-CSV href state_code=  (refuses on mismatch)
       /state_admin/trainings ─▶ [training_id, name, status, count]
         └─ show page ─▶ role_id, host_email, quiz_link, [schedule_id…]
              └─ roster(s) ─▶ one row per attendee <section>
       write_state() ─▶ pre-delete today's (state) partition, then insert
```

Contracts honored:
- **State scoping is verified, never trusted.** `switch_state()` mirrors
  `ptv_tools.set_state`'s contract exactly — it fires the LiveView `set_state`
  event, then reads the live *Download State User CSV* href's `state_code=` and
  **refuses to scrape** unless it matches the requested state. It reuses
  `ptv_tools.state`'s `_scrape_state_links` / `_current_state_code` so the
  verification can't silently diverge. (PTV's current-state is shared,
  server-side session context — an unverified switch has real
  wipe/scrape-the-wrong-state potential.)
- **Read-only toward PTV.** Navigation + DOM reads only; no mutating control is
  ever clicked.
- **Per-state failure isolation**; exit code 1 if any attempted state failed.
- **Same-day idempotency**: per-state pre-delete of today's partition, then
  insert — subject to the BQ streaming-buffer caveat (§4).

`--states OH,PA` overrides the pull set for ops/testing; `--list` scrapes and
prints the current session state's training list without switching or writing.

## 4. BigQuery landing — `ptv_raw_2026.training_signups`

Full DDL: [`bq/ptv_training_signups.sql`](../bq/ptv_training_signups.sql)
(`CREATE TABLE IF NOT EXISTS`; the sync runs it at startup so a fresh Civis
container self-heals). Grain: **one row per registration** (`registration_id`
is PTV's stable per-signup id). Partitioned by `as_of_date`, clustered by
`state, email`.

Columns: `as_of_date, registration_id, training_id, training_name,
training_status, schedule_id, session_date, start_time, end_time, role_id,
role, email, signup_date, status, attended, host_email, quiz_link, state`.

Fabricated example rows (real emails live only in BQ — never in git):

| as_of_date | registration_id | training_id | training_name | schedule_id | session_date | signup_date | status | email | state |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-24 | 1620 | 94 | Ohio Poll Monitor Training | 141 | 2024-10-23 | 2024-08-29T16:02:48 | ATTENDING | jane@example.org | OH |
| 2026-07-24 | 4477 | 161 | On Demand Poll Monitor Training | *(null)* | *(null)* | 2026-04-20T19:27:13 | CANCELLED | sam@example.net | OH |

**Typing:** `signup_date` is **DATETIME** — PTV shows a naive wall-clock with no
timezone, so we store it lossless and assert no tz (interpret downstream).
`session_date` DATE, `start_time`/`end_time` TIME. Unparseable/blank → NULL.

**Dedupe (consumers MUST):** same-day idempotency relies on the pre-delete, but
BQ's streaming buffer blocks DELETE on rows streamed in the last ~90 min, so a
same-day rerun *can* leave exact-duplicate rows. Dedupe on
`(as_of_date, registration_id)`:

```sql
SELECT * EXCEPT(rn) FROM (
  SELECT t.*, ROW_NUMBER() OVER (
           PARTITION BY as_of_date, registration_id ORDER BY as_of_date) AS rn
  FROM `proj-tmc-mem-com.ptv_raw_2026.training_signups` t
) WHERE rn = 1;
```

A `v_training_signups_current` convenience view (latest snapshot per state,
deduped) is a natural follow-up but not required for the sync to run.

## 5. Validation (2026-07-24, local)

**Full 50-state + DC run:** 51 states attempted, **2,603 signup rows across 19
states with data, 0 failed states, exit 0**, in **~16 min** wall-clock. **Zero
reconciliation warnings** — every training in every state scraped exactly its
expected row count. Largest: MA 502, OH 396, MO 363, AZ 211, GA 174, NE 160.

Per-state deep check (OH, NE):

| State | rows | trainings | distinct regs | null signup_date | blank email | signup range |
|---|---|---|---|---|---|---|
| OH | 396 | 7 | 396 | 0 | 0 | 2024-08-21 → 2026-04-20 |
| NE | 160 | 9 | 160 | 0 | 0 | 2024-10-23 → 2026-07-23 |

- **Reconciliation holds:** scraped rows == the list "Attending" (total-reg)
  count for **every** training (the sync warns on any mismatch — a missed or
  truncated roster). No warnings fired across all 51 states.
- **On-demand matters:** ~1,400 of the 2,603 rows are on-demand signups (GA and
  MD are 100% on-demand; MO 224, OH 168) — silently dropped by the pre-fix
  version. Captured with null session dates; scheduled sessions carry dates.
- `distinct_regs == rows` (no dup registration ids); 100% non-null
  `signup_date`; 0 blank emails.
- Live signal confirmed: NE's latest signup was **the day before** the run.
- No states were unselectable for the super-admin account; no pagination
  controls tripped (largest single insert MA=502, chunked at 500/request).

## 6. Feeds `ep_dashboards` (downstream, out of scope here)

`training_id` is `ep_dashboards.training_event_map`'s `source_event_id` for
`source_system='ptv'`. Consuming this table lets the dashboards' Training layer
replace inferred signup dates with **real timestamps**, count every signup
(incl. prior cycles), and per-event map PTV trainings to cycle/verdict. Wiring
that up is an **ep-dashboards** work item, not this sync's.

## 7. Go-live — LOCAL scheduled task (LIVE 2026-07-24)

**This runs LOCALLY, not in Civis.** The browser automation and the Outlook
magic-code login are inherently local (Windows + a running Outlook profile); a
headless/remote job fundamentally cannot self-authenticate to PTV. Registered as
a Windows Task Scheduler job — same pattern as ep-dashboards' local scheduled
agent. No Civis job, no `civis/*.sh` entrypoint, no cross-machine state shipping.

- [x] **Wrapper:** `scripts/run_ptv_trainings.ps1` — runs the ep-syncs venv
      Python on `sync_ptv_trainings.py`, forces UTF-8 I/O, logs a dated file
      under `logs/` (gitignored), prunes logs >30 days. Pure ASCII (PS 5.1
      BOM-less trap). Validated end-to-end via the exact scheduler command.
- [x] **Registered:** task **"EP PTV Trainings Sync"**, `-Daily -At 4:00AM`
      (ET), `-StartWhenAvailable -WakeToRun`, 1 h `-ExecutionTimeLimit`,
      principal UserId=RobKerth / LogonType=**Interactive** / RunLevel=Limited
      (per-user, no admin). First fire **2026-07-25 04:00 ET**, daily after.
      Runtime ~16 min. To remove: `Unregister-ScheduledTask -TaskName 'EP PTV
      Trainings Sync'`.
- [x] **Auth self-heal built in:** `ensure_session(headless=False)` verifies
      headless (silent when live) and opens a headed Outlook relogin *only if
      the session died*. Nothing to provision. Interactive logon is required
      precisely so that relogin can reach a desktop + Outlook.
- [ ] **Env precondition (the real dependency, Rob to confirm):** the machine is
      **awake, logged in, Outlook running** at 4 AM. Interactive-token tasks
      skip if logged out; a locked session is fine. Confirm sleep/wake behavior.
- [ ] **First-fire watch:** the 2026-07-25 log settles PTV's server-side session
      TTL — either "session valid" (persisted overnight) or an auto-relogin.
      Session observed valid ~7.5 h after login (08:18 → 15:46) with no relogin;
      overnight is the open span.
- [x] `.env` carries `PTV_ADMIN_EMAIL` + `BIGQUERY_CREDENTIALS`. No
      `PTV_API_KEY` needed (browser, not API).

## 8. Gotchas / open items

- **Pagination not yet handled** — but the full 50+DC run tripped **no** pager
  and produced **no** reconciliation mismatch (scraped rows == list count for
  every training, largest roster-bearing state MA), so no state currently
  paginates. The sync still **warns** if a pagination control appears or counts
  diverge; add paging only if that fires as rosters grow into 2026.
- **Fresh-table streaming lag.** Right after table creation, the streaming
  endpoint can 404 briefly; handled by an 8 s post-create pause + the
  connector's insert retry. Only relevant on the first-ever run.
- **`attended` is nearly always NO/unmarked** in PTV (hosts don't mark it) —
  attendance truth still comes from Mobilize; captured here only for
  completeness.
- **Row-level PII** (`email`): access-controlled BQ only, never git.

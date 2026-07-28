# NM (CCNM) Asana → BigQuery Sync — Recon + Design

*Recon run 2026-07-25 against the live workspace using `AsanaConnector`
(ccef-connections v0.5.0) and `ASANA_API_KEY_PASSWORD` from `.env`.*

**Verdict: build it, in two phases. Phase 1 now.**

NM is not a PTV state. PTV is a tool we make available, not a requirement, and
NM has no registered Airtable base either (enabled Airtable states are MD, MI,
NE, NY, OR, PA, UT, WI). **NM therefore uses neither of the two toolsets that
feed `ep_2026_cleaned`, and ~40 tracked NM EP volunteers are currently
invisible to every consumer of the interface layer.** Absorbing a state's own
tooling into the canonical contracts is precisely what that layer is for, and
this board is case #1 of the pattern.

Phase 1 captures the board nightly and lands the 33 volunteers who have an
email into the existing `volunteers` contract. Phase 2 — after NM answers §7 —
lifts coverage to 41/41 and adds pipeline-stage events. Phase 1 does not block
on NM, and starting it now is the only way we ever accumulate stage-transition
history, which Asana does not expose retroactively.

---

## 1. Access: what the token can see

The PAT in `.env` is **Rob's own personal token** (`GET /users/me` →
`Rob Kerth`, gid `1202647507907665`). It sees one workspace,
`commoncause.org` (gid `8446703955071`), and **25 active + 3 archived
projects** — the whole national org, not just EP.

Access is therefore inherited from Rob's account membership, so any NM project
Rob is *not* a member of is invisible to the sync and would fail as "project
not found" rather than loudly. See Q10.

The connector itself is built exactly to the mailroomed spec — Bearer auth,
`{"data": …}` unwrapping, offset pagination with a forced `limit`, 402 mapped
to a clear paid-tier error. No changes needed for this sync.

## 2. What NM actually built

**`EP Volunteer Onboarding Kanban`** — gid `1216633527817242`, team **CCNM**,
owner **Mason Graham**, created 2026-07-16, **modified the morning of the
recon**, so it is in live daily use.

| Property | Value |
|---|---|
| Tasks | **41** (40 distinct names — one name appears twice) |
| Sections (the pipeline) | `Sign Up` (20) → `Training Completed` (11) → `Shifted` (10) → `Deployed` (0) |
| Custom fields | **none** |
| Assignees | Mason Graham (30), Cesar Marquez (11) — all 41 assigned |
| Tags | `jotform` (18), `indivisible` (15), `PTV` (6), untagged (2) |
| `completed` flag | set on only 2 of 41 |
| Subtasks | none |

**The grain is one task per volunteer.** The task *name* is the volunteer's
full name; the volunteer's **email address is pasted as the entire task
`notes` body** (33 of 41). Nothing else is populated — no phone, no county,
no ZIP, no state, no role.

Tags line up almost perfectly with sections, so they read as *recruitment
source*, not workflow state:

```
Sign Up            + jotform      18     ← web form signups
Training Completed + indivisible  11     ← partner list, imported already-trained
Shifted            + indivisible   4
Shifted            + PTV           6     ← the incidental PTV overlap
Sign Up            + (none)        2
```

There is also an **archived first attempt** — `Kanban board` (gid
`1216573686848135`, also CCNM/Mason Graham), sections
`New Signup / Completed / Shifted / Deployed / Paused`, a `KANB` text field
and `Story points`, holding 6 rows of `Test 4` / `Test 5` / `Test Thursday`
scaffolding. Dead; ignore it. The live board is the successor.

### Two other EP projects exist and are *not* the NM board

- **`Election Protection 2026 Campaign`** (gid `1216396602015622`, owner Mason
  Graham) — a 21-task national timeline still carrying its template rows
  (`[READ ME] Instructions for using this template`, `[EXAMPLE TASK] …`).
  Sections are workplan areas (Volunteer Goals, Volunteer Trainings, EP
  Coalition, Boiler Room/Crisis Response…). Tasks are **milestones**
  (`100 Volunteers Shifted`, `250 Volunteers`, `Election Day`), not people.
  Untouched since 2026-07-10. A **goal/target table**, not a volunteer feed —
  useful later for goal-vs-actual, but a different sync. See Q12.
- **`NE: Election Protection`** (gid `1214472832039298`, owner Gavin Geis) —
  Nebraska, 6 project-management tasks. Not volunteer data.

> Note for the record: prior project memory had New Mexico's lead as Cesar
> Marquez. Both Cesar and Mason are working the board, but **Mason owns every
> CCNM Asana project** — so Mason is the field-contract counterpart.

## 3. How this board relates to PTV (not a reconciliation problem)

Measured against the current `ptv_raw_2026.users` snapshot (NM roster: 1,515
PTV registrants, all with email):

| Test | Result |
|---|---|
| Kanban emails parsed from notes | 33 of 41 |
| …matching the current PTV **NM** roster | 5 |
| …matching PTV in **any** state | 5 |
| …absent from PTV entirely | **28** |
| Kanban *names* matching the PTV NM roster | 5 (10 across all states) |
| Kanban emails appearing in `shift_volunteers` (NM) | 1 |

Name-matching does no better than email-matching, so this is not a key-quality
problem — **these are genuinely different people from PTV's NM registrants**,
recruited through Jotform and an Indivisible partner list and tracked only in
Asana. That is the expected shape for a state that isn't using PTV, and it is
the argument for the sync: this is information that exists nowhere else in our
data.

The ~5-record overlap is incidental (most likely the 6 tasks tagged `PTV`,
hand-copied from a PTV list) and **needs no new dedupe logic**. The
`volunteers` view already resolves exactly this case for Airtable self-adds
with a `NOT EXISTS` guard against the PTV snapshots (`40_volunteers.sql:170`);
the Asana branch reuses it, so a person in both systems lands once, on the PTV
branch. A person who later registers in PTV flips branches on the next
capture — same as a self-add does today.

The one thing that *is* a defect: **`Shifted` disagrees with PTV.** Ten tasks
sit in `Shifted`; PTV can confirm a shift for one. That is not a data-quality
failure so much as an unknown definition — see Q2. It matters because it
determines which contract the stage maps onto, and specifically whether it
may touch `shift_signups` (§5.3: it may not).

## 4. The real constraint: the interface layer is keyed on email

`ep_2026_cleaned.volunteers` has grain **(state, email)**, and its Airtable
self-add branch ends with `WHERE sv.email IS NOT NULL`
(`40_volunteers.sql:169`) — **rows with no email are silently dropped.** Any
Asana branch inherits that, because email *is* the person's identity
throughout the layer.

Consequences, in priority order:

1. **8 tasks have no email — and all 8 are in `Shifted`**, the furthest-along
   stage. Those people cannot enter the roster at all. The most operationally
   interesting records on the board are the ones the layer cannot see. This is
   the single highest-value fix and the substance of Q1/Q2.
2. **Email lives in free text.** A regex over `notes` is the only way to get
   the key. It degrades silently the moment someone types a note above the
   address, pastes two addresses, or writes "email: x@y.com". Phase 1 carries
   `notes_had_email` and `notes_residual` so drift is visible in data rather
   than discovered months later.
3. **Names arrive as one string.** `volunteers` exposes `first_name` /
   `last_name` separately; the board has only a full-name task title. A
   naive split mishandles multi-word given names, particles, and suffixes
   (`Theresa Hadden-Martinez` splits fine; `Mary Jo Van Dyke` does not). See
   Q4.
4. **No phone, county, or ZIP.** These NULL-pad, with precedent — the self-add
   branch already NULL-pads `role`, `source_code`, and `training`.
5. **No state on the task.** Already solved by house convention: *"`state` on
   Airtable-derived rows comes from the base registry, never from record
   fields"* (`docs/ep_2026_cleaned_spec.md`). The Asana registry row supplies
   `NM`. Not a blocker — but worth raising if the board might ever hold
   non-NM people (Q3).

## 5. Design

Small enough to be a **misc-jobs task**, not its own Civis job — one module
under `misc_jobs/`, one `JOBS` row, one `misc_jobs_schedule.yaml` entry
(`days: daily`), riding the existing nightly 3 AM ET runner. No new Civis
surface, no new credential beyond `ASANA_API_KEY_PASSWORD`.

### 5.1 Raw landing

`asana_raw_2026.ep_kanban_tasks` — new dataset, snapshot-append partitioned on
`as_of_date`, same idempotency model as `ptv_raw_2026` (pre-delete today's
partition for the captured projects, then insert), so a same-day rerun
collapses rather than doubles.

| Column | Type | Source |
|---|---|---|
| `as_of_date` | DATE (partition) | run date |
| `task_gid` | STRING | `gid` — the only stable identity |
| `project_gid`, `project_name` | STRING | registry row |
| `state` | STRING | **registry row**, per house convention |
| `task_name` | STRING | `name` (volunteer full name today) |
| `stage` | STRING | `memberships[project].section.name` |
| `stage_order` | INT64 | registry-declared rank, so `Sign Up < Training Completed < Shifted < Deployed` sorts correctly downstream |
| `source_tags` | STRING | tags joined, sorted (`indivisible`, `jotform`, `PTV`) |
| `parsed_email` | STRING | first email regex-matched in `notes`, then `norm_email` |
| `notes_had_email` | BOOL | data-quality signal (§4.2) |
| `notes_residual` | STRING | `notes` minus the email; non-empty ⇒ convention drift |
| `assignee_name`, `assignee_email` | STRING | `assignee.*` — CC staff, **not** volunteers |
| `completed`, `completed_at` | BOOL, TIMESTAMP | as-is |
| `due_on`, `start_on` | DATE | as-is |
| `created_at`, `modified_at` | TIMESTAMP | as-is |
| `custom_fields_json` | JSON | full `custom_fields` verbatim — empty today, absorbs the answer to Q1 with no migration |
| `permalink_url` | STRING | click-through for ops |

Plus `asana_raw_2026.projects` — a thin daily snapshot of project + section +
custom-field-setting metadata, so a renamed section or a newly added custom
field shows up in history instead of silently changing `stage` values.

### 5.2 Registry

`ep.asana_sync_sources`, mirroring `airtable_sync_sources` /
`shift_volunteer_sync_targets`: one enabled row per board, carrying
`project_gid`, `state`, the declared stage order, and a `canonical_overrides`
JSON escape hatch. Inserting a row starts capturing a board — so the second
state to stand one up is a registry insert, not a code change.

### 5.3 Into the interface layer

**`ep_2026_cleaned.asana_pipeline`** (new, `35_asana_pipeline.sql`) — current
snapshot, one row per board volunteer: normalized `email`, `email_raw`,
`full_name`, `state`, `stage`, `stage_order`, `source_tags`,
`notes_had_email`, `task_gid`, `permalink_url`, `created_at`, `modified_at`.
The Asana analogue of the generated `shifted_volunteers` view.

**`40_volunteers.sql`** — a **third UNION branch**, `source_system = 'asana'`,
`in_ptv = FALSE`, guarded by the existing `NOT EXISTS` against PTV snapshots
*plus* a matching guard against the Airtable self-add branch, so the
(state, email) grain holds if a state ever runs both. (No collision today —
NM has no Airtable base — but the guard is the point of the exercise.)
`is_active` = present in the latest snapshot. `joined_at` = task `created_at`,
which is board-entry, not true signup date — document that in the view
description so nobody reads it as recruitment timing.

**`50_volunteer_activity.sql`** — stage events (`asana_signed_up`,
`asana_training_completed`, `asana_shifted`, `asana_deployed`), source_system
`asana`, `source_ref` = `task_gid`.

**`shift_signups` gets nothing.** NM's `Shifted` carries no shift detail — no
date, no location, no role — and `shift_signups` is the view whose numbers
agree with PTV by construction (`volunteers.shift_count` derives from it, and
V2 asserts the `in_ptv` roster matches `v_users_current` exactly). Injecting a
self-reported stage with no event detail would corrupt that. Stage lives in
`asana_pipeline` and `volunteer_activity`; `shift_signups` stays PTV-grained.

**Verification.** V2 is unaffected (Asana rows are `in_ptv = FALSE`). V1 grain
needs the new cross-source exclusion to hold. Add **V8**: every
`asana_pipeline` row with `notes_had_email` appears in `volunteers`, and the
count of dropped rows equals the email-less count — so coverage loss is
asserted, never silent.

### 5.4 Flow

```python
from ccef_connections import AsanaConnector, BigQueryConnector

with AsanaConnector() as asana:
    for src in enabled_registry_rows():           # ep.asana_sync_sources
        proj  = asana.get_project(src.project_gid, opt_fields=PROJECT_FIELDS)
        secs  = asana.get_sections(src.project_gid)
        tasks = asana.get_project_tasks(src.project_gid)   # DEFAULT_TASK_FIELDS
        rows += [flatten(t, src) for t in tasks]
# pre-delete today's partition for the captured projects, then insert
```

Full snapshot every run — 41 rows makes `modified_since` pointless, and a full
pull keeps deletions visible as absence. **Read-only toward Asana, always:**
nothing in EP should write back to a board a state team maintains by hand.

Stage transitions come from `LAG(stage) OVER (PARTITION BY task_gid ORDER BY
as_of_date)` across snapshots — cheap, no Stories API, but **only from go-live
forward**, which is the argument for starting Phase 1 now. True historical
signup→trained→shifted timestamps would need the Stories API (new connector
scope) and shouldn't be promised without confirming it's wanted.

### 5.5 Effort

~150 lines + DDL + registry seed + two view edits. The connector is done.

## 6. The generalizable part

NM is the first state whose tooling we don't provide, and it won't be the last.
The minimum contract for any such tool to enter `ep_2026_cleaned`:

1. **A normalized email per person** — the layer's primary key. Without it the
   person does not exist downstream, no matter how good the rest of the record.
2. **A state**, supplied by *our* registry rather than trusted from the tool.
3. **A stable per-record id** for snapshot diffing (`task_gid` here).
4. **A stage or status**, plus a declared ordering, so it maps onto pipeline
   reporting.
5. **A source/attribution value**, for recruitment-channel rollups.
6. Ideally **split first/last names**; a single display-name string is lossy.

Everything else NULL-pads with precedent. Worth reusing as the intake
checklist next time a state says "we're tracking this in <tool>."

---

## 7. Questions for NM (Mason Graham, cc Cesar Marquez)

Framing for the conversation — and it matters that it's this framing, not a
nudge toward PTV: *you're tracking EP volunteers in a system we don't
provide, which is fine and expected. We can pull the board into BigQuery
nightly so NM's volunteers show up in national EP reporting alongside
everyone else. A couple of small conventions on the board make that work
reliably and keep working without anyone thinking about it.*

**Blocking (these change the table contract, not just the parsing)**

1. **Can the volunteer's email move from the task notes into a real custom
   field?** It's currently the note body. A single `Email` text field makes it
   structured and survives someone adding a note above it. Our roster is keyed
   on email, so this is the one change that most affects what we can do.
   (We'll parse notes as a fallback for existing rows regardless.)
2. **The 8 tasks with no email are all in `Shifted` — can those be
   backfilled?** Those are your furthest-along volunteers and the ones our
   roster currently can't include at all. Relatedly: **what does `Shifted`
   mean on this board?** Has the person claimed a specific shift, agreed to
   take one, or something else? We won't map it onto national shift numbers
   until we know, because those are defined as claimed PTV shifts and we don't
   want to blur the two.
3. **Is one task = one volunteer permanent?** Everything downstream assumes
   it. If tasks might later become recruitment activities or training
   sessions, better to know now.

**Data model**

4. **First and last name in separate fields, or is splitting the task title
   acceptable?** We can split on the space, but it will mangle multi-word
   given names and suffixes. Low stakes, easy to get right up front.
5. **`Deployed` is empty and `completed` is checked on only 2 of 41 tasks —
   what should each mean?** Concretely: when a volunteer is fully deployed, do
   you move the card to `Deployed`, check the task complete, or both? We can
   report on either convention, just not on an ambiguous mix.
6. **Are the tags (`jotform`, `indivisible`, `PTV`) the recruitment source?**
   That's how they read, and they're genuinely useful as attribution. If so:
   is the list closed, or do new partners get new tags? And would source be
   better as an enum custom field — tags are easy to forget when hand-creating
   a card.
7. **One name appears on two tasks** — two different people, or an accidental
   duplicate? Determines whether we dedupe.
8. **Should the board carry a `State` field?** We'll set NM from our own
   registry, which is how we handle this for every other state's tooling, so
   this is only worth doing if the board might ever hold non-NM people.

**Operations**

9. **What's creating the cards?** A batch of tasks was modified around 6:00 AM
   on the recon day, which looks automated — a Jotform→Asana integration, an
   Asana rule, or someone doing a morning pass? **If Jotform is the real
   upstream source of truth, we may be able to read it directly** and skip the
   fragile note-parsing entirely. That would be a better answer than Q1.
10. **Should the sync run on a dedicated account's token rather than Rob's?**
    Today it would use Rob's PAT, which can see all 25 org projects and breaks
    if Rob loses access to the board. Asana service accounts are
    Enterprise-only, so the options are a shared sync user or accepting a
    staffer's PAT. Either works — worth a decision rather than a default.
11. **What do you want back?** Nightly capture gives us pipeline counts by
    stage over time, source attribution, and NM volunteers appearing in
    national EP reporting. Is there a number or dashboard you're currently
    assembling by hand that we should just produce?

**Separately**

12. **`Election Protection 2026 Campaign` still has its template rows**
    (`[READ ME] Instructions for using this template`, two `[EXAMPLE]` tasks)
    and hasn't been touched since 2026-07-10. Is it live? Its `Volunteer
    Goals` section holds real-looking targets (50/100/150/200/250 volunteers
    with dates, plus `100/150/200 Volunteers Shifted`) that we could sync as a
    goal-vs-actual table — but only if those are real commitments rather than
    template leftovers.

---

## Appendix — reproducing the recon

Recon scripts were scratch-only (they print live volunteer data and are not
committed). To re-derive:

```python
from ccef_connections import AsanaConnector
with AsanaConnector() as a:
    ws = a.get_workspaces()                                    # → commoncause.org
    a.get_projects(ws[0]["gid"], archived=False)               # → 25 projects
    a.get_project("1216633527817242", opt_fields=(
        "name,team.name,owner.name,created_at,modified_at,"
        "custom_field_settings.custom_field.name,"
        "custom_field_settings.custom_field.type"))
    a.get_sections("1216633527817242")
    a.get_project_tasks("1216633527817242")                    # 41 tasks
```

`load_dotenv()` resolves `.env` from the *calling file's* directory — a script
run from outside the repo must pass the path explicitly
(`load_dotenv("…/ep-syncs/.env")`) or `CredentialManager` reports
`ASANA_API_KEY_PASSWORD` missing even though it's seeded.

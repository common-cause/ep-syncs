# EP Hub Host Tracker sync — design

Built 2026-08-21 for MI. Code: `misc_jobs/hub_host_tracker.py`. Registry:
`bq/hub_host_trackers.sql`. Runs as a `hub_host_tracker` task on the nightly
`run_misc_jobs.py` runner (daily).

## 1. The program problem

Some states don't mail every EP volunteer kit from the state office. They
distribute through regional **hub hosts**: a host takes a box of lanyards,
vests, car magnets, guides and swag, and hands kits to the volunteers assigned
to them. MI is the 2026 case, working out of the `EP Hub Host Tracker`
spreadsheet (owner: Shan Abbott, first host: Kevin Fisher).

Before this sync, the whole thing was manual — no formulas anywhere in the file.
Staff retyped volunteer details onto each host's tab from Airtable. Three
things were asked for:

1. every quiz completer on a shared landing page, kept current;
2. a dropdown of *valid* hosts to assign them with;
3. formulas in `TEMPLATE` so that cloning it and naming a host auto-builds that
   host's distribution list.

## 2. Tab ownership — the load-bearing decision

| Tab | Owner | Written when |
|---|---|---|
| `_data` (hidden) | the job | every night |
| `Volunteer Landing Page` | **shared** — A..Q is a mirror, R+ is human | mirror seeded once; dropdown re-applied nightly |
| `Hosts` | program staff | **never** (read-only; headers ensured by scaffolding) |
| `TEMPLATE` | program staff | scaffolding only, and only its formula cells |
| per-host tabs (`KEVIN Fisher`, …) | program staff | scaffolding only |
| `README (sync)` | the job | every night |

`Volunteer Landing Page!A1` holds exactly one array formula, `={_data!B:R}`, so
columns A..Q are a live mirror of the job-owned hidden tab. Column R
(`Assigned Host`) is a validated dropdown and is **human-owned — the job never
writes a value into it.** S onward is free space.

### Why this beats a keyed per-column update

MI's follow-up ask was to flag shifted status on the landing page and pull it
into the host lists, noting it "will require key based update of that single
column." With the mirror it does not, and that is the better outcome:

- **Nothing to clobber.** The whole A..Q block is one formula over a tab only
  the job touches. `Shifted?` — and every other field — refreshes nightly for
  free. A keyed update would have to read the sheet back, match on email, and
  write into a grid humans also edit.
- **No silent match failures.** Keyed-on-email updates fail quietly when an
  email is retyped, case-shifted, or duplicated. (This repo has already been
  bitten: the Airtable leg of `sync_shift_volunteers.py` matches
  case-sensitively, and MI alone has 9 duplicate-email groups.) There is no
  match step here at all.

The one thing that *would* force a keyed update is staff hand-adding rows to
the landing page — walk-ups, non-quiz-takers. Rob ruled that out (2026-08-21):
every row comes from a quiz. **If that ever changes, the mirror has to go and
this becomes a read-back-and-merge job.** Don't half-migrate it.

## 3. The row ledger (`_data` column A)

Host assignments live in column R, aligned to the mirrored rows **by row
position**. So a row vanishing from the middle of the extract would shift every
row below it up by one and silently reassign those volunteers to the wrong
hosts. This is not hypothetical: `ep_2026_raw.*__quiz_responses` is rebuilt
from scratch every night, so an Airtable record deleted today is gone from the
extract tomorrow.

Sorting the query deterministically is not enough. Instead:

- `_data` column A holds a stable per-volunteer key — their **earliest** quiz
  record id, so a retake can never move a row.
- The job reads the previous `_data` back **before** writing it. Rows keep the
  order they already had; genuinely new volunteers append at the bottom.
- A volunteer whose quiz record vanished is **carried forward** with their
  last-known values and flagged `No -- record deleted` in `In Quiz Base?`, and
  the run logs a warning.

Rows are never removed and never reordered. The sheet is its own append-only
ledger, which also means the contract survives a registry edit, a
re-registered quiz base, or a rebuild of the cleaned views.

`_read_prior_data` **refuses to write** if `_data!A1` isn't the expected ledger
header. Without the ledger the order — and every assignment aligned to it —
can't be preserved, so a half-recognised tab is a hard stop, not a fresh start.

This is the same protective instinct as `sync_volunteer_sheets.py`'s
row-stability contract, but stronger: that one relies on PTV ids increasing
monotonically, which only holds because PTV never deletes. Airtable does.

## 4. Grain: one row per volunteer, not per submission

A retake, or someone who passed both the Poll Monitor and Rover quizzes, is
**one row** with both role labels joined into `Role(s)`. Two rows would mean two
physical kits. Details come from the latest submission; the ledger key and sort
position from the first.

A blank email can't identify a person, so those rows stay per-record
(`person_key` falls back to `rec:<record_id>`) rather than collapsing into each
other.

## 5. Column layout

`DATA_COLUMNS` in the module is the single source of truth; the landing-page
geometry is derived from its length so the two can't drift.

```
_data   A  _key              <- ledger, NOT mirrored
        B  Volunteer Name  -\
        C  Location         |
        D  Contact email    |   the 7 columns that flow to host tabs.
        E  Contact Phone    |-  Order MUST match TEMPLATE!G6:M6 --
        F  Role(s)          |   the host formula is ONE FILTER over a
        G  Shifted?         |   contiguous range.
        H  Requested items?-/
        I..R  Quiz, Score, Submitted, Shifts, First Shift, Latest Shift,
              County, Zip, Ever Shifted?, In Quiz Base?

landing A..Q  = mirror of _data!B:R
        R     = Assigned Host   (human, dropdown)
        S+    = Notes / free space
```

`Location` is the **full mailing address, verbatim** from the quiz (Rob,
2026-08-21) — hosts mail kits (the checklist has a `MAIL ONLY / Return label`
line), and parsing a city out of free-text addresses that are inconsistently
comma-formatted would invent errors for no gain.

`Requested items?` flattens the quiz's `toolkit_identifier_request` JSON array
into one packing string, inlining the vest size (`L/XL Vest`) and shortening the
pick-up marker (`Yard sign (pickup)`), matching the shape staff were typing by
hand.

## 6. Inclusion rule

**Anyone who submitted a quiz**, not just passers (Rob, 2026-08-21). `Score` is
shown (`6/7`, `7/7`) so staff can see who hasn't passed. 40 MI volunteers as of
2026-08-21; exactly one at 6/7.

## 7. `Shifted?` and the primary → general wipe

`Shifted?` reflects **current** PTV shift signups
(`ep_2026_cleaned.volunteers.shift_count > 0`). PTV, not the Airtable
`Shifted Volunteers` table, per Rob 2026-08-21 — "we're about to wipe and do the
general." Two consequences designed for:

- When MI wipes its primary shifts, `Shifted?` legitimately drops to `No` for
  everyone and refills as general shifts are claimed. **That is expected, not a
  bug** — it's called out in the README tab so nobody reports it as one.
- So the page also carries **`Ever Shifted?`**, latched from the all-time daily
  snapshots in `ptv_raw_2026.shift_volunteers`, which survive the wipe. Only
  `Shifted?` flows to host tabs; `Ever Shifted?` is context for whoever assigns.

The latch already earns its place: on 2026-08-21, 27 volunteers were currently
shifted and 28 had ever been — one had dropped a shift.

Note the MI Airtable base is registered as *"MI Primary 2026 Field Report"*. If
the general gets a **new** base, the Airtable route would have broken here
anyway; PTV is the durable choice.

## 8. Host tab formulas

`TEMPLATE!A1` is a strict dropdown sourced from `Hosts!$A$2:$A`. Everything else
follows from it:

| Cell | Formula |
|---|---|
| `A2` | phone — `VLOOKUP(A1, Hosts!$A:$E, 4, FALSE)` |
| `A3` | `HYPERLINK` to the Mobilize link (column 5), suppressed when blank |
| `D1` | city — column 2 |
| `D2` | email — column 3 |
| `G7` | the distribution list (below) |

```
=IF(A1="","",IFERROR(FILTER('Volunteer Landing Page'!$A$2:$G,
   LOWER(TRIM('Volunteer Landing Page'!$R$2:$R))=LOWER(TRIM($A$1))),""))
```

- Matched on `LOWER(TRIM(...))` so a stray space or case difference between the
  `Hosts` tab and a hand-typed `A1` doesn't silently return an empty list.
- Guarded on a blank `A1`, which would otherwise match every *unassigned* row.
- `TEMPLATE!A1` is left **empty** with a cell note, not a `REPLACE WITH HOST
  NAME` placeholder — strict validation would red-flag the placeholder, making
  the template look broken.
- `TEMPLATE` is grown to 250 rows so the `FILTER` has spill room; it shipped at
  31, which would have `#REF!`d a host with more than 24 volunteers. **Clones
  inherit the grid, so grow the TEMPLATE, not the clone.**

Hosts are added by hand: add the row to `Hosts`, duplicate `TEMPLATE`, pick the
name. Auto-cloning a tab per `Hosts` row was considered and rejected (Rob,
2026-08-21) — it would put tab creation, naming and deletion inside a nightly
job in someone else's live spreadsheet.

## 9. Why the scaffolding is a separate, hand-run mode

`--install-scaffolding` installs the `Hosts` headers, the `TEMPLATE` formulas +
dropdown, and the same treatment for host tabs that already exist. It is
**deliberately not on the nightly path**: `TEMPLATE`'s kit checklist (A:E) and
its instructions cell are program-staff content, and a job that rewrote that tab
nightly would eventually fight whoever was editing it. The nightly path touches
only `_data`, the landing page's own formula/dropdown, and the README.

It is idempotent, so re-running after a staff edit repairs the formulas without
side effects.

Two behaviours worth knowing:

- **Harvest before overwrite.** Before replacing a host tab's header cells with
  lookups, whatever is already typed there is promoted into that host's `Hosts`
  row — but only into cells still blank, so `Hosts` stays the staff-owned source
  of truth. Obvious placeholders (`PHONE NUMBER`, anything ALL CAPS or
  containing `REPLACE`) are ignored. This is how Kevin's city and email reached
  the `Hosts` tab.
- **Legacy retrofit.** `KEVIN Fisher` was cloned from a `TEMPLATE` that predated
  the `Role(s)` column, so its distribution list ran one column short and the
  `FILTER`'s seven columns would have landed under the wrong headers. The
  scaffolding detects that exact legacy header, inserts a column before `K`, and
  labels it. A tab matching *neither* the current nor the known legacy layout is
  skipped with a warning rather than guessed at.

## 10. Registry contract

One enabled row in `ep.hub_host_trackers` = one state's tracker refreshed
nightly. What varies between states isn't the layout, it's **which quiz bases
feed the page and what role each implies** — so that mapping is data:

```sql
quiz_sources ARRAY<STRUCT<base_key STRING, role_label STRING, quiz_label STRING>>
```

MI seeds `mi_poll_monitor_quiz → 'Poll Monitor'` and `mi_rover_quiz → 'Rover'`.
A state adding a third quiz mid-cycle is an `UPDATE`, not a code change.

Prerequisites for registering a state:

1. Every `base_key` is already an **enabled `base_type='quiz'` row** in
   `ep.airtable_sync_sources`. This job reads what `sync_airtable_bases.py`
   landed; it never talks to Airtable.
2. `sheets-controllers@sheets-controllers` is a **writer** on the spreadsheet.
   Unlike the volunteer-export sheets, this job does **not** create the file.
3. Missing quiz columns degrade to NULL with a warning (a state whose quiz never
   asked for a vest size still works), but a missing *table* is a hard error
   pointing at the registry.

## 11. PII

The landing page carries volunteer names, **full mailing addresses**, emails and
phones. The MI file is writer-shared to all of `commoncause.org`, so every
registration is a deliberate PII-exposure decision by the program team — record
it in the registry's `notes`, as the MI row does.

Nothing row-level may leave the sheet and BigQuery: the `--dry-run` report is
**counts-only on purpose**, and no sample rows belong in this doc, a log, or a
ticket. See the `pii-handling-policy` knowledge-library entry.

## 12. Verification performed (2026-08-21)

- 40 MI volunteers from 2 quiz bases; 27 currently shifted / 28 ever; scores
  `7/7`×39, `6/7`×1; 1 missing phone+county (no PTV match); all 40 with an
  address and requested items; 1 repeat submitter correctly collapsed.
- Second run: `40 kept + 0 new` — ledger stable. Scaffolding re-run: no
  re-retrofit, no re-promotion.
- `merge_with_ledger` tested directly: order preserved, a deleted record carried
  forward and flagged, no row shifted, new row appended, stable on a second
  pass, duplicate ledger key collapsed.
- Host `FILTER` exercised end to end: two volunteers assigned to Kevin Fisher
  appeared on his tab across the right seven columns, one via a deliberately
  mangled `"kevin fisher "` (case + trailing space). Test assignments reverted.
- `TEMPLATE` with a blank `A1` renders an empty distribution list, not an error.
- `_data` hidden; landing page frozen/bolded header; dropdown validation present
  on `Volunteer Landing Page!R`, `TEMPLATE!A1` and `KEVIN Fisher!A1`.

## 13. Open items

- **First green Civis run** not yet observed (registered 2026-08-21; the nightly
  fires 3 AM ET). No credential, pin or entrypoint change was needed.
- **`Hosts` phone + Mobilize link are blank for Kevin Fisher** — his tab only
  ever held the `PHONE NUMBER` placeholder and an unlinked label. Program staff
  fill those in; `A2`/`A3` on his tab stay blank until they do.
- **MI's primary → general transition.** When MI wipes shifts, expect
  `Shifted?` to go to `No` across the board. If the general also brings a new
  Airtable quiz base, add it to `ep.airtable_sync_sources` **and** to the MI
  row's `quiz_sources`.
- **Kit inventory (`Quantity on hand`, column E)** is per-host manual and
  untouched by any of this. If MI later wants real inventory tracking, that's a
  different data source, not an extension of this sync.

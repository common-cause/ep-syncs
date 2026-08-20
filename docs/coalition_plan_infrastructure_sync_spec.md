# Coalition Plan "Infrastructure" sync — design + handoff contract

*As-built 2026-08-19/20. Ported into ep-syncs from ep-dashboards
(`scripts/sync_infrastructure_sheet.py`), which had it running on a laptop Task
Scheduler job landing in `ep_dashboards.infrastructure_raw`. Deterministic
pipelines belong on Civis and raw capture belongs here; ep-dashboards models it.*

## What it captures

The **50 State EP Coalition Plan** spreadsheet
(`1YdFVBUcqMF5d4Hniy8CKbxEQ_gJG13Xe7Gkb8HXnasY`), tabs
`Infrastructure (General)` and `Infrastructure (Primary)` — the EP program
team's hand-maintained tracker of per-state setup: training and quiz links, the
forms volunteers file reports on, PTV role hygiene, polling-place selection,
post-election collection. **Program staff own the sheet; this sync is
read-only.**

Tab layout (both tabs, identical): row 1 vestigial `Primary`/`General`
super-group labels (ignored), row 2 sparse column-group labels (forward-filled),
**row 3 is the real header row**, rows 4–54 are 50 states + DC alphabetical by
full name with DC between Connecticut and Delaware. Structure and the wider
workbook are documented in the knowledge-library entry
`ep-50-state-coalition-plan-spreadsheet-structure-and-how-to-populate-it` —
read its "Which columns are the same on both tabs" section before inferring
anything from a filled cell.

## Layers

| Layer | Object | Grain |
|---|---|---|
| Raw | `ep_2026_raw.coalition_plan_infrastructure` | (as_of_date, phase, state, sheet column) — verbatim cell text |
| Cleaned | `ep_2026_cleaned.coalition_plan_infrastructure` | (phase, state, col_name), latest snapshot per phase, cell parsed |
| Consumer | ep-dashboards `int_infrastructure_items` → `mart_infrastructure_*` → 2 Hex pages | program interpretation |

Runner: `misc_jobs/infrastructure_sheet.py`, registered as `infrastructure_sheet`
in `run_misc_jobs.py` and scheduled `daily` in `misc_jobs_schedule.yaml` (the
nightly ~3 AM ET "EP Misc Sync Jobs" Civis job, id 361625051). Two Sheets reads
and ~1,632 rows a night; runtime is seconds.

## Design decisions

**The melt is deliberately dumb, and must stay that way.** One row per
(state, sheet column), cell text verbatim, blanks kept as NULL rows. The program
team adds and renames columns without telling anyone, so a melt turns a schema
change into *new rows* instead of a broken load. Do not pivot it, do not filter
blanks, do not interpret cells in the sync.

**`ep_2026_raw`, not a per-source dataset.** `coalition_plan_raw_2026` would
read better, but `com-dbt@` has no `bigquery.datasets.create` and BigQuery checks
that permission even for `CREATE SCHEMA IF NOT EXISTS` on a dataset that already
exists — so a new dataset is a one-time admin step the sync can never self-heal
(see `docs/asana_nm_sync_spec.md` §8 for what that cost the Asana build). One
table doesn't warrant that; the table name carries the source.

**All parsing lives in the cleaned view (the assignment's "Option A").** Verdict
token, links array, note, character normalization and latest-snapshot dedup are
*source-shape* work — determined by how the sheet is written, not by what EP
wants to measure — so they belong in the interface layer, where any consumer
gets them. What deliberately stays with the consumer: status vocabulary,
readiness scoring, plain-language labels. Those change when the program's
reading changes (twice in one day, on this source).

**`ep_2026_cleaned.norm_state()`** was added alongside `norm_email`/`norm_phone`
so this view emits a resolved USPS code and therefore joins to every other view
in the layer. It absorbs the source's two join-breaking quirks (DC's empty
abbrev cell, row 22's "Lousiana") as explicit aliases. Add an alias when a new
source misspells something; never fuzzy-match — a wrong state is worse than a
NULL one.

**Load job, not streaming insert.** `DELETE` today's partition then append via
`load_dataframe`, so there is no streaming buffer to block a same-day rerun and
a Civis retry is harmless. (Verified: an immediate rerun replaced all 1,632 rows.)

**Internal joins key on `(phase, sheet_row, col_index)`**, not `(phase, state,
col_name)`: `col_name` is not guaranteed unique on a hand-edited header row, and
a state that failed to resolve would drop its own links and notes out of a
NULL-valued join.

**pandas is imported inside `load()`**, not at module scope. `run_misc_jobs.py`
imports every task module at startup, so a module-level import of a dependency
the container might lack would take down every other task — which is exactly how
the 2026-07-30→08-17 outage worked. `pandas` is also named in the Civis
entrypoint's extras (it is *not* part of the `bigquery` extra).

## Loud failures, on purpose

- A missing or renamed tab raises, listing the worksheets it did find. Half a
  snapshot is worse than none.
- A row-3 layout change (column A no longer `State`) raises.
- An empty melt refuses to write.
- Row-3 headers are logged every run, and a state count ≠ 51 warns — so a
  column rename or a deleted row shows up in the nightly log the morning it
  happens instead of as a silent gap in the marts.

## Verification (2026-08-19/20)

- Dry run and live run: 51 states × 16 columns × 2 tabs = 1,632 rows, 87
  non-blank general / 149 primary — matching the ep-dashboards table it replaced.
- Same-day rerun: 1,632 rows deleted and reloaded, immediately (no buffer wait).
- Cleaned view vs the tested ep-dashboards `stg_infra__cells`: **row-for-row
  identical**, including the `links` arrays (`EXCEPT DISTINCT` both directions =
  0 rows, on 1,632 rows). 0 grain duplicates, 51/51 states resolved, 0 NULL.
- `apply_bq_views.py --check` clean for the new and changed files.
- `sync_health` reports one `coalition_plan` stream per phase.

## Handoff to ep-dashboards

Their local Task Scheduler step keeps running until they re-point — and because
this lands in a **different table**, there is no collision and no dual-write
concern at all. Their `stg_infra__cells` reads the old
`ep_dashboards.infrastructure_raw` and dedups to `MAX(as_of_date)`, so if that
local step stopped before they re-point, the marts would go **stale silently
rather than erroring**. Sequence: they re-point `sources.yml` to
`ep_2026_cleaned.coalition_plan_infrastructure`, drop `stg_infra__cells`, then
retire the local step and (once they're happy) `ep_dashboards.infrastructure_raw`.

## PII

The Infrastructure tabs' **column C is a person's name** (State Lead), so
`cell_value` / `cell_text` carry staff names. BigQuery is access-controlled, so
that's fine there. Do not commit row-level extracts, and don't paste real cell
text into docs, commit messages or tickets — the dry-run report is counts-only
for this reason. The wider workbook's State Leads / Landscape tabs (personal
emails, full partner rosters) are **not** captured. Policy: knowledge-library
`pii-handling-policy`.

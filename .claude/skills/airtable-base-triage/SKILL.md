---
name: airtable-base-triage
description: Decide what to do about Airtable bases that have become visible to the sync-operations PAT but are not registered for BigQuery capture — register the EP ones, mute the rest with a reason, escalate the ambiguous. Also handles bases whose access we have LOST. Reads ep.v_airtable_base_triage_queue.
---

# Airtable base triage

## What this pass is for

`misc_jobs/airtable_base_visibility.py` records nightly which Airtable bases the
sync-operations PAT can see. It makes no judgments. This pass makes them.

The failure it exists to prevent: a base that is collecting EP data and is not
registered has **no branch at all** in the `ep_2026_cleaned` union views — not
an empty one. Downstream cannot distinguish "this state collected nothing" from
"this state was never wired", there is no failing job, and no zero row count.
Five live bases sat dark that way from 2026-07-23 to 2026-09-02, and MA rendered
as `not captured` in a funder report while actively collecting.

**You are not discovering bases.** The PAT sees only what someone has granted.
A base nobody shared with us is invisible here no matter what you do — finding
those is the human canvass. Your job is to make sure a grant that *has* landed
does not sit unnoticed.

## Step 0 — the freshness guard. Run this FIRST, every time.

```sql
SELECT MAX(last_seen_date) AS last_sweep FROM `proj-tmc-mem-com.ep.airtable_base_visibility`
```

`last_sweep` must equal today's ET date. **If it is behind, stop and close
failed-on-precondition, naming the stale date.**

This is not a formality. The queue computes visibility as
`last_seen_date = MAX(last_seen_date)`. If the nightly sweep failed, every row
keeps yesterday's date, the maximum moves back with them, every base still
computes as visible, and **the queue goes clean rather than stale**. A failed
sweep is indistinguishable from a quiet night by looking at the queue alone.
Reporting "queue empty" over a sweep that never ran is the same class of error
as the unregistered-base gap itself: a silence that reads as health.

## The detection surface

```sql
SELECT * FROM `proj-tmc-mem-com.ep.v_airtable_base_triage_queue` ORDER BY reason, first_seen_date
```

Three `reason` values, in priority order:

1. **`access_lost`** — an enabled registry row whose base the PAT can no longer
   read. Capture for it is failing or about to. **Escalate; never attempt a
   fix.** You cannot restore an Airtable grant, and disabling the registry row
   to silence the error would convert a loud failure into exactly the silent
   gap this whole system exists to prevent.
2. **`new_unregistered`** — visible, no registry row, not muted. The main case.
3. **`needs_review`** — a prior pass escalated it and no human has ruled yet.
   Re-read it; if you still cannot decide, leave it and say so. Do not flip your
   own prior escalation just to clear the queue.

An empty queue is the healthy steady state and a complete, reportable result.

## Verdicts

Exactly three outcomes for a `new_unregistered` base.

### Register it

Only for bases that collect **2026-cycle-relevant EP program data** — or
prior-cycle EP data, which we capture deliberately (see "Archives" below).

Insert a row into `proj-tmc-mem-com.ep.airtable_sync_sources`. The registration
contract lives in `bq/airtable_sync_sources.sql` — read it before writing. In
short:

- `base_id` is the upsert key. **MERGE, never blind INSERT.**
- `bq_table_prefix` matches `^[a-z][a-z0-9_]*$`, contains **no `__`** (that is
  the landed-table separator), and must not collide with an existing row.
  Check first — BigQuery enforces no uniqueness.
- `state` is a 2-letter code and comes from **your judgment about the base**,
  never from record fields. `US` is the one non-state code in use (tabletop
  exercises).
- `base_type` is `quiz` | `field_report` | `tracker`.
- Verify PAT access before enabling: `python sync_airtable_bases.py --check-access`.

Naming convention, derived from the live registry:

| Situation | Prefix | Example |
|---|---|---|
| The state's current/standing base | `{st}_{kind}` | `ma_quiz`, `ri_field_report` |
| A per-cycle clone | `{st}_{year}_{qualifier}_{kind}` | `ma_2026_primary_quiz` |
| A prior-cycle archive | `{st}_{year}_{kind}` | `pa_2024_quiz` |
| A second base of the same kind | add a qualifier | `mn_quiz_2`, `mo_legal_monitor_quiz` |

Then set `triage_status='registered'` on the visibility row. This is
bookkeeping only — the queue derives registration from the registry, so the
base leaves the queue whether or not you remember this step.

### Mute it (`triage_status='ignored'`)

For bases that are **not EP program data**. `triage_notes` is required: an
unexplained mute is indistinguishable from a base someone forgot, which is the
failure mode this table exists to prevent.

Reliable not-EP categories, from the 2026-09-02 inventory:

- **Social Media Monitoring Reports** (~20 bases, many partner-named: ACLU of
  Utah, APIA Vote, NALEO, Make the Road, COPAL MN…). A *different program* —
  disinformation monitoring, not Election Protection. Partner names in the title
  do not make it EP.
- **Legislative session trackers** — "2023 Florida Legislature", "Terms for 2019
  Florida Legislators", gerrymandering pledges.
- **Internal admin** — "Corporate organizational chart", "Creative Request
  Form", "Project planning", "Running List", "Common Cause Emails Database".
- **Scratch / API test** — "JotForm API", "Test Table for JotForm", "Untitled
  Base", "TMC EP Clone 1-3", "Field Reporting Test".
- **Templates — never register.** `app64MZeqXk6BuuPi`,
  `app00ZGvBKtveksbn`, `appTt4SsXD0lsBU6i`. Registering a template captures
  its dummy rows as if they were program data.
- **OH BOE Communications Tracker** `appTQh59UzvukR6rL` — explicit opt-out on
  record (public voter-submission tracker, self-reported member PII, no
  downstream consumer). Decided 2026-07-23; do not relitigate.

### Escalate it (`triage_status='needs_review'`)

When you cannot tell. Say what you checked and what would settle it.

## Archives: capture them

Standing policy since 2026-09-02 (Rob's call): capture **every non-template EP
quiz base the PAT can see**, including prior cycles. Two reasons it is safe:

- Cycle is a property of a **row, not a base** — MA, MD, MN and MO reuse ONE
  base across elections, so no registry flag could separate cycles anyway.
- `ep_2026_cleaned.quiz_responses` filters `created_at >= 2026-01-01`, so
  archive rows land in `ep_2026_raw` and never reach the 2026 interface layer.

So "this base is from 2024" is **not** a reason to mute. It is a reason to
year-qualify the prefix.

## Bootstrap: the first pass is a backlog, not a night's work

The first sweep recorded 192 bases and the queue opened at **134**
`new_unregistered` — that is the accumulated history of every base ever shared
with this PAT, not a night's drift. Steady state after it is cleared is ~0.

Do not attempt 134 individual judgments in one unattended pass. Work by
category: apply the not-EP categories above as batches with a shared note, then
individually triage the EP-shaped residue, then escalate what is left. **Get Rob
in the loop for the first pass** — a bulk mute is the one action here that is
hard to notice being wrong.

## Guards

- **Never modify a row whose `triaged_by` is a person.** Human verdicts are not
  an agent's to revise. Correcting a *prior agent* verdict is fine, and the
  correction goes in `triage_notes`.
- **Never change `bq_table_prefix` on an existing registry row.** It names
  landed BigQuery tables; changing it orphans every table already captured.
- **Never set `enabled=TRUE` without PAT access verifying.** Register disabled
  with a note instead — a disabled row is visible downstream, an absent one
  is not.
- **Never disable or delete a registry row to clear an `access_lost` entry.**
- **Never DELETE from `ep.airtable_base_visibility`.** The ledger's value is
  that rows persist; a deleted row makes a base look new again forever.
- **When in doubt, `needs_review`.** A wrong mute silently loses a state's
  data for a cycle; a `needs_review` row just waits. Asymmetric costs — bias
  hard toward the cheap error.

## Verification, after any write

```sql
-- 1. The queue no longer carries what you just handled (and nothing new broke)
SELECT reason, COUNT(*) FROM `proj-tmc-mem-com.ep.v_airtable_base_triage_queue`
GROUP BY reason;

-- 2. No prefix collisions introduced
SELECT bq_table_prefix, COUNT(*) c
FROM `proj-tmc-mem-com.ep.airtable_sync_sources`
GROUP BY 1 HAVING c > 1;

-- 3. No muted base lacks a reason
SELECT base_id, base_name FROM `proj-tmc-mem-com.ep.airtable_base_visibility`
WHERE triage_status = 'ignored' AND (triage_notes IS NULL OR triage_notes = '');
```

All three must return zero rows (query 1: zero for the reasons you handled).

If you registered anything, the capture picks it up on the next 6:45 AM ET
run of `sync_airtable_bases.py`. To land it immediately:
`python sync_airtable_bases.py --bases <prefix>`.

## Reporting

Counts by verdict, and for anything registered: base name, prefix, state,
base_type, and the table inventory `--check-access` reported. For anything
escalated: what you checked and what would settle it.

**PII: base names and counts only.** Never read or report records — this pass
needs the base list, not its contents.

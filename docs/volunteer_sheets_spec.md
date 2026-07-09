# Volunteer Sheets Sync — Spec

*Drafted 2026-07-08. Status: **built and verified locally** (2026-07-08) —
all 51 state sheets + ACLUM partner prototype live in Drive. **Not yet
scheduled in Civis**; partner-code registry needs a curation pass first
(§8).*

Coalition/state-facing Google Sheets built from PTV volunteer data: one set of
spreadsheets **by state**, one set **by partner source code**, living in the
externally-shareable "2026 EP Volunteer Exports" shared-drive folder,
refreshed daily by `sync_volunteer_sheets.py`. Replaces the 2024 approach
(manually-configured Connected-Sheets query tabs with scheduled refresh).

Partners are expected to **edit these sheets** (match-back, notes, tracking
columns), so the design's central constraint is: *the sync must never destroy
partner edits*.

---

## 1. Approach: scheduled push, not Connected Sheets

The job queries BQ and writes plain values via ccef-connections
`SheetsWriterConnector` (in the v0.2.0 release tag, shared-drive capable).
Rejected alternative: automating Connected-Sheets setup via API — the Sheets
API can attach a BQ data source, but refresh schedules can't be set
programmatically and refreshes run under a user identity with BQ access, so
the manual per-sheet setup pain survives.

Push-job advantages: partners see plain values (no BQ access questions),
creation is fully automated and idempotent, and it reuses the existing stack
(ccef-connections, `{NAME}_PASSWORD` creds, GitHub-backed Civis container
job, registry-table pattern from the shift sync).

## 2. Spreadsheet anatomy — protecting partner edits

Each spreadsheet has three job-managed tabs plus whatever partners add:

| Tab | Owner | Behavior |
|---|---|---|
| `Volunteers` | shared | A1 holds one array formula (`={_data!A:R}`) mirroring `_data`. Partners annotate in columns to the **right** of the mirrored block and may add their own tabs. Seeded at creation; re-seeded only if A1 is ever found empty (accidental deletion self-heals; partner content is never overwritten). Grid is grown as data grows, never shrunk. |
| `README` | sync job | Usage instructions (don't sort/insert rows, annotate to the right, Active-flag semantics). Rewritten every run. |
| `_data` (hidden) | sync job | The extract. Cleared + rewritten every run. Hidden for tidiness, not security — everyone with sheet access is cleared for the data. |

**Row-stability contract.** Partner annotations right of the formula block
only stay aligned if row order never changes. Three rules guarantee that:

1. **All-time roster, not current snapshot**: every (state, email) ever seen
   in `ptv_raw_2026.users` (append-only snapshots make this trivial), with an
   `Active` column = present in the state's latest snapshot and `Last Seen` =
   the last snapshot date the row appeared. Rows are never removed.
2. **Stable sort: PTV `id` ascending** (registration order) — new volunteers
   always append at the bottom of every sheet. (NULL ids sort last; ties
   break on state, email.)
3. **Partner-sheet membership is by *any* snapshot's source code**, not just
   the latest (`codes_ever` in the roster query) — a code edit in PTV can't
   silently pull rows out of a partner's sheet.

## 3. Data content

All PII ships — a primary purpose is letting partners match volunteers back
to their own lists. One row per (state, email); a volunteer registered in two
states appears in both state sheets.

Columns (A:R): PTV ID, First Name, Last Name, Email, Phone, County, Zip,
State, Join Date, Source Code, Role, Training, Shifts Claimed, Upcoming
Shifts, First Shift, Latest Shift, Active, Last Seen.

Shift columns come from `v_shift_volunteers_current` (LEFT JOIN on
(state, email)) — **not** the raw `users.shifted` flag, which is
known-unreliable. Attribute columns show the volunteer's *latest* snapshot
values.

**State sheets**: all volunteers in that state, every source code (including
`previous_years` and blanks). **Source-code sheets**: all states for that
code, matched case-insensitively (PTV codes appear as `ONEAZ`/`oneaz` etc.).

## 4. Which sheets exist — the registry

`proj-tmc-mem-com.ep.volunteer_sheet_targets` (created + seeded 2026-07-08;
DDL and contract in `bq/volunteer_sheet_targets.sql`). Inserting an enabled
row is how a sheet comes into existence — the next run creates and populates
it. This is the "middle table" between the historical partner-code flags and
the export: a source_code target's `source_codes ARRAY<STRING>` lumps several
codes belonging to one group into a single sheet.

Seeded contents:
- **51 state targets** (all states + DC), enabled.
- **83 source-code targets** from `ep_archive.source_codes WHERE
  external='Y'` intersected with codes present in 2026 data, deduped
  case-insensitively, minus `previous_years`/`quiz`/`actionnetwork`.
  Enabled, but **needs Rob's curation pass before the Civis job is
  scheduled** — the archive flags are imperfect (e.g. `fec`, `new`, `wfh`
  look dubious) and grouping decisions (e.g. `seiu668` + `SEIU668-26`) are
  judgment calls.

Codes **new in 2026 are not in the archive** and must be added by hand. The
sync's end-of-run report warns about active codes ≥25 volunteers that no
target covers; at build time that list included: riseup (424), common cause
oregon (405), ccaz (193), civicne (164), indivisibleaz (87), cpc (78),
puente (60), members (39), ccri (32), and a few smaller.

## 5. Script + job shape

`sync_volunteer_sheets.py` at project root:

1. Read enabled registry rows; one BQ query for the full all-time roster
   (already in stable sheet order); partition rows per target in memory.
2. Per target: `get_or_create_spreadsheet(sheet_title, folder_id=<subfolder>)`
   (idempotent by title within the subfolder) → rewrite `_data` → ensure
   `Volunteers` mirror tab (grow-only) → rewrite `README` → hide `_data` →
   drop default `Sheet1` → apply `share_with` grants (skipping existing
   permissions, so no re-invites).
3. Per-target try/except failure isolation; exit 1 if any target failed.
4. `--targets NE,PA,aclum` CLI override for ops/testing.

Civis: GitHub-backed container job, daily **7:00 AM ET** (after all-volunteers
lands at 6:30). Credentials: `BIGQUERY_CREDENTIALS_PASSWORD` +
`GOOGLE_SHEETS_CREDENTIALS_PASSWORD`. Full details + status in
`civis/SCHEDULED_SCRIPTS.md`. Install extra: `ccef-connections[bigquery,sheets]`
(v0.2.0 tag suffices).

Rate limits: ~130 targets × ~12 Sheets/Drive calls against a 60 write-req/min
quota → a full run takes 20–30 minutes; per-call 429s are retried with
backoff (`retry_google_operation`), plus a 1s pause between targets.

## 6. Drive placement

- Root: **"2026 EP Volunteer Exports"** shared-drive folder,
  `18oLarQuNErA7qMMz0YBRKEZyP-3eQDMS`.
- Subfolders (created 2026-07-08, IDs hardcoded as constants in the script):
  `By State/` = `1KkTrm93PmNNEVH_drxdD8b_hYHlLVKcC`,
  `By Partner/` = `1apbIxjIQ2e5DRHYtvSCGFyMGpVaG-eKJ`.
- The SA (`sheets-controllers@sheets-controllers.iam.gserviceaccount.com`) is
  a member of the shared drive: verified list/create/edit/trash (no
  hard-delete — Contributor level; the sync never deletes files, so
  sufficient). Files in a shared drive belong to the drive, which sidesteps
  the SA's no-storage-quota limitation entirely.
- Sharing: folder/drive membership handles internal + standing coalition
  access; per-partner external shares via the registry's `share_with` (the
  job grants writer, idempotently) or manually.

## 7. Verified at build time (2026-07-08)

- All 51 state sheets + ACLUM partner sheet created and populated (PA: 5,247
  all-time rows; NE: 479; ACLUM: 223).
- Mirror formula renders `_data` on the `Volunteers` tab; `_data` hidden.
- Rerun idempotency: no duplicate sheets, formula not re-seeded.
- Partner-edit preservation: an annotation in column S survived a full
  refresh untouched.

## 8. Remaining before go-live

1. **Rob curates the 83 seeded partner-code targets** (delete/disable junk,
   set groupings via `source_codes` arrays, fix canonical `sheet_title`
   casing where it matters, add 2026-only codes like riseup/civicne/ccaz if
   they're wanted).
2. Run the sync once for all source_code targets (creates the partner
   sheets).
3. Create the Civis job per `civis/SCHEDULED_SCRIPTS.md` (needs a
   `GOOGLE_SHEETS_CREDENTIALS` credential in Civis) + enable failure
   notifications.
4. Decide who shares which partner sheets with which external emails
   (registry `share_with` vs. manual).

## 9. Deliberately out of scope (phase 1)

- Per-audience column trimming (decided: everyone gets the full field set).
- Protected ranges on `Volunteers` A:R (add if accidental partner edits
  inside the mirror block prove common).
- Write-back (reading partner annotations into BQ) — plausible phase 2; the
  stable-row design makes it easy later.
- Intra-day refresh cadence.

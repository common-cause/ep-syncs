# Airtable Bases Capture — Design & Landing-Zone Contract

*As-built 2026-07-23. Script: `sync_airtable_bases.py`. Registry:
`bq/airtable_sync_sources.sql`. History DDL: `bq/airtable_records_history.sql`.
Civis job spec: `civis/SCHEDULED_SCRIPTS.md` ("sync_airtable_bases.sh").*

This document is the **contract the `ep_2026_cleaned` interface layer builds
against**. If the landing shape changes, change this doc in the same commit.

## What it does

For each enabled row in `proj-tmc-mem-com.ep.airtable_sync_sources` (one row
per Airtable base), every run:

1. Reads the base's live schema via the Airtable metadata API
   (`AirtableConnector.get_base_schema`, ccef-connections ≥ 0.5.0).
2. Discovers **every table in the base** minus the row's `exclude_tables`
   (all-by-default: a state adding a new table lands automatically with no
   registration churn — the drift-proof choice).
3. Per table:
   - **Typed table** `ep_2026_raw.{bq_table_prefix}__{table_key}` rebuilt via
     a load job (`WRITE_TRUNCATE` full replace).
   - **History append**: one row per record into
     `ep_2026_raw.airtable_records_history` (verbatim JSON payload),
     after a per-(day, base) pre-delete for rerun idempotency.

Read-only toward Airtable. Per-base and per-table failure isolation; exit 1
on any failure.

## Scope (2026)

Field-report bases + quiz bases provisioned by ep-airtable-utilities.
**Not** captured: template bases (`app64MZeqXk6BuuPi`, `app00ZGvBKtveksbn`,
`appTt4SsXD0lsBU6i`) and the OH BOE tracker (`appTQh59UzvukR6rL`) — by
decision 2026-07-23. Seeded 2026-07-23: 14 bases (6 field-report: NE PA WI
UT MD MI; 8 quiz: NY PA UT OR OR-dropbox OR-trusted-messenger
MI-poll-monitor MI-rover), all PAT-verified and enabled.

## Typed-table contract (what interface views read)

Table name: `{bq_table_prefix}__{table_key}` where
`table_key = sanitize_column_name(airtable_table_name)`.
Example: `ep_2026_raw.ut_field_report__poll_monitoring_checklist`.

Columns, in order:

| Column | Type | Source |
|---|---|---|
| `_airtable_record_id` | STRING | record id (`rec...`), unique per row |
| `_airtable_created_time` | TIMESTAMP | record `createdTime` |
| one per Airtable field | see type map | present even when all-null (schema-derived) |
| `_synced_at` | TIMESTAMP | run timestamp (UTC) |

- Column names: `sanitize_column_name(field_name)` — lowercase, non
  `[a-z0-9_]` → `_`, runs collapsed, leading digit prefixed with `_`.
- **Collision policy**: two fields sanitizing identically get deterministic
  `_2`/`_3` suffixes by schema order, with an ERROR log naming both fields.
- **Type map** (dtypes derive from Airtable field *metadata*, never from
  data — so an all-null column keeps its declared type):
  number/currency/percent/duration → FLOAT64; autoNumber/count/rating →
  INT64; checkbox → BOOL; date/dateTime/createdTime/lastModifiedTime →
  TIMESTAMP; **everything else STRING**. Lists/dicts (multipleRecordLinks,
  attachments, multi-selects, some lookups/formulas) are JSON-serialized
  strings — e.g. a record link lands as `'["recAAABBBCCCDDD1"]'`, readable
  with `JSON_VALUE(col, '$[0]')` (BQ JSON functions accept JSON-formatted
  STRINGs).
- Empty source tables still produce an empty typed table with the full
  schema-derived column set (downstream views compile before data exists).
- A failed cast (e.g. a formula field returning error objects into a numeric
  column) logs a warning and leaves the column as delivered — consumers
  should `SAFE_CAST` defensively.
- **Schema drift**: renamed/added/retyped fields simply re-derive next run
  (full replace). A renamed *table* orphans the old typed table — drop it
  manually.

Fabricated example row (`ne_field_report__shifted_volunteers`):

| _airtable_record_id | _airtable_created_time | email | first_name | ... | _synced_at |
|---|---|---|---|---|---|
| recXXXXXXXXXXXXXX | 2026-06-01 14:03:22 UTC | jane.doe@example.com | Jane | ... | 2026-07-23 10:45:00 UTC |

## History-table contract

`ep_2026_raw.airtable_records_history` — full DDL + column descriptions in
`bq/airtable_records_history.sql`. One row per record per run;
`PARTITION BY as_of_date`, `CLUSTER BY bq_table_prefix, table_key`;
`fields` is the **verbatim** payload (UNSANITIZED field names as JSON keys;
empty fields absent, per Airtable API semantics).

**Reader dedupe recipe** — JSON columns are not groupable/comparable, so
`SELECT DISTINCT` does NOT work here (unlike the ptv_raw_2026 views):

```sql
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY as_of_date, bq_table_prefix, table_key, airtable_record_id
  ORDER BY synced_at DESC) = 1
```

Same-day reruns pre-delete the (day, base) slice; when the streaming buffer
blocks the delete (~30-90 min window) the rows double and the recipe above
collapses them. Verified 2026-07-23 (202 raw → 101 deduped).

**Attachment URLs expire within hours** of capture — the history preserves
structure/audit, not files. (Checklist-photo archival, if ever needed, is a
separate job; ep-airtable-utilities has `download_checklist_images.py`.)

## Write paths (why two)

- Typed tables use `load_dataframe` (load job): no streaming buffer,
  inherently idempotent replace, pandas nullable dtypes → native BQ types.
- History uses streaming `insert_rows` in 500-row chunks with
  `json.dumps(fields)` into the JSON column (verified working — the
  `fields_json STRING` fallback in the design was not needed). A load-job
  path for JSON columns was rejected: pandas→parquet JSON handling varies
  by pyarrow version and `load_dataframe` can't pass an explicit schema.

## Registry

`ep.airtable_sync_sources` — one row per base. Full column semantics +
registration contract in `bq/airtable_sync_sources.sql` (PAT validation,
prefix rules `^[a-z][a-z0-9_]*$` with no `__`, upsert-by-base_id,
insert-disabled → `--check-access` → `--list` review → enable).
`canonical_overrides` (JSON) is reserved for the `ep_2026_cleaned`
union-view generator — the capture sync ignores it.

ep-airtable-utilities registers new bases at go-live (spec mailed
2026-07-23: `ep-syncs__airtable-base-capture-registration-spec.md`).

## Ops

```bash
python sync_airtable_bases.py                          # all enabled bases
python sync_airtable_bases.py --bases ne_field_report,ut_quiz
python sync_airtable_bases.py --only ne_field_report__incident_reports
python sync_airtable_bases.py --list                   # discovery review, writes nothing
python sync_airtable_bases.py --check-access           # PAT gate, incl. disabled rows
```

Civis: daily 6:45 AM ET (after the 6:00 shift sync's Airtable upserts, so
the day's "Shifted Volunteers" capture includes them). See
`civis/SCHEDULED_SCRIPTS.md`.

## Verification log (2026-07-23)

- `--check-access`: 14/14 bases accessible with the sync-operations PAT.
- `--list`: table inventories reviewed; no template leftovers anywhere
  (UT's bespoke "Poll Monitoring Checklist" discovered correctly; NE
  carries two extra real tables, All Volunteers + Volunteer Stories).
  No `exclude_tables` needed at seed time.
- Single-base run (NE): 6 tables, 101 typed + 101 history rows;
  `JSON_VALUE(fields, '$.Email')` extraction confirmed; typed dtypes
  confirmed (autoNumber→INT64, createdTime→TIMESTAMP, links→STRING JSON).
- Same-day rerun: pre-delete blocked by buffer as expected; dedupe recipe
  returned exactly 101; typed tables stable (replace).
- Full run: 14/14 bases, 34 tables, 2,335 rows, exit 0. 14 of 34 tables
  empty so far (typed empty tables exist; history rows only for records).

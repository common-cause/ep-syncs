# EP Syncs

Sync scripts connecting Protect the Vote (PTV) shift scheduling and Airtable to BigQuery for election protection volunteer data.

## Project Type
bigquery

## Connections & External APIs

**All external API connections use `ccef-connections`.** Do not write your own BigQuery,
Airtable, or PTV clients directly in this project.

The shared library lives at:
```
C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections
```
Install it with:
```bash
pip install -e "C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections"
```

**If a PTV API wrapper or Airtable client doesn't exist in `ccef-connections` yet:**
Spec it out and build it *in `ccef-connections`*, then import it here.
Do not duplicate connection logic in individual projects.

## Credential Pattern
All credentials follow `{CREDENTIAL_NAME}_PASSWORD` in `.env` (Civis-compatible).
JSON credentials are stored as unquoted JSON strings. Never commit `.env`.

Active credentials in `.env`:
- `BIGQUERY_CREDENTIALS_PASSWORD` — seeded (GCP: `proj-tmc-mem-com`, SA: `com-dbt@`)
- `AIRTABLE_API_KEY_PASSWORD` — fill in your Airtable personal access token
- `PTV_API_KEY_PASSWORD` — fill in once credential type is confirmed from sample script

## BigQuery MCP

The global `bigquery` MCP is active and pre-approved for this project. Use `bq_query(sql)` and `bq_list_tables(dataset)` to query data or inspect tables without leaving the conversation. Connects to `proj-tmc-mem-com` using the shared service account.

```
bq_query("SELECT * FROM ep.some_table LIMIT 5")
bq_list_tables("ep")
```

## Schema MCP (bq-schema-docs)

The global `schema` MCP provides field-level documentation for all 63 datasets in `proj-tmc-mem-com`. Use it to understand table structure before writing queries.

```
schema_list_datasets()                                                           # master index of all datasets
schema_get_dataset("ep")                                                         # README + data model overview
schema_list_tables("ep")                                                         # all table names in a dataset
schema_get_table("ep", "some_table")                                             # all fields + types
schema_search("volunteer", dataset="ep")                                         # find tables by keyword
```

All tools are pre-approved — no confirmation needed. Docs are auto-generated from INFORMATION_SCHEMA.

## Key Files
_TODO: add key files and their purpose_

## How to Run
_TODO_

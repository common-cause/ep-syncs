# AsanaConnector — Buildout Spec

*From: ep-syncs. Date: 2026-07-17. Requested build location: ccef-connections
(per the shared rule: connection logic lives in ccef-connections, consuming
projects import it).*

## Why

Common Cause New Mexico is adopting Asana for Election Protection volunteer
tracking (NM lead: Cesar Marquez, cmarquez@commoncause.org). ep-syncs will run
a nightly Asana → BigQuery snapshot sync (via the existing "EP Misc Sync Jobs"
Civis runner) and needs an `AsanaConnector` to do it. Nothing Asana-related
exists in ccef-connections today (verified 2026-07-17).

**The NM workspace is on a paid Asana plan** — custom fields are available and
the paid rate limit applies. Design for paid-tier behavior but fail clearly on
free-tier restrictions (see 402 below), since future consumers may hit free
workspaces.

## Scope

**Read-only, v1.** Pull tasks (with custom fields), projects, sections, and
workspaces. No write-back, no webhooks/Events API (needs a public HTTPS
receiver — doesn't fit Civis polling), no search API (paid-only endpoint,
60 req/min; defer until a consumer needs it), no stories/comments (defer).

## API facts (verified against developers.asana.com, 2026-07-17)

- **Base URL:** `https://app.asana.com/api/1.0`
- **Auth:** Personal Access Token as a Bearer header:
  `Authorization: Bearer <PAT>`. PATs work on all plan tiers and are tied to a
  human user account (Asana service accounts are Enterprise-only). The token
  inherits that user's project access.
- **Response envelope:** everything is wrapped — `{"data": <object|array>}`.
  Paginated list responses add `"next_page": {"offset", "path", "uri"} | null`.
- **Pagination:** offset tokens, `limit` 1–100 per page. **You must pass
  `limit` to reliably get `next_page`** (without it some endpoints truncate
  around ~1,000 rows or time out). Loop: request with `limit=100`, follow
  `next_page.offset` as the `offset` param until `next_page` is null. Offset
  tokens expire as data changes — never persist them across runs; a mid-
  pagination token-expiry error should surface as a retryable failure of the
  whole listing call, not a partial success.
- **Rate limits:** 1,500 req/min on paid domains (150 free), per-token.
  429 responses carry a `Retry-After` header (seconds). Concurrency caps:
  50 in-flight GETs / 15 in-flight writes (irrelevant for serial use).
- **Errors:** JSON body `{"errors": [{"message": ..., "help": ...}]}`.
  GIDs are opaque strings, not ints.
- **`opt_fields`:** comma-separated, dot notation for nested fields
  (`assignee.email`, `memberships.section.name`). Default responses are
  compact stubs (gid/name/resource_type) — a useful task pull *requires*
  opt_fields.
- **Custom fields:** requesting `custom_fields` via opt_fields returns full
  objects on each task: `gid`, `name`, `type` (`text|number|enum|multi_enum|
  date|people`), type-specific value fields, and `display_value` — a universal
  string rendering. `display_value` is the recommended consumption path for
  syncs; keep the raw objects available for typed access.

## Connector design (follow house patterns)

- `src/ccef_connections/connectors/asana.py`, class
  `AsanaConnector(BaseConnection)`; register in `connectors/__init__.py`
  (lazy PEP-562 map). **Base install only** — `requests` + `tenacity`, no new
  extra.
- **Credential:** `ASANA_API_KEY_PASSWORD` (plain string, not JSON), via
  `CredentialManager`; add a named helper `get_asana_api_key()` alongside the
  existing per-service helpers in `core/credentials.py`.
- **Template:** `ActionNetworkConnector` is the closest model — central
  `_request()` + `_paginate()` + public methods behind a retry decorator.
  Differences: Bearer-header auth, `{"data": ...}` unwrapping, offset-token
  pagination instead of HAL `_links.next`.
- **Retry decorator:** add `retry_asana_operation` in `core/retry.py` using
  the "honor Retry-After" family (`retry_action_builder_operation` /
  `retry_github_operation` pattern): retry only on `RateLimitError`, wait
  `retry_after + 2s`, `stop_after_attempt(5)`, `reraise=True`.
- **Status-code mapping in `_request()`:**
  - `401` → `AuthenticationError`
  - `402` → non-retryable error with an explicit message ("paid-tier Asana
    feature on a free workspace") — Asana uses 402 Payment Required for
    premium-only endpoints/params
  - `429` → `RateLimitError(retry_after=<Retry-After header>)`
  - other `>= 400` → `ConnectionError`, include the `errors[].message` text
  - unwrap and return `data` on success
- **Lifecycle:** `connect()` builds a `requests.Session` with the auth header
  and validates via `GET /users/me` (store the authed user's gid/name for
  logging); `health_check()` = `GET /users/me`; `disconnect()` closes the
  session. Context-manager support comes from `BaseConnection`.

### Public methods (v1)

| Method | Endpoint | Notes |
|---|---|---|
| `get_workspaces()` | `GET /workspaces` | paginated |
| `get_projects(workspace_gid, archived=None)` | `GET /projects?workspace=` | paginated; `archived` filter passthrough |
| `get_project(project_gid)` | `GET /projects/{gid}` | project metadata incl. custom field settings if requested |
| `get_sections(project_gid)` | `GET /projects/{gid}/sections` | paginated |
| `get_project_tasks(project_gid, opt_fields=DEFAULT_TASK_FIELDS, modified_since=None, completed_since=None)` | `GET /tasks?project=` | the workhorse; paginated; datetime params ISO-8601 |
| `get_task(task_gid, opt_fields=DEFAULT_TASK_FIELDS)` | `GET /tasks/{gid}` | single fetch |
| `get_subtasks(task_gid, opt_fields=DEFAULT_TASK_FIELDS)` | `GET /tasks/{gid}/subtasks` | paginated |

`DEFAULT_TASK_FIELDS` (module constant, overridable per call):
`name, notes, completed, completed_at, created_at, modified_at, due_on,
due_at, start_on, assignee.name, assignee.email, memberships.section.name,
memberships.project.name, tags.name, custom_fields, num_subtasks, parent.gid,
parent.name, permalink_url` (gid always returned).

Methods return plain `list[dict]` / `dict` (raw Asana objects, `data`
unwrapped) — consuming projects own their flattening, same as the other
connectors.

## Testing & acceptance

- Unit tests with mocked responses covering: envelope unwrapping; a 2–3-page
  `next_page` chain terminating on null; 429 → Retry-After honored → success;
  401 → `AuthenticationError`; 402 → clear non-retryable error; opt_fields
  and modified_since passed through as query params.
- Live smoke test needs a PAT with access to an NM project. Provisioning is
  pending on the ep-syncs side (open question: dedicated sync account vs. a
  staffer's PAT — coordinate with Rob, who is coordinating with Cesar
  Marquez). Connector work should proceed on mocks; don't block on the token.

## Release

Bump version + **cut and push a `vX.Y.Z` git tag** — the Civis jobs install
ccef-connections pinned to a release tag. Related housekeeping noticed while
speccing: `pyproject.toml` is at `0.2.1` but only `v0.2.0` is tagged; either
push a `v0.2.1` tag or fold the fix into this release's tag.

## What ep-syncs will do with it (context, not scope)

```python
from ccef_connections.connectors import AsanaConnector

with AsanaConnector() as asana:
    tasks = asana.get_project_tasks(project_gid)  # one call per NM project,
# flatten (display_value per custom field) → snapshot-append to
# asana_raw_2026.tasks, date-partitioned on as_of_date — same idempotency
# model as ptv_raw_2026. Registry-driven target list TBD on the ep-syncs side.
```

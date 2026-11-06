# HTTP API

The SDK handles uploads, revisions, retries and recovery for you, so most users
never call the API directly. Use it when you build your own client.

Base URL: `https://api.isagoge.in/v1/leanwarp`

Authenticate every request with an API key from the
[dashboard](https://isagoge.in/dashboard):

```http
Authorization: Bearer lw_live_…
```

## Workflow

1. `GET /versions` lists the environments LeanWarp serves. Pick one whose
   `lean_toolchain` and dependencies suit your files.
2. `POST /workspaces` with `{"bundle_id": …}` creates a workspace at revision
   `0`. No worker (the server that runs Lean) starts.
3. `PUT /workspaces/{workspace_id}/files` uploads `.lean` files and returns the
   new `revision`.
4. `POST /workspaces/{workspace_id}/operations` submits an operation against
   that revision. It returns `202 Accepted` with an `operation_id`.
5. `GET /operations/{operation_id}?wait_seconds=20` until `state` is
   `completed`, `failed` or `cancelled`. The server holds each read until the
   operation ends or `wait_seconds` (0 to 20) pass, so you don't need to poll
   quickly.
6. `POST /workspaces/{workspace_id}/stop` stops the worker.

```sh
curl -s https://api.isagoge.in/v1/leanwarp/workspaces/$WORKSPACE/operations \
  -H "Authorization: Bearer $LEANWARP_API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"kind": "check", "expected_revision": 1, "payload": {"file": "Main.lean"}}'
```

## Endpoints

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| `GET` | `/versions` | none | Served environments, one current build each. A key is optional, but one you send must be valid. |
| `GET` | `/resources` | read | Published worker resources and billing metadata. |
| `GET` | `/account` | read | Balance, reserved and available credit, in microdollars. |
| `POST` | `/workspaces` | write | Create a workspace. |
| `GET` | `/workspaces` | read | List the workspaces your key can see. |
| `GET` | `/workspaces/{workspace_id}` | read | A workspace's state and current revision. |
| `GET` | `/workspaces/{workspace_id}/files` | read | The current revision and each file's SHA-256, without contents. |
| `PUT` | `/workspaces/{workspace_id}/files` | write | Upload or delete files. Returns the new revision. |
| `POST` | `/workspaces/{workspace_id}/operations` | execute | Submit an operation. See [Operations](#operations). |
| `GET` | `/workspaces/{workspace_id}/operations` | read | Stored operations with results, newest first. See below. |
| `GET` | `/workspaces/{workspace_id}/usage` | read | Each history limit's `used` and the account's `limit`. |
| `GET` | `/operations/{operation_id}` | read | An operation's state and result. `?wait_seconds=N` waits up to 20 seconds for it to end. |
| `POST` | `/operations/{operation_id}/cancel` | execute | Ask to cancel, then poll until it ends. |
| `POST` | `/workspaces/{workspace_id}/stop` | write or execute | Stop the worker. Files are kept. |
| `DELETE` | `/workspaces/{workspace_id}` | write | Delete a workspace. |
| `POST` | `/feedback` | any | Report a problem with LeanWarp itself. See [Feedback](#feedback). |

A key can be limited to one workspace. It then sees only that workspace.

The operation list takes `limit` (1 to 25, default 25) and `cursor`. It returns
`{"operations": […], "next_cursor": …}`. To read the next, older page, pass
`next_cursor` back unchanged. It is `null` after the last page. A page can hold
fewer than `limit` operations, to keep the response near a 1 MiB UTF-8 target.
A nonempty page always includes its first operation, even if that operation
exceeds the target. Legal pages stay below the SDK's 2 MiB response limit.
LeanWarp keeps only a window of finished history, so export results you need.

## Operations

Each operation reads the revision named by `expected_revision` and never changes
its files. `file` is a `.lean` path relative to the project. Lines and columns
start at 1.

| `kind` | `payload` |
| --- | --- |
| `check` | `file`, `strict` (default `false` over HTTP; the SDK defaults to strict). |
| `inspect` | `file`, `line`, `column`. |
| `try_tactics` | `file`, `line`, `column`, `tactics` (1 to 32), `timeout_ms` per tactic (default 30000). |
| `verify_target` | `file`, `candidate_declaration`, `target_statement`, `target_context`, `execution_mode` (`reusable` or `fresh`), `timeout` seconds. |
| `holes` | `file`, `goals` (default `true`: bounded goals for the first 16 unfinished or unknown placeholders). |
| `suggest` | `file`, `line`, `column`. Optional `tactics` (1 to 32) to use instead of LeanWarp's portfolio, and `timeout_ms` per tactic (learned budgets by default). |
| `extract_decls` | `file`. |
| `sorry2lemma` | `file`, `max_extractions` (1 to 256, default 64), optional `name_prefix`, `strict_final_check` (default `false`). |
| `check_files` | `files` (1 to 64 distinct paths), `strict` (default `true`), optional `timeout` seconds per file (1 to 300). |
| `verify_batch` | `codes`: 1 to 64 objects `{"custom_id", "proof"}`. Each `custom_id` is unique and 1 to 128 characters, without control characters. Proofs must not be blank. The payload is at most 256 KiB of UTF-8. Also `strict` (default `true`) and optional `timeout` seconds per proof (1 to 300). The proofs run in the workspace's environment. They may import its packages, such as Mathlib, but not the workspace's own modules. Such a proof is refused with `error.code` `unsupported_import`. |

`suggest`, `check_files` and `verify_batch` stop starting new work after four
minutes. Their default `timeout_seconds` is 600, enough for the work still in
flight to finish. A lower `timeout_seconds` ends the operation when it expires.

### Applying an edit

A completed `suggest` with `status` `closed` returns `patch`. To apply it,
replace the UTF-8 bytes from `start_utf8_offset` to `end_utf8_offset` with
`replacement`. A `sorry2lemma` with `source_changed` returns the whole
`transformed_source`. To apply either one:

1. Check that your copy of the file hashes to `source_sha256`. If it doesn't,
   don't apply the result.
2. Check that the result hashes to `patched_source_sha256` or
   `transformed_source_sha256`.
3. Upload it with `PUT /files`, with `expected_revision` set to the operation's
   revision.

A `409` revision conflict means the workspace changed meanwhile, so rerun
the operation. The Python SDK's `leanwarp_cloud.edits.edited_source` performs
these checks.

## Worker resources and billing

LeanWarp selects the published worker when you create a workspace. Set
`max_spend_microusd` to limit its total spending, or change the cap in the
dashboard. Existing workspaces, and identical retries of a creation request,
keep their saved billing terms.

For example, `{"bundle_id": "…", "max_spend_microusd": 2000000}` creates a
workspace capped at $2, including reserved credit. The cap is an integer from
zero to 1,000,000,000,000 microdollars, or `null` for no workspace cap.

The CLI and MCP `resources` command reports one `worker`: its
`starting_minute_microusd`, `usage_based`, `hold_microusd` and resource limits,
plus `idle_seconds`. CPU values are physical cores (one physical core is two
vCPUs). Charges are billed by the second. One dollar is 1,000,000 microdollars.

When `usage_based` is true, CPU and RAM charges rise independently above their
reserved baselines. The starting rate remains a paid minimum while the worker
runs, including idle time. Bursting depends on available host capacity. When
`usage_based` is false, the starting minute quote is the fixed rate for the time
the worker runs. The raw HTTP catalog keeps the compatibility fields described
below.

Stop the worker yourself when you finish. Disconnecting HTTP does not stop
billing. Stopping discards in-memory state but keeps uploaded files and
completed results. The next operation rebuilds the worker. Unused reserved
credit is released after shutdown and final usage are confirmed.

## Feedback

`POST /feedback` sends the LeanWarp team a report about the service itself. It
needs an `Idempotency-Key` and accepts a key with any permission. It is free and
starts no worker:

```json
{
  "kind": "bug",
  "severity": "worked_around",
  "summary": "try_tactics returned ambiguous_cursor_capture on a plain by block",
  "details": "What you did, what you expected and what happened.",
  "workspace_id": "…",
  "operation_id": "…"
}
```

`kind` is `bug`, `missing_feature`, `confusing_result`, `docs`, `performance` or
`other`. `severity` is `blocking`, `worked_around` or `minor` (the default). The
summary is one line of 8 to 200 characters. `details` holds up to 4,000.

Name the workspace and operation the report is about instead of quoting source.
A workspace or operation your account does not hold is dropped. A key limited to
one workspace always reports about that workspace.

The reply is `201` with `feedback_id`, `duplicate` and `occurrences`. The same
kind and summary within a day counts as another occurrence of the first report.
Resending a request with the same `Idempotency-Key` within a day returns its
first reply and counts nothing again.

An account can send 60 feedback requests an hour and 300 a day, repeats and
refused reports included. Of these, 20 an hour and 100 a day may be new reports.
Beyond that, the reply is `feedback_limit_exceeded`. A request the API rejects
before it reaches storage, such as a malformed body, stores nothing and is not
counted. A report expires 180 days after its last repeat and is not shown after
that. An account keeps at most 500. Deleting a workspace deletes the reports
about it. Deleting reports never resets these limits. The SDK sends a report
once and never retries it.

## Writes are safe to retry

Creating a workspace, uploading files and submitting operations require an
`Idempotency-Key` header of 8 to 200 printable ASCII characters. Resending the
same request with the same key returns the original result instead of acting
twice. Use a new key for each new request.

LeanWarp keeps a window of finished history, not all of it (see
[limits](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#credentials-credit-and-limits)).
A replay within an hour of the original always returns its result. Later, a
submission whose operation was removed returns `operation_expired` for at least
7 days, and is not run again. Reading that operation returns `not_found`.

Uploads and submissions also carry `expected_revision`, the revision you last
saw. If someone else changed the workspace since then, the request fails with
`revision_conflict` instead of overwriting their work.

## Errors

A rejected request has this shape:

```json
{"error": {"code": "revision_conflict", "message": "The workspace changed since your last sync, …"}}
```

`code` is stable, so branch on it. `message` explains what happened and what to
do next. It may change. Common codes:

| Code | Status | Meaning |
| --- | --- | --- |
| `authentication_required` | 401 | Missing, invalid, expired or revoked key. |
| `leanwarp_key_scope_denied` | 403 | The key lacks the permission, or is limited to another workspace. |
| `invalid_request` | 422 | The body doesn't match the API. The message names the field. |
| `invalid_cursor` | 422 | The page cursor was altered. Pass `next_cursor` unchanged. |
| `unsupported_bundle` | 422 | The environment isn't served. The wire code keeps its original name for compatibility. |
| `revision_conflict` | 409 | `expected_revision` is out of date. |
| `workspace_busy` | 409 | Another operation is running in the workspace. |
| `idempotency_conflict` | 409 | The `Idempotency-Key` was used for a different request. |
| `operation_expired` | 409 | The request already ran and its result is no longer stored. It was not run again. |
| `operation_limit_exceeded` | 422 | The workspace reached the account's operation limit (1,000 by default) within the last hour. |
| `operation_storage_limit_exceeded` | 422 | Running operations and stored results reached the account's limit (64 MiB by default). |
| `source_history_limit_exceeded` | 422 | The workspace reached the account's sync limit (1,000 by default) within the last hour, or stored source reached the account's limit (64 MiB by default). |

## Results and failed operations

An accepted operation runs after the request returns. When it ends in state
`failed`, it carries an `error_code` and an `error_message`. For example:

- `leanwarp_insufficient_credit` means there wasn't enough credit to start or
  renew a worker.
- `leanwarp_budget_exceeded` means the workspace reached its spending cap.

A denied elastic renewal can stop an operation that is already running. The
failure keeps the financial reason.

A `completed` operation can still report a failed check or a rejected proof, so
read `result.result.status`. A verification passed only when its status is `ok`
and its receipt's `policy` is `fixed_target_kernel_check_v1`.

`result` holds the receipt's `operation_id`, `revision` and `generation`. The
operation's own fields are under `result.result`, and the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#results)
lists them. Completed operations also have `execution`, which says which worker
ran them and what was reused, and `timing`, with `queued_ms`, `worker_ms`,
`upload_ms` and `run_ms`. After dispatch recovery, `queued_ms` is absent rather
than an estimate of earlier attempts. Clients should tolerate absent timing.

A failure to prepare dependencies is also a completed receipt, for any operation
kind. It has `status: error`, `diagnostics` explaining the failure, and
`diagnostics_complete: false`. It contains no successful tactic trials.

Held operation reads count against the API's per-account and overall request
limits. A busy service returns HTTP 503 with `Retry-After`. If a proxy cannot
hold the connection, the SDK falls back to ordinary polling, within the original
wait deadline.

## SDK releases

Every response under `/v1/leanwarp` carries `LeanWarp-SDK-Latest` and
`LeanWarp-SDK-Minimum`: the newest SDK release, and the oldest one the service
accepts. A request is refused with `426` and `sdk_upgrade_required` when its
`User-Agent` is the SDK's (`leanwarp-sdk/VERSION (cli|mcp|python)`) and older
than the minimum. Other clients are never refused for their version.

## Historical wire compatibility

`LeanWarpCloud.resources()` and raw `GET /resources` still return the release
billing catalog under `resources`, with `default_resource_profile` and
`idle_seconds`. Ordinary CLI and MCP commands show only that published default.
Older APIs without the default field use the retained `standard` row. Clients
must not pick an arbitrary catalog entry. Catalog names remain identifiers for
stored requests and billing snapshots, not customer choices.

For existing integrations and automation, workspace creation still accepts
`resource_profile` and `max_resource_profile`:

- Omitting the selection uses the published default.
- Omitting the maximum uses the selected worker's resources.
- If an explicit maximum cannot fit the default, creation selects that maximum.
- An explicit selection must fit its maximum.

Resource limits use CPU and RAM ceilings for elastic workers. They do not change
billing policy. On recovery, resend the exact original body and idempotency key,
including these fields when the original request used them.

Fixed billing rows keep `minute_microusd` and `hourly_microusd`.
`elastic_consumption_v1` rows keep `minimum_minute_microusd`,
`maximum_minute_microusd`, `minimum_hourly_microusd`, `maximum_hourly_microusd`
and `hold_microusd`. Historical fixed snapshots keep their original accounting
rules.

Deprecated CLI `--profile` and `--max-profile` flags still parse, for existing
automation. An operation override for `check`, `inspect` or `try_tactics`
replaces the warm worker and its in-memory state. Uploaded files and completed
results remain. The replacement stays warm until stopped. For `verify_target`,
an override uses a temporary worker that stops afterwards. These replacement
rules predate elastic billing. New clients should let LeanWarp select the worker
and set a spending cap instead.

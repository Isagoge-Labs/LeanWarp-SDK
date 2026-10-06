# HTTP API

The SDK handles uploads, revisions, retries and recovery for you, so most users
never call the API directly. Use it when you are building your own client.

Base URL: `https://api.isagoge.in/v1/leanwarp`

Authenticate every request with an API key from the
[dashboard](https://isagoge.in/dashboard):

```http
Authorization: Bearer lw_live_…
```

## Workflow

1. `GET /versions` lists the served environments. Pick one whose `lean_toolchain`
   and dependencies suit your files.
2. `POST /workspaces` with `{"bundle_id": …}` creates a workspace at revision `0`.
   No compute starts.
3. `PUT /workspaces/{workspace_id}/files` uploads `.lean` files and returns the new
   `revision`.
4. `POST /workspaces/{workspace_id}/operations` submits an operation against that
   revision and returns `202 Accepted` with an `operation_id`.
5. `GET /operations/{operation_id}?wait_seconds=20` until `state` is `completed`,
   `failed` or `cancelled`. The server holds each read until the operation ends
   or `wait_seconds` (0 to 20) pass, so you don't need to poll quickly.
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
| `GET` | `/versions` | read | Served environments, one current build each. |
| `GET` | `/resources` | read | Worker sizes and their rates. |
| `GET` | `/account` | read | Balance, reserved and available credit, in microdollars. |
| `POST` | `/workspaces` | write | Create a workspace. |
| `GET` | `/workspaces` | read | List the workspaces your key can see. |
| `GET` | `/workspaces/{workspace_id}` | read | A workspace's state and current revision. |
| `PUT` | `/workspaces/{workspace_id}/files` | write | Upload or delete files; returns the new revision. |
| `POST` | `/workspaces/{workspace_id}/operations` | execute | Submit `check`, `inspect`, `try_tactics` or `verify_target`. |
| `GET` | `/operations/{operation_id}` | read | An operation's state and result; `?wait_seconds=N` waits up to 20 seconds for it to end. |
| `POST` | `/operations/{operation_id}/cancel` | execute | Ask to cancel; poll until it ends. |
| `POST` | `/workspaces/{workspace_id}/stop` | write or execute | Stop the worker; files are kept. |
| `DELETE` | `/workspaces/{workspace_id}` | write | Delete a workspace. |

A key can be limited to one workspace; it then sees only that workspace.

## Writes are safe to retry

Creating a workspace, uploading files and submitting operations require an
`Idempotency-Key` header of 8 to 200 printable ASCII characters. Resending the same
request with the same key returns the original result instead of acting twice.
Use a new key for each new request.

Uploads and submissions also carry `expected_revision`, the revision you last
saw. If someone else changed the workspace since, the request fails with
`revision_conflict` instead of overwriting their work.

## Errors

A rejected request has this shape:

```json
{"error": {"code": "revision_conflict", "message": "The workspace changed since your last sync, …"}}
```

`code` is stable; branch on it. `message` explains what happened and what to do
next, and may change. Common codes:

| Code | Status | Meaning |
| --- | --- | --- |
| `authentication_required` | 401 | Missing, invalid, expired or revoked key. |
| `leanwarp_key_scope_denied` | 403 | The key lacks the permission or is limited to another workspace. |
| `invalid_request` | 422 | The body doesn't match the API; the message names the field. |
| `unsupported_bundle` | 422 | The environment isn't served. The wire code keeps its original name for compatibility. |
| `revision_conflict` | 409 | `expected_revision` is out of date. |
| `workspace_busy` | 409 | Another operation is running in the workspace. |
| `idempotency_conflict` | 409 | The `Idempotency-Key` was used for a different request. |

## Results and failed operations

An accepted operation runs after the request returns. When it ends in state
`failed`, it carries an `error_code` and an `error_message`. For example,
`leanwarp_insufficient_credit` means there wasn't enough credit to start a worker,
and `leanwarp_budget_exceeded` means the workspace reached its spending cap.

A `completed` operation can still report a failed check or a rejected proof: read
`result.result.status`. A verification passed only when its status is `ok` and its
receipt's `policy` is `fixed_target_kernel_check_v1`.

`result` holds the receipt's `operation_id`, `revision` and `generation`, and the
operation's fields under `result.result`; the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#results)
lists them. Completed operations also have `execution`, saying which worker ran
them and what was reused, and `timing`, with `queued_ms`, `worker_ms`,
`upload_ms` and `run_ms`. After dispatch recovery, `queued_ms` is absent rather
than an estimate of earlier attempts. Clients should tolerate absent timing.

A dependency preparation failure is also a completed receipt with `status: error`,
`diagnostics` explaining the failure, and `diagnostics_complete: false`, for any
operation kind. It contains no successful tactic trials.

Held operation reads share the API's per-account and overall request limits. A
busy service returns HTTP 503 with `Retry-After`. The SDK falls back to ordinary
polling if a proxy cannot hold the connection, within the original wait deadline.

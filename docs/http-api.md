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
6. `POST /workspaces/{workspace_id}/stop` ends billing for the current workspace generation. A running worker returns `202` with `state: stopping`, its `generation`, and `charge_cutoff_at`. Poll the workspace until it is `stopped`. A workspace with nothing to clean up returns `200`.

Steps 3 to 5 can be one request: `POST /workspaces/{workspace_id}/run` takes the
files to upload or delete (`files`, `delete_paths`) with the operation (`kind`,
`payload`, and optionally `resource_profile`, `timeout_seconds` and
`retention`). `timeout_seconds` limits running the operation; starting a worker
does not count against it (see below). It returns the new `revision` and the
`operation`. Add `?wait_seconds=20` to hold it until the operation ends. One
`Idempotency-Key` covers the upload and the operation; resending replays
whichever already happened. Use a new key for each different
request: the upload commits first, so files sent under a key already used for
another operation are committed before that conflict is reported. After a
rejected request, `GET /workspaces/{workspace_id}/files` shows whether the upload
was committed. Uploading needs write permission as well as execute. Without files
it only submits.

```sh
curl -s https://api.isagoge.in/v1/leanwarp/workspaces/$WORKSPACE/operations \
  -H "Authorization: Bearer $LEANWARP_API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"kind": "check", "expected_revision": 1, "payload": {"file": "Main.lean"}}'
```

That is a strict check: it passes only without `sorry`, Lean errors or extra
axioms. Send `"strict": false` for a faster draft check while editing.

## Endpoints

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| `GET` | `/versions` | none | Served environments, one current build each. A key is optional, but one you send must be valid. |
| `GET` | `/resources` | read | Published worker resources and billing metadata. |
| `GET` | `/account` | read | Balance, reserved and available credit, and credit returned after LeanWarp's own failures, in microdollars. |
| `POST` | `/workspaces` | write | Create a workspace. |
| `GET` | `/workspaces` | read | List the workspaces your key can see. |
| `GET` | `/workspaces/{workspace_id}` | read | A workspace's state and current revision. |
| `GET` | `/workspaces/{workspace_id}/files` | read | The current revision and each file's SHA-256, without contents. |
| `PUT` | `/workspaces/{workspace_id}/files` | write | Upload or delete files. Returns the new revision. |
| `POST` | `/workspaces/{workspace_id}/operations` | execute | Submit an operation. See [Operations](#operations). |
| `POST` | `/workspaces/{workspace_id}/run` | execute, and write to upload | Upload files and submit an operation on the new revision. `?wait_seconds=N` waits up to 20 seconds for it to end. |
| `GET` | `/workspaces/{workspace_id}/operations` | read | Stored operations with results, newest first. See below. |
| `GET` | `/workspaces/{workspace_id}/usage` | read | Each history limit's `used` and the account's `limit`. |
| `GET` | `/operations/{operation_id}` | read | An operation's state and result. `?wait_seconds=N` waits up to 20 seconds for it to end. |
| `GET` | `/operations?ids=A,B,…` | read | Up to 100 operations at once. `&wait_seconds=N` returns as soon as any of them ends, waiting up to 20 seconds. See [Waiting on many operations](#waiting-on-many-operations). |
| `POST` | `/operations/{operation_id}/cancel` | execute | Ask to cancel, then poll until it ends. |
| `DELETE` | `/operations/{operation_id}` | write or execute | Delete a finished operation's stored request and result. See [Releasing results](#releasing-results). |
| `POST` | `/workspaces/{workspace_id}/stop` | write or execute | End billing and request cleanup. An optional `generation` query parameter scopes a retry to the generation from the first answer. A retry returns the saved close even if new work has queued. Disconnect sends `require_idle=true` to check for active work on that generation. Files are kept. |
| `DELETE` | `/workspaces/{workspace_id}` | write | Delete a workspace. |
| `POST` | `/feedback` | any | Report a problem with LeanWarp itself. See [Feedback](#feedback). |

A key can be limited to one workspace. It then sees only that workspace: another
workspace's operations answer `not_found` to it.

Each operation records which key submitted it. In the dashboard, the account's key
list shows each key's `last_used_at`, recorded to the hour, and
`GET /account/key-usage?hours=N` (signed-in account, 1 to 2,160 hours, default 24)
returns each key's `operations` submitted and the `worker_seconds` they held a
worker over the last `N` hours. Per-key use is kept for 90 days, even after the
operations themselves are deleted.

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
start at 1, and columns count characters (Unicode code points).

| `kind` | `payload` |
| --- | --- |
| `check` | `file`, `strict` (default `true`; `false` is a draft check, in which `sorry` is only a warning). |
| `inspect` | `file`, `line`, `column`. Select a proof hole or a tactic in a proof. |
| `try_tactics` | `file`, `line`, `column`, `tactics` (1 to 32), `timeout_ms` per tactic (default 30000). Near the operation's limit, tactics tried together share the time left, and those with no time left are listed in `skipped`. |
| `verify_target` | `file`, `candidate_declaration`, `target_statement`, `target_context` (it may import only the environment's packages, never the workspace's modules), `execution_mode` (`reusable` or `fresh`), `timeout` seconds. |
| `holes` | `file`, `goals` (default `true`: bounded goals for the first 16 unfinished or unknown placeholders). |
| `suggest` | `file`, `line`, `column`. Optional `tactics` (1 to 32) to use instead of LeanWarp's portfolio, and `timeout_ms` per tactic (learned budgets by default). Near the operation's limit, tactics tried together share the time left. |
| `extract_decls` | `file`. |
| `sorry2lemma` | `file`, `max_extractions` (1 to 256, default 64), optional `name_prefix`, `strict_final_check` (default `false`). |
| `have2lemma` | `file`, `max_extractions` (1 to 256, default 64), optional `name_prefix`. |
| `simplify_proofs` | `file`. Optional `tactics` (1 to 32, without `sorry` or `admit`) instead of LeanWarp's default list, `timeout_ms` per attempt (1 to 60000, default 10000), `max_simplifications` (1 to 256, default 16), `min_reduction_bytes` (default 1). |
| `repair_proofs` | `file`. Optional `tactics` (1 to 32, without `sorry` or `admit`) instead of LeanWarp's default list, `timeout_ms` per attempt (1 to 60000, default 10000), `max_repairs` (1 to 256, default 16). The operation attempts repairs within its time budget and repair limit; remaining holes can be `skipped`. Each theorem it edits must end complete. |
| `check_files` | `files` (1 to 64 distinct paths), `strict` (default `true`), optional `timeout` seconds per file (default 60), which starts once the file's imports have loaded, except in a file using `native_decide`. It must fit the operation's budget for its entries (see below). |
| `verify_batch` | `codes`: 1 to 64 objects `{"custom_id", "proof"}`. Each `custom_id` is unique and 1 to 128 characters, without control characters. Proofs must not be blank. The payload is at most 256 KiB of UTF-8. Also `strict` (default `true`) and optional `timeout` seconds per proof (default 60), which starts once the proof's imports have loaded, except in a proof using `native_decide`. It must fit the operation's budget for its entries (see below). The proofs run in the workspace's environment. They may import its packages, such as Mathlib, but not the workspace's own modules. Such a proof is refused without running: `status` `rejected`, `failure.kind` `policy`, `error.code` `unsupported_import`. A code may add `target`: `{"context", "statement", "declaration"}` (see below). |

Any `payload` may also carry `lean_options`, an object of Lean options applied as
a lakefile's `leanOptions` would be, such as `{"maxSynthPendingDepth": 3}`. Options
that could weaken checking are refused.

An operation's `timeout_seconds` limits execution to 1–600 seconds. The default
depends on the operation. For example, `check` and `inspect` default to 120 seconds,
and `suggest`, `simplify_proofs`, `repair_proofs`, `check_files` and
`verify_batch` to 600.
For `try_tactics` with N submitted tactics the default in seconds is
`min(600, max(120, 30 + ceil((N × timeout_ms + 100) / 1000)))`.
A per-tactic timeout above 60,000 ms is capped at 60,000 ms before calculating
the default limit or running a race. Each trial reports its `effective_timeout_ms`.
An explicit limit shorter than the sum scales the per-tactic budgets down.
Whitespace-equivalent tactics share a trial and each input position receives its
outcome. When the remaining time cannot fund a race, every input appears in
`skipped` with reason `operation_budget_exhausted`. A zero-duration engine
timeout appears in `results`.

Inspection returns `selected_hole` or `selected_tactic` and one complete list
in `goal_contexts`. With `goal_status: no_goals` Lean observed an empty tactic
state. With `omitted`, `context_omitted.goal_contexts` counts the hidden goals.
Both have public `status: unavailable`. A usable observation has
`status: metadata_only`, `observation_only: true` and `retained_proof_state: false`.
An unknown draft hole may recover goals by capture, with `observation_only: false`.

Every stage shares that one limit: preparing the file's local imports,
capturing its proof state, checking it and running tactic trials. A check or
verification whose Lean run reaches it returns a `timeout` non-verdict, retryable
with more time. A batch keeps the entries it finished (see below). Otherwise,
including a check that reaches it while its imports are still being prepared,
the operation fails with `operation_timeout`.

Starting a worker does not count against `timeout_seconds`, so a file gets the
same limit on a warm worker as on a cold one. A start has its own limits. It
fails with `capacity_unavailable` if it is still waiting for capacity 10 minutes
after the operation's submission, queueing included (whenever the service
reclaims an operation and must start a worker for it, the wait starts afresh).
It then has up to 15 minutes to create the worker and 5 more for it to become
ready (`worker_start_timeout`). Both failures are retryable, and nothing ran.
Uploading files counts against neither limit.

`suggest`, `check_files` and `verify_batch` stop starting new work after four
minutes. Their default `timeout_seconds` is 600, enough for the work still in
flight to finish. A lower `timeout_seconds` stops starting new work ten seconds
before it expires, or halfway through a limit under 20 seconds, so finished
results still return. With a limit of one or two seconds, `check_files` and
`verify_batch` run at most one source. Any they do not reach are `not_checked`.

Sources that share the same imports run together on one worker, so results keep
your order but the order they ran in may differ. Cancelling either operation keeps
the entries that finished: `state` is `cancelled`, `result` holds them, and the
rest are `not_checked`. A batch still running when its `timeout_seconds` expires
keeps them the same way, with `state` `completed`.

`native_decide` runs in its own fresh Lean process, so a source that uses it
loads its imports again within its `timeout`. Allow for that when setting one.

Each entry is promised its `timeout`, or 60 seconds without one. A `timeout`
larger than the operation's budget for its entries (`timeout_seconds` less 40
seconds, and at most 210) is refused at submission, so with the default
`timeout_seconds` of 600 an entry can have up to 210 seconds. An entry that had
its whole budget and did not finish is `timeout`. One that started with less, because the
batch's time ran short, and did not finish is `not_checked`, not `timeout`:
retry it on its own. An entry on the default 60 seconds under a shorter
`timeout_seconds` can come back `not_checked` this way.

### Binding a proof to its statement

Without a `target`, a `verify_batch` entry's `ok` means this source compiles, its
declarations pass Lean's kernel, and it uses no axiom beyond `propext`,
`Classical.choice` and `Quot.sound`. It does not mean the source proves what you
meant: `theorem x : True := trivial` is `ok`. To fix the statement, add
`target`: `context` (the imports and definitions the statement needs, such as
`import Mathlib`; only the environment's packages), `statement` (a closed Lean
`Prop`) and `declaration` (the theorem the proof introduces). The entry is then
checked as `verify_target` would check it, and an accepted entry carries the
checker's `receipt`. A refused one is `rejected` with `failure.kind`
`target_mismatch`, `declaration_missing`, and so on. `target` requires
`"strict": true`; a draft batch with one is refused at submission. The context
and statement count toward the 256 KiB payload.

```json
{"custom_id": "p1", "proof": "theorem t (n : Nat) : n + 0 = n := rfl",
 "target": {"context": "", "statement": "∀ n : Nat, n + 0 = n", "declaration": "t"}}
```

### Releasing results

Stored results count against the account's storage limit until history makes
room for them, an hour after they finish. A client that keeps its own copy can
release them sooner: read the result, then `DELETE /operations/{operation_id}`.
That deletes the finished operation's request and result at once and answers
`204`; repeating it also answers `204`. A queued or running operation is refused
with `operation_active`; cancel it or wait. Reading an operation never deletes it.

Resubmitting a deleted request with the same `Idempotency-Key` answers
`operation_expired` and does not run it again.

A `check`, `inspect` or `try_tactics` can instead ask for short retention: send
`"retention": "short"` with the operation. Its result is then eligible for
removal about 60 seconds after it finishes, instead of an hour, so a search loop
stays within its limits without a `DELETE` for each result. This is not an
erasure time: history removes results only when it needs room, so a quiet
workspace may keep one longer, and each workspace's newest operation is always
kept. Until it is removed, resending the request returns the original result;
afterwards it answers `operation_expired`, as a deleted one does. Other kinds
refuse `"short"` with `invalid_request`. The default is `"standard"`.

### Waiting on many operations

`GET /operations?ids=A,B,…&wait_seconds=N` names up to 100 operations and holds the
request until any of them ends, or `N` seconds pass. The reply is
`{"operations": […], "unread": […]}`, in the order you listed them. A pending
operation appears without its result; a finished one in full, as
`GET /operations/{operation_id}` returns it. To keep the response near 1 MiB,
finished operations that do not fit are listed by ID in `unread` instead: read
each with its own `GET`; so are finished operations not yet read when the wait's
`N` seconds are up, as the wait never holds the request longer. If storage cannot
answer even the first read in time, the reply is `503`
`persistence_request_unavailable`; retry it. An operation your
key cannot see, or whose result was already deleted, is left out.

### Applying an edit

A completed `suggest` with `status` `closed` returns `patch`. To apply it,
replace the UTF-8 bytes from `start_utf8_offset` to `end_utf8_offset` with
`replacement`. A `sorry2lemma`, `have2lemma`, `simplify_proofs` or
`repair_proofs` with `source_changed` returns the whole `transformed_source`.
The last three return it only once LeanWarp's audit has accepted the rewritten
file. If the audit rejects it, the response gives the reason and marks the
proposed edits as rejected. It returns zero accepted edits and no replacements.
To apply an accepted edit:

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
plus `idle_seconds`. CPU and memory values describe the worker’s allocated resources
and limits. Charges are billed by the second. One dollar is 1,000,000 microdollars.

When `usage_based` is true, CPU and RAM charges rise independently above their
reserved baselines. The starting rate remains a paid minimum while the worker
runs, including idle time. Bursting depends on available host capacity. When
`usage_based` is false, the starting minute quote is the fixed rate for the time
the worker runs.

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
A replay returns the original result while it is stored. Unless you explicitly
delete it, history keeps a finished result for at least an hour, or a minute
with [short retention](#releasing-results). After deletion or removal by history,
replaying the submission returns `operation_expired` for at least 7 days.
Reading the removed operation returns `not_found`. `retention` is part of the
request: leaving it out is the same as `"standard"`, and resending a key with a
different `retention` is an `idempotency_conflict`.

Creating a workspace replays this way until 7 days after the workspace is
deleted; after that, the same key creates a new workspace.

Stop, cancel and delete requests need no idempotency key. To retry a stop,
specify the original workspace generation. A stop without a generation acts on
the current worker, which may have changed since the first request.
A repeated workspace delete answers
`not_found`, so after a lost response treat that as deleted. A repeated result
delete answers `204`.

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
| `operation_active` | 409 | The operation has not finished, so its result cannot be deleted yet. |
| `operation_limit_exceeded` | 429 | The workspace stores the account's limit of operations (1,000 by default). |
| `operation_storage_limit_exceeded` | 429 | Stored results, and the space held for queued and running operations, reached the account's limit (512 MiB by default). |
| `source_history_limit_exceeded` | 429 | The workspace reached the account's sync limit (1,000 by default) within the last hour, or stored source reached the account's limit (64 MiB by default). |
| `workspace_creation_limit_exceeded` | 429 | The account created its limit of workspaces within the last hour (300 by default). |

The four limits above free with time, so they answer `429` with `Retry-After`:
the seconds until enough records that count can be removed, an hour after each
finished or a minute for short retention (or the whole hour-long window when
nothing is waiting to leave it; for storage held only by running operations, 30
seconds). Deleting results you have read frees operation history and storage at
once. Stored source frees only when you delete files or workspaces, so waiting
does not help that case. An account's limits can be
raised; email contact@isagoge.in.

## Results and failed operations

An accepted operation runs after the request returns. When it ends in state
`failed`, it carries an `error_code` and an `error_message`. For example:

- `leanwarp_insufficient_credit` means there wasn't enough credit to start or
  renew a worker.
- `leanwarp_budget_exceeded` means the workspace reached its spending cap.

A denied elastic renewal can stop an operation that is already running. The
failure keeps the financial reason.

Worker time lost to a failure LeanWarp's own systems raise (its storage being
unavailable, no capacity to start a worker, or a LeanWarp update stopping the
worker) is credited back automatically, once the worker's billing settles: the
time at that worker's average charged rate (its charge divided by its charged
time), never more than the worker charged, and up to $20 per operation. Other
failures, such as a worker that exits, stops responding or stops because its
lease ran out, are not credited automatically; contact LeanWarp to have one
reviewed. After an incident on LeanWarp's side, LeanWarp credits the affected
accounts itself. `GET /account` reports the automatic
credits as `infrastructure_credit_microusd`. Time a worker spends starting is
never charged; charging starts when it is ready.

A `completed` operation can still report a failed check or a rejected proof, so
read `result.result.status`. Every verification result (a `check`, each
`check_files` and `verify_batch` entry, a `verify_target`) has one of six:

| `status` | Verdict? | Meaning |
| --- | --- | --- |
| `ok` | yes | Accepted. Strict: by the checker, which re-checks every declaration with Lean's kernel. Draft: Lean elaborated it. |
| `error` | yes | Lean reported errors; `diagnostics` are Lean's. |
| `rejected` | yes | Elaborated but refused; `failure.kind` says why. |
| `timeout` | no | It had its whole requested time and did not finish. |
| `infrastructure_failure` | no | LeanWarp could not complete the check. |
| `not_checked` | no | Never started, or started with less than its requested time and did not finish. |

`rejected`, `timeout` and `infrastructure_failure` carry `failure: {kind,
retryable, message}`. Rejection kinds: `axioms`, `kernel`, `policy`,
`target_mismatch`, `declaration_missing`, `target_elaboration`,
`candidate_elaboration`. Non-verdict kinds: `timeout`, `memory_limit`,
`resource_limit` and `internal`. A non-verdict says nothing about the proof:
retry it when `retryable` is true; do not change the proof because of it.
Lean's own limits are verdicts, since the same source always gives the same
answer: exceeding `maxHeartbeats` or `maxRecDepth` is `error` for a proof and
`rejected` with `target_elaboration` for a target. Only wall-clock time, memory,
native stack depth and a lost process are non-verdicts.

A batch's `status` is `ok` when every entry is, `failed` when at least one was
refused (`error` or `rejected`), and otherwise `incomplete`. `passed`, `failed`
(`error` and `rejected` only), `timed_out`, `infrastructure_failures` and
`not_checked` count every entry exactly once.

A verification passed only when its status is `ok` and its receipt's `policy`
is `fixed_target_kernel_check_v2`.

`result` holds the receipt's `operation_id`, `revision` and `generation`, and
`build`: the `bundle_id`, `engine_commit` and `environment_id` of the build that
produced it. The operation's `build` field names the same build as soon as the
operation is dispatched to a worker, while it runs, and is `null` until then. An
operation that was interrupted and rerun names the worker that reran it. A workspace's `bundle_id`
names its current build. After a build update, the next operation starts a new worker. The operation's own fields are under `result.result`, and the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#results)
lists them. Completed operations also have `execution`, which says which worker
ran them and what was reused, and `timing`, with `queued_ms`, `worker_ms`,
`upload_ms` and `run_ms`. On terminal operations, `timing.total_ms` measures
elapsed service time from enqueue until completion, failure, or cancellation,
including time spent in earlier attempts after recovery. When a worker was started for the operation,
`worker_ms` has parts, and may also include other work: `start_wait_ms`
(waiting for capacity to start one, after the wait in `queued_ms`), `create_ms`
(creating it) and `ready_ms` (waiting for it to answer). The worker also reports
its own measures of its start: `isolation_ms` (setting up resource limits and
metering), `project_ms` (preparing the project) and `engine_start_ms` (starting
the Lean engine). It times these itself, so they can overlap `create_ms` and
need not add up to `ready_ms`. Parts of `run_ms` appear when Lean timed them:
`elaboration_ms` (checks: importing and checking the file), with `imports_ms`
(loading the imports, such as Mathlib) inside it for draft and strict checks when measured (zero is a measurement),
`proof_state_ms` and `tactics_ms` (tactic trials: capturing the proof state,
then running the tactics). After dispatch recovery, `queued_ms` is absent rather than an estimate
of earlier attempts. Clients should tolerate absent timing.

A service update can interrupt an operation that is running. Most finish during
the update. Others continue or restart on a new worker automatically, so a result
can take several minutes longer; after repeated interruptions the operation fails
with `recovery_limit_exceeded` and you can retry it. A warm worker keeps running
through an update. A completed operation that was interrupted has
`execution.restarted`: `count`, and `reason` `release` (an update moved it) or
`worker_lost` (its worker or server stopped responding).

A failure to prepare dependencies is also a completed receipt, for any operation
kind, with `diagnostics_complete: false`. A local import that fails to build is
`status: error` with Lean's errors. One that stopped without them (a time
limit, the memory limit, a cancellation) is, for `check`, `verify_target` and
each `check_files` entry, the non-verdict it was (`timeout` or
`infrastructure_failure`), except that a `check_files` entry whose imports ran
out of time is `not_checked`; other kinds report `status: error` with its
`error.code`. It contains no successful tactic trials.

Held operation reads count against the API's per-account and overall request
limits. A busy service returns HTTP 503 with `Retry-After`. If a proxy cannot
hold the connection, the SDK falls back to ordinary polling, within the original
wait deadline.

## SDK releases

Every response under `/v1/leanwarp` carries `LeanWarp-SDK-Latest` and
`LeanWarp-SDK-Minimum`: the newest SDK release, and the oldest one the service
accepts. A request that would start work (creating a workspace, uploading or
submitting) is refused with `426` and `sdk_upgrade_required` when its
`User-Agent` is the SDK's (`leanwarp-sdk/VERSION (cli|mcp|python)`) and older
than the minimum. Reads, cancelling, stopping and deleting are always served, so
an outdated SDK can still collect results and stop billing. Other clients are
never refused for their version.

When a rise in the minimum is scheduled, every response to an SDK older than the
latest release also carries `LeanWarp-SDK-Sunset`: the date it takes effect, such
as `2026-12-01`. The Python library warns once with a `DeprecationWarning`, and the
CLI and MCP report it as `update_available.sunset`.

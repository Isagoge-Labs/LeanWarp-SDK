# Changelog

Update an installed SDK with `leanwarp update`.

## 0.2.8

- `check`, `inspect` and `try_tactics` take `retention="short"`: a finished
  result is then eligible for removal about 60 seconds after it finishes, so a
  search loop need not delete each one. The default stays `"standard"`.
- If `apply` reports a `revision_conflict`, it keeps your local files. Check
  whether the edit was written, then use `reconcile` to review what `sync`
  would upload or delete.
- `apply` run again after an upload whose response was lost reports `applied`
  when that upload landed, instead of asking to run the operation again.

## 0.2.7

- Every result says whether there was a verdict. A check, batch entry or
  verification is `ok`, `error` or `rejected` (a verdict), or `timeout`,
  `infrastructure_failure` or `not_checked` (no verdict, with
  `failure.retryable`). With no verdict, `decided` is `false` and the CLI exits
  `4`: retry rather than change the proof. A batch is `ok`, `failed` or
  `incomplete`.
- A `verify_batch` entry can carry a `target` (`context`, `statement`,
  `declaration`) to check it against that statement, with a receipt when
  accepted. Receipts use the policy `fixed_target_kernel_check_v2`, which
  `OperationOutcome.verified` requires.
- A `check` sent over HTTP is strict unless it says `"strict": false`.
- `suggest` no longer has a `partial` tier: run `sorry2lemma` to give each hole
  its own lemma, then `suggest` on it.
- A batch entry without `timeout` has 60 seconds. A `timeout` the operation
  cannot honour is refused when it is submitted.
- Lean options set in your `lakefile` apply to every check and verification.
  `leanwarp status` names any that are not applied.
- `check_files` and `verify_batch` run proofs that share imports together, keep
  the entries that finished when cancelled, and keep each failing entry's first
  error when results are shortened. A long Lean message keeps its start and its
  goals.
- A brief outage no longer ends a wait. `wait()` keeps polling through `429`,
  `5xx` and dropped connections, `stop`, `cancel` and `delete` are retried, and
  after a lost response the next command resends the request with the same key.
- Every error is a `LeanWarpError` with `code`, `status`, `message`,
  `retryable` and `retry_after`.
- `delete_operation(id)` frees a finished result's storage, and
  `wait_operations(ids)` waits on up to 100 operations at once.
- MCP tools can be called in parallel and take an `operation_id`. Results are
  compact, errors first; pass `detail: "full"` for everything.
- For CI: `leanwarp connect --ephemeral`, `leanwarp check --all` and
  `leanwarp stop --delete`, and `--block` to wait for any operation. The
  getting-started guide has a GitHub Actions recipe.
- `inspect` at a position that selects no single proof hole returns
  `cursor_outside_proof` or `ambiguous_cursor_capture`.
- Connecting from a partial checkout keeps the workspace's other files, listed
  under `remote_only`; `leanwarp prune` deletes them once you confirm.
- `leanwarp setup --remove` undoes `leanwarp setup`, and `LEANWARP_FEEDBACK=off`
  turns off feedback.
- The agent skill starts with setup (install, sign in, connect), so an agent can
  set LeanWarp up from https://isagoge.in/leanwarp/skill.md.
- The SDK runs on Python 3.10 to 3.14 and accepts older releases of its
  dependencies.
- When support for your SDK release is due to end, results show the date under
  `update_available`.

## 0.2.6

- Changed files and the operation that uses them now go to the service in one
  request, so checks and other operations start sooner.
- The HTTP API reference covers `POST /workspaces/{workspace_id}/run`, which
  uploads files and submits an operation together.
- The reference explains how `timeout` works in `check_files` and `verify_batch`:
  it starts once imports have loaded, except for sources using `native_decide`,
  and a short `timeout_seconds` still returns finished results.
- Results now document `engine_` errors on a single batch entry and the
  `elaboration_ms`, `proof_state_ms` and `tactics_ms` timings.
- The getting-started guide explains how to restate Lean options from your
  `lakefile` as `set_option` lines.

## 0.2.5

- The MCP server is listed in the official
  [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.Isagoge-Labs/leanwarp`.
- The documentation is now at
  [isagoge.in/leanwarp/docs](https://isagoge.in/leanwarp/docs). The README and
  the package's links point there.
- The getting-started guide, agent setup, reference and HTTP API pages are
  rewritten to be shorter and simpler.

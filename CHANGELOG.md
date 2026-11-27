# Changelog

Update an installed SDK with `leanwarp update`.

## 0.2.10

- Connect plain Lean files to a ready environment, or connect a Lake project to
  its pinned dependencies. Preparing a new project environment uses prepaid
  credit when builds are available. Reusing a ready environment is free.
- Run `leanwarp update` to upgrade from 0.2.9 or earlier. For a previously
  connected project, run `leanwarp stop`, `leanwarp disconnect`, then
  `leanwarp connect` to create a workspace in its new environment. The new service lists toolchains at `/versions` and creates
  workspaces using an account-owned `environment_id`.
- `connect` waits up to four hours for a build by default; `--timeout SECONDS`
  (`timeout=` in Python) changes it. When the wait ends first, `ConnectTimeout`
  is raised and the CLI exits with code 3. Run `connect` again in the same project
  to start the build or rejoin one already in progress. Exit code 3 still also means an
  operation is running: run `leanwarp wait`, never submit it again.
- MCP `connect` returns `state: building` after about 40 seconds while a build
  goes on; call it again to keep waiting.
- `connect --retry` builds a failed environment again once its cause is fixed.
  After `stored_file_missing` it also uploads the missing file again.
- A lakefile that reads files outside the project gets them from `connect`,
  which must keep running while the build asks: only inside path dependencies
  and the directories listed in `.leanwarpread`. A project that a worker cannot
  load the same way from `lean-toolchain`, the lakefile and `lake-manifest.json`
  alone, such as one whose lakefile reads other files when Lake loads it, is
  refused with `configuration_not_reproducible`.
- Each dependency's default libraries are built; importing a module from its
  other libraries is refused with an error naming it.
- Private GitHub dependencies are connected in the dashboard with a GitHub
  account that administers them; until then, connecting returns
  `grant_required`. Paused access keeps your workspaces, files and results.
- A path dependency sends its files except what its own top-level `.gitignore`
  and `.leanwarpignore` leave out, build outputs, version control and
  credential files (`.env*`, `.npmrc`, SSH private keys, `*.pem`, `*.key` and
  similar), which are never uploaded or sent; a build that reads a file its
  ignore files leave out is refused. `leanwarp files --dependencies` (MCP
  `files` with `dependencies`) lists the files `connect` sends.
- A package URL in `lake-manifest.json`, `.lake/package-overrides.json` or a
  path dependency's own manifest, or a lakefile URL, with a credential in it is
  refused before anything is sent;
  connect private GitHub repositories in the dashboard instead.

- `verify_many` checks a dataset of proofs, each against its own statement,
  in chunks of up to 64 spread over several workspaces. Verdicts stream to a
  JSONL file as they arrive, each with the item's stable `id` and the
  `target_sha256` of the statement it was checked against. Run it again with
  the same output to resume: written items are skipped and an interrupted
  chunk is read back or, within 6 days, replayed, never charged twice; an
  older one runs again only with `rerun_lost=True`. A written verdict is kept
  only for the same proof, target and settings (its `input_sha256`); a resumed
  run that changes any of them is refused. An output is verified in one
  environment, named by `environment_id` on each line: once its first proof
  chunk is journaled or a verdict written, a resumed run in another is refused.
  Each line's `decided` is false for `timeout`,
  `infrastructure_failure` and `not_checked`, which the summary counts as
  `undecided`, not `failed`; resuming skips them, so retry them in a new
  output. One run at a time uses an output. Items need a `target` unless
  `require_target=False`.
- `AsyncLeanWarpCloud`: every client method as a coroutine, with the same
  retries and deadlines.
- `proof_states` (`leanwarp proof-states`, MCP `proof_states`) lists every
  tactic in a file with its goals before and after, a page per call, instead of
  an `inspect` per tactic.

## 0.2.9

- Three proof rewrites, each written into the file by `apply`:
  `have2lemma` moves each named `have` into its own lemma, `simplify-proofs`
  replaces theorem proofs with shorter ones that still check, and
  `repair-proofs` attempts to fill `sorry` holes within the repair limit and
  time budget. LeanWarp returns the rewritten file only once it has no errors,
  keeps every statement, and each theorem repaired or simplified is complete;
  otherwise `status` is `rejected` with a `reason`.
- The CLI's `check`, `inspect` and `try-tactics` take `--retention short`, and
  the MCP tools `check`, `inspect` and `try_tactics` take `retention`.
- Inspect a tactic in a completed proof or a proof hole. `no_goals` identifies
  a known empty tactic state. `omitted` counts hidden goals. Compact MCP
  answers preserve complete selections and goals when they fit.
- Tactic trials share one capture and race. The default limit sums their
  budgets, adds 100 ms, rounds up to seconds and adds 30 seconds, bounded to
  120–600 seconds. A shorter explicit limit scales the budgets down. An identical
  retry after an upgrade reuses the accepted operation even if the default changed.
- If every tactic was skipped, `try-tactics` reports no verdict (CLI exit `4`).
  If a trial ran, read its outcome to see whether it closed the goal.
- A workspace stop can return `stopping`. Billing for that generation has ended
  while cleanup finishes. The Python client sends a stop without a generation
  once. Pass the workspace generation to retry the same stop safely.
- Draft and strict checks report `timing.imports_ms` when Lean measured import loading.

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
- `suggest` no longer has a `partial` tier. A suggestion must close its
  declaration even if other declarations still have holes.
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

# LeanWarp reference

## Commands

Run commands from the Lean project, or pass `leanwarp --project DIR`. Every
command prints one JSON object. `leanwarp COMMAND --help` lists its options.

A **workspace** holds the project's uploaded files and results on LeanWarp. A
**worker** is the server that runs Lean for it. Each upload makes a new
**revision**.

| Command | What it does |
| --- | --- |
| `auth login [--no-browser]`, `auth logout` | Open the dashboard to create a key, then save the key you paste. `logout` forgets the key on this machine. |
| `setup [--agent NAME] [--no-mcp]` | Install the skill and MCP server for Claude Code, Codex, Cursor and `~/.agents`. No key needed. |
| `update` | Upgrade `leanwarp` to the latest release and refresh the skills `setup` installed. |
| `doctor [--environment NAME]` | Show which environment the project runs on, and summarise the upload. Starts no worker. Before you sign in, it checks against the public catalog. |
| `files` | List the Lean files that would upload, their sizes and any problems. No key needed. |
| `connect [--environment NAME] [--max-spend USD]` | Create the project's workspace. `--max-spend` sets a spending cap. Starts no worker. |
| `connect --workspace ID` | Connect the project to an existing workspace instead, such as one from another machine. Reports what the next sync uploads or deletes. Starts no worker. |
| `check FILE [--draft]` | Compile a file and report Lean's errors and warnings. |
| `inspect FILE --line N --column N` | Show the goals and local context at a position. |
| `try-tactics FILE --line N --column N --tactic T [--tactic T …]` | Try tactics without editing the file. |
| `verify FILE --declaration NAME --target STATEMENT [--context-file PATH]` | Check that a declaration proves exactly the given statement. |
| `holes FILE [--no-goals]` | List every `sorry` and `?_` with its position, and the goals of the first 16. |
| `suggest FILE --line N --column N [--tactic T …] [--tactic-timeout MS]` | Find a tactic that closes the hole at a position. Never edits the file. |
| `apply` | Write the latest `suggest` or `sorry2lemma` result into its file and upload it. |
| `sorry2lemma FILE [--max N] [--prefix NAME] [--strict-final-check]` | For each theorem that still has a `sorry`, move its proof into a named lemma, and make the theorem use that lemma. Never edits the file. |
| `declarations FILE` | List the declarations a file adds, with types and dependencies. |
| `check-files FILE [FILE …] [--draft] [--file-timeout SECONDS]` | Check up to 64 files in one operation. Strict unless `--draft`. |
| `verify-batch FILE.jsonl [--draft] [--proof-timeout SECONDS]` | Check up to 64 inline proofs in one operation. Strict unless `--draft`. Each line of the file is `{"custom_id": "...", "proof": "..."}`. Pass `-` to read standard input. The proofs are not uploaded as files. |
| `wait [--timeout SECONDS]` | Wait for the latest operation's result. |
| `status` | Show the workspace, the latest operation and the workspace's use of each history limit. |
| `workspaces` | List your account's workspaces. `this_project` marks this project's workspace. |
| `export --output FILE [--workspace ID]` | Save every stored operation and result to a new file, as JSON Lines, newest first. |
| `cancel` | Cancel the latest operation. |
| `stop` | Stop the worker. Files are kept. |
| `feedback --kind KIND --summary TEXT [--details TEXT] [--severity S] [--operation ID]` | Report a confirmed problem with LeanWarp itself to its team. The report names the latest operation. Free, and starts no worker. |
| `recover` | Resend a request whose response was lost. |
| `reconcile` | After another client changed the workspace, adopt its revision. Reports what the next sync uploads or deletes. |
| `disconnect` | Forget a stopped workspace, so the project can connect to another environment. |
| `delete --confirm ID` | Delete the named workspace with its files and results. Your credit is kept. This cannot be undone. |
| `environments`, `resources`, `account` | List environments, show the published worker's resources and pricing, or show your credit. |
| `skill [--reference]` | Print the agent instructions or this reference. No key needed. |
| `mcp` | Run the MCP server for this project over stdio. |

Operations are the commands that run Lean: `check`, `inspect`, `try-tactics`,
`verify`, `holes`, `suggest`, `sorry2lemma`, `declarations`, `check-files` and
`verify-batch`. Each first uploads changed files and removes deleted ones. They
accept:

- `--wait SECONDS`: how long to wait for the result, 0 to 40 (default 30).
- `--execution-timeout SECONDS`: the operation's time limit on the server, 1 to 600.

`verify --fresh` runs on a temporary worker. Otherwise, operations share the
workspace's worker, which keeps imports loaded between calls.

`check` is strict by default: `sorry` is an error, and the result is what a
final build would say. `check --draft` is faster while you edit. The worker
keeps the file open, and after an edit Lean reuses its work on the unchanged
part of the file. In a draft check, `sorry` is only a warning. So finish with a
full `check` or with `verify`.

Tactic trials start from the same captured goal without changing the file.
They run in batches of up to four branches normally, or eight on the usage-based
worker, so a large portfolio fits the worker's CPU and memory limits. Results
keep the order of the tactics you gave. Hosted checks have demonstrated reuse of
the captured document. To see the path that actually ran, read each result's
`execution` fields.

`suggest` runs LeanWarp's ranked portfolio for the goal in the same batches:
default tactics, the goal's premises and what has worked before. `--tactic` runs
your alternatives instead. Without `--tactic-timeout`, each tactic gets
LeanWarp's learned time budget for its family.

A tactic that closes the goal is accepted only if it changes nothing else:

- Every other hole and every declaration stays as it was.
- The edit stays inside its own declaration, so a "tactic" cannot append a
  command.

Then a fresh recheck sets `verification`:

- `strict`: the whole file passes the strict check.
- `declaration`: other declarations still have holes, but this one is complete.
  By Lean's `#print axioms`, it depends only on `propext`, `Classical.choice`
  and `Quot.sound`. A proof through an unproved lemma or `native_decide` fails
  here.
- `partial`: this declaration still has other holes. The file compiles with no
  error it lacked before, and the declaration gains no axiom and no unproved
  lemma.

When a search tactic such as `exact?` closes the goal, the patch holds the term
it found (`exact Nat.add_comm a b`), and `found_by` names the search. This way,
compiling the file doesn't repeat the search. If the found term does not pass,
LeanWarp checks the search tactic itself instead.

Preparing imports, the portfolio and its rechecks share one four-minute budget.

`apply` writes a result only onto the exact bytes it was computed from. Run it
straight after `suggest` or `sorry2lemma`. The result must:

- be this project's latest operation, and successful;
- have completed at the revision the project last uploaded;
- be for a file unchanged since then.

Each result names the SHA-256 of the source it read and of the source it
produces. `apply` also uploads other changed files, as `sync` would. It prints
`status: applied` with the new `revision`. If the upload fails after the file is
written, run `apply` again. It sees the edit is already in place and uploads it.

`apply` refuses a read-only or hard-linked file. It also refuses a result for a
file the operation was not asked about. The project records the files it
submitted in `.leanwarp/operation.json`. A result submitted by an earlier SDK
version has no such record, so run the operation again. Check the file
afterwards.

`apply` keeps a copy of each file it replaces in `.leanwarp/apply-backups` (the
printed `backup_directory`). The copy includes anything an editor wrote to the
old file. Each copy has a `RECOVERY.txt` naming its source. Copies are never
deleted automatically.

If an editor saves the file while `apply` runs, or `apply` is interrupted,
it leaves `.leanwarp/apply-recovery.txt` and refuses further applies and
uploads. Stop and ask the user to:

- close their editors;
- keep the version they want, saving it as a new file if the original is
  hard-linked;
- delete the marker.

Run the operation again if the source changed. `status` and `recover` report
this. They never choose between the two versions for you.

`sorry2lemma` moves the proof of each `theorem` or `lemma` that still has a
`sorry` into a generated lemma inserted before it. It rewrites the original to
call that lemma. The rest of the file must compile. The lemmas keep their
`sorry`, so prove each one next. Section variables are preserved in the
generated lemma application. A failed transformation leaves the source
unchanged.

`check-files` runs each file as `check` would, on the workspace's warm (already
running) workers, within a four-minute budget. A file the budget does not reach
is `not_checked`, and the batch does not pass.

`verify-batch` checks inline proofs the same way, without uploading them as
files. Each `custom_id` must be unique and 1 to 128 characters long. The request
holds at most 256 KiB of UTF-8. A proof can import what the environment
provides, such as Mathlib, but not the project's own modules. Such a proof is
refused with `error.code` `unsupported_import`, and does not run. Use
`check-files` for proofs that need the project's modules.

## Environments

An environment is a Lean release with pinned (fixed) library versions, already
built on LeanWarp's workers, such as `lean-4.35-mathlib`.
`leanwarp environments` lists them with their toolchain and Mathlib commit.
LeanWarp updates an environment's build, for example with engine fixes, without
changing its Lean or Mathlib. A workspace picks up the new build when its next
worker starts.

`connect` chooses an environment from the project's `lean-toolchain` and its
lockfile, `lake-manifest.json`:

| The project has | LeanWarp uses | `match` |
| --- | --- | --- |
| A toolchain and lockfile that match an environment | That environment | `exact` |
| Only a toolchain that matches | That environment | `exact` |
| Neither file | The only served environment, or the newest served stable Lean release | `unpinned` |
| Files that match no environment | Nothing; `connect` lists the options | |
| Any of these, plus `--environment NAME` | That environment | `requested` |

With `--environment`, `matches_project` says whether the project's own files
match. If they don't, your files compile against the environment's Lean and
Mathlib, which can differ from a local build. An import of a package the
environment doesn't contain fails like any missing import.

A workspace stays on its environment. If the project's toolchain or lockfile
changes, run `stop`, `disconnect` and `connect` again.

## Results

Every operation result starts with `success`. The operation follows, as
LeanWarp returned it:

| `success` | Meaning | Exit code |
| --- | --- | --- |
| `true` | The check passed, the proof was verified, inspection returned goal context, the tactic trial ran, a suggestion closed its hole, an extraction completed, or a listing (`holes`, `declarations`) completed, even of a file that does not compile yet. For `check-files` and `verify-batch`, every file or proof passed. | `0` |
| `false` | It did not pass. Read `result.result`, or `error_message` if the operation failed. | `1` |
| `null` | Still running. Run `wait`. Never submit it again. | `3` |

Other exit codes:

- `1` from `doctor` when no environment matches.
- `2` when a command could not run. Its JSON then has `error` (a stable code)
  and `message`.

Any result, from the CLI or MCP, can also carry `update_available` (with
`current`, `latest` and `command`) when the service names a newer SDK release.
An SDK older than the service's minimum is refused with `sdk_upgrade_required`
until you update it with `leanwarp update`. An SDK installed from GitHub before
the PyPI package (`leanwarp-sdk`) has no `update`. Replace it with `uv tool
uninstall leanwarp-sdk && uv tool install leanwarp`.

What to read in `result.result`:

| Operation | Fields |
| --- | --- |
| `check` | `status` (`ok` when there are no errors), `diagnostics` and `cached`. |
| `inspect` | `goal_status`: either `available` with `goal_contexts`, or `unavailable` with `unavailable_reason`. `retained_proof_state`, when present, says whether the worker kept a proof state. It does not promise which tactic execution path will run. Also `selected_hole`, `enclosing_declaration`, `premise_hints` and `context_omitted`. |
| `try_tactics` | `results`: one entry per tactic, in the order given, each with `tactic`, `outcome`, `status`, `diagnostics`, `elapsed_ms` and `execution`. `skipped` lists tactics that did not run, with a reason. |
| `verify_target` | `status` `ok` and `receipt.policy` `fixed_target_kernel_check_v1`. A rejection has `failure_kind`. |
| `holes` | `holes`: each with `kind`, `token`, its exact semantic `state` (`resolved`, `sorry`, `unresolved` or `unknown`), `unfinished` and source positions. A filled `?_` is resolved independently of other holes in its declaration. `unfinished_count` counts confirmed `sorry` and `unresolved` placeholders. `unknown_count` is separate. The legacy `unfinished` boolean stays true for unknowns. False means positively resolved. `hole_count` counts all syntax placeholders, not remaining goals. `resolved_count`, `holes_omitted` and `goals_omitted` account for resolved entries and output limits. Goals (`target`, `hypotheses`) are returned only for the first 16 unfinished or unknown entries. `goal_status: omitted` means the context was not returned. `status: error` and `diagnostics` keep compilation errors. `diagnostics_complete` says whether elaboration finished. |
| `suggest` | Either `status` `closed`, with `tactic`, `found_by` (when `tactic` is what a search such as `exact?` found), `verification` (`strict`, `declaration` or `partial`), `patch` (`start_utf8_offset`, `end_utf8_offset`, `replacement`), `source_sha256` and `patched_source_sha256`. Or `not_closed`, with `tried`, `candidates`, `rejected_by_recheck`, `rejections` (each closing `tactic` the recheck refused, with its `reason`, listed below) and `exhausted` (`portfolio` or `budget`). `portfolio` is `leanwarp` or `caller`. |
| `extract_decls` | `declarations` written in the file, not those Lean generates, such as recursors. Each has `name`, `kind`, `type`, `local_dependencies`, `dependencies` (up to 128, then `dependencies_omitted`), `level_params`, `is_unsafe`, `is_partial` and the source range. `declarations_omitted` counts any beyond 512 or beyond the result's size. |
| `sorry2lemma` | `status` (`extracted`, `partial`, `unchanged`, `no_targets` or `failed`), and `targets`, each with `declaration_name`, `generated_lemma_name` and `status`. When `source_changed`, also `transformed_source` with its `transformed_source_sha256`. Then `final_check` and, if requested, `strict_final_check`. A transformed file too large to return gives `status` `error` with `error.code` `result_too_large`. |
| `check_files` | `status` (`ok` only when every file passed), `passed`, `failed`, and `results` in the order given. Each result has `file`, `status`, `diagnostics` and `elapsed_ms`. A file whose imports could not be prepared has `phase` `dependencies`. A file the batch's time did not reach is `not_checked`. |
| `verify_batch` | `status` (`ok` only when every proof passed), `strict`, `passed`, `failed`, and `results` in the order given. Each result has `custom_id`, `status`, `diagnostics` and, when known, `elapsed_ms`. A proof whose imports were refused or could not be read has `phase` `dependencies`. A proof the batch's time did not reach is `not_checked`. |

A `suggest` rejection `reason` is one of:

- `uses_sorry`: the tactic is or contains `sorry` or `admit`.
- `changes_other_holes`: the edit removed or added another hole, such as a new `?_`.
- `changes_declarations`: the edit added, removed or changed a declaration.
- `edit_outside_declaration`: the edit reaches past the declaration it is in.
- `uses_unproved_lemma`: the proof cites a lemma that still has a `sorry`.
- `uses_unproved_or_extra_axiom`: the proof relies on `sorry` or an axiom beyond
  `propext`, `Classical.choice` and `Quot.sound` (for example `native_decide`).
- `new_errors`: the file has an error it did not have before.
- `axiom_audit_unavailable`: the engine could not supply structured,
  kernel-checked axiom evidence for the exact declaration. This does not mean
  the proposed tactic is invalid.
- `recheck_incomplete`: the recheck timed out or did not finish. The tactic may
  still be correct. Add it by hand and check the file.

A recheck that runs out of the operation's time ends the search with
`exhausted: budget`. It does not count as a rejection.

A trial's `outcome` is `closed_proof`, `open_proof_state` (the tactic ran and
goals remain, shown in its diagnostics), `tactic_error`, `timeout` or
`rejected`. An unavailable context doesn't mean a goal is solved. A tactic that
succeeds in a trial isn't a proof until the file checks with it. Engine errors,
such as a position outside any proof, arrive as `status` `error`, with an
`error` object holding `code` and `message`.

Inspection supports ordinary `sorry` drafts and explicit proof holes. The
position you select must identify one goal context. Tactic inputs are bare Lean
tactics. A `by` wrapper is optional.

For successful inspections, `status` keeps the older `proof_state` or
`metadata_only` marker, for compatibility. These markers describe what was
retained, not whether goals are available: `metadata_only` can still contain
useful goals. Use `goal_status` and `goal_contexts` to decide whether context
was returned. An unavailable context has `status: unavailable`, so older clients
also report it as unsuccessful.

### How it ran

Completed operations also carry `execution`, which says what LeanWarp reused,
and `timing`, which says where the time went. Only the facts that apply appear.

| `execution` field | Values |
| --- | --- |
| `worker` | `warm`: the workspace's running worker. `started`: a new worker started for this operation. `temporary`: a separate worker that stopped afterwards. |
| `imports` | `reused`: the file's imports were already loaded. `extended`: loaded imports were extended. `loaded`: imported now. |
| `document` | Draft checks only. `incremental`: Lean reused its earlier work on the file. `full`: Lean processed (elaborated) the file from the start. |
| `trials` | Tactic trials, counted by how they ran. `proof_state`: from Lean's captured proof state. `document_state`: replayed from the captured document. `source_recheck`: the file was rechecked with the tactic in place. `cached`: an identical earlier trial. |

`timing` has `queued_ms`, `worker_ms` (getting a ready worker, including any
start), `upload_ms` and `run_ms`. `queued_ms` is left out
when the operation was recovered after a failed attempt. Trials also report
their own `execution` and `elapsed_ms`.

Each result names the operation ID and source revision it belongs to. The SDK
rejects a result that doesn't match the operation the project submitted.
`result.generation` identifies the worker that ran it. A fresh or resized
verification runs on its own worker.

### Verification policy

`fixed_target_kernel_check_v1` elaborates the target statement on its own, with
only the context you supply. Then it checks the candidate proof against it with
Lean's kernel. So helper declarations in the candidate file can't change what
the target means. Supply the target's imports and definitions with
`--context-file` (MCP: `target_context`).

The checked theorem's axioms are audited transitively. Only `propext`,
`Classical.choice` and `Quot.sound` are allowed. `sorry`, `admit` and any other
axiom are rejected, including ones reached through imports. Source is rejected
if it uses `native_decide`, unsupported `set_option` commands or custom
elaborators. Declaration names must be ASCII. A rejection is not permission to
weaken the statement.

Fixed-target verification permits `set_option autoImplicit false`, including its
scoped `in` form, and preserves the source and scope. Other options are still
rejected, including `autoImplicit true` and kernel-check controls.

The receipt records hashes and environment identities for the check. It is not
a signed, portable certificate. Lean and the environment's dependencies are
still trusted, and the check does not sandbox hostile metaprograms.

## MCP

Add the server to your client:

```json
{
  "mcpServers": {
    "leanwarp": {
      "command": "leanwarp",
      "args": ["--project", "/absolute/path/to/lean-project", "mcp"]
    }
  }
}
```

If the client can't find `leanwarp` on its `PATH`, use its absolute path. The
server reads the key saved by `auth login`, or `LEANWARP_API_KEY`. It starts
without a key. Tools that need a key then say how to sign in. The resources
`leanwarp://guide` and `leanwarp://reference` serve the agent instructions and
this reference without a key. The CLI and MCP share the project's `.leanwarp/`
state, so don't run both against one project at the same time.

Tools: `doctor`, `files`, `environments`, `connect`, `check` (`draft`),
`inspect`, `try_tactics`, `verify_target` (`file`, `candidate_declaration`,
`target_statement`, `target_context`), `holes`, `suggest` (`tactics` optional),
`apply`, `sorry2lemma`, `extract_decls`, `check_files` (`files`, `draft`),
`verify_batch` (`codes`, `draft`), `wait`, `status`, `cancel`, `stop`,
`recover`, `reconcile`, `disconnect`, `workspaces`, `history` (one page; pass
`next_cursor` for older ones), `delete` (`workspace_id`), `account`, `resources`
and `feedback` (`kind`, `summary`, `details`, `severity`, `operation_id`).

`connect` accepts `workspace_id` to connect to an existing workspace. Operation
tools wait up to `wait_seconds` (default 20, at most 40). Like the CLI, they
lead their result with `success`.

## Python

Install the library in your application's environment:

```sh
uv add leanwarp
```

Without uv, use `python -m pip install leanwarp`. The CLI's isolated install
doesn't make the library importable in your application.

```python
from leanwarp_cloud import OperationOutcome, ProjectSession, load_client

with load_client() as cloud:
    project = ProjectSession(cloud, "/path/to/lean-project")
    project.connect()
    result = project.submit_and_wait(
        "verify_target",
        {
            "file": "LeanWarpExample.lean",
            "candidate_declaration": "add_zero_example",
            "target_statement": "∀ n : Nat, n + 0 = n",
            "execution_mode": "reusable",
        },
        wait_seconds=30,
    )
    if not OperationOutcome(result).terminal:
        result = project.wait(timeout=300)
    project.stop()
    if not OperationOutcome(result).verified:
        raise RuntimeError("the proof was not verified", result)
```

`load_client()` reads the same key as the CLI. Like the CLI, `ProjectSession`
keeps the workspace, revisions and any unfinished request in
`.leanwarp/session.json`. `connect(environment=NAME)` chooses an environment
explicitly. Closing the client doesn't stop the worker, so call `stop()`.

`project.apply()` writes the latest `suggest` or `sorry2lemma` result, as the
CLI does. With the lower-level client, `leanwarp_cloud.edits.edited_source(kind,
result, source)` returns the edited file. It raises `EditError` unless the
source and the result's checksums match.

`project.submit_and_wait("verify_batch", {"codes": [...]}, wait_seconds=...)`
checks up to 64 inline proofs, each `{"custom_id": ..., "proof": ...}`, in one
operation. The lower-level `LeanWarpCloud.verify_batch(workspace_id, codes,
expected_revision=...)` does the same. Results keep each `custom_id`, in order.

`project.feedback(kind, summary, details=..., severity=...)` reports a problem
with LeanWarp itself, naming the project's workspace and latest operation.

`LeanWarpCloud(api_key)` is the lower-level HTTP client, for applications that
manage their own state. Before each create, sync or submit, save the request and
its idempotency key. After an uncertain response, resend exactly that request
with the same key. See the [HTTP
API](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/http-api.md).

## Recovery

| Situation | What to do |
| --- | --- |
| A create, upload or submit response was lost | `recover` resends the saved request, even if files changed since. |
| A file can't be uploaded, or the upload is too large | Run `files` to see every problem. Then exclude unrelated files in `.leanwarpignore`. |
| `success` is `null`, or `wait` timed out | Run `wait` again. To abandon it, run `cancel`, then `wait` until it ends. |
| Another LeanWarp command is using the project | Let it finish. A lock prevents overlapping writes. |
| `revision_conflict` from another machine | Decide with the other writer which files to keep. Then run `reconcile`: it adopts the workspace's revision and lists what the next sync uploads over theirs or deletes. Don't delete `.leanwarp/` to force it. |
| The key was rotated, or lacks a permission | Sign in with a key for the same account that has the permission, then run `recover`. `stop` works meanwhile. |
| The toolchain or lockfile changed | `stop`, `disconnect`, then `doctor` and `connect`. |
| `.leanwarp/session.json` is damaged | Keep it. It may record an unfinished request. |
| Not enough credit, or a spending cap reached | Add credit or raise the cap in the dashboard, then retry. |

Submitting an operation takes two requests: the file upload, then the operation.

- If `recover` returns an `operation_id`, the operation was submitted. Wait for
  it.
- If it returns only a workspace and revision, the upload or creation finished.
  Run the intended command again.
- With nothing to recover, `recover` returns the same view as `status`.

`cancel` and `stop` aren't recorded for recovery. If their response is lost,
check `status` and run them again. Don't assume a lost response or a closed
client stopped billing. `stop` works even while an earlier request awaits
`recover`. That request stays recorded.

The project's connection lives in its `.leanwarp/` folder. A clone of the
project on another machine can create its own workspace with `connect`. Or it
can share one with `connect --workspace ID` (IDs from `workspaces`). Connecting
to an existing workspace takes its current files as the baseline, so the next
sync uploads only what differs. The two copies then share one revision history,
and `reconcile` resolves a `revision_conflict` between them.

`disconnect` needs a confirmed `stop` and no queued or running operation.
`delete` refuses while work is queued or running. Deleting this project's own
workspace also disconnects the project and discards any request awaiting
`recover`. Run `export` first to keep its results. You can also stop or delete
a workspace in the dashboard.

## Credentials, credit and limits

`auth login` saves the key in `$XDG_CONFIG_HOME/leanwarp/credentials.json`
(normally `~/.config/leanwarp/credentials.json`), readable only by you.
`LEANWARP_API_KEY` takes precedence. The key decides which LeanWarp service is
used, so there is no URL to configure. `auth logout` forgets the saved key. To
disable the key, revoke it in the dashboard.

Workers are billed per second while they run, including idle time. `resources`
reports one published `worker`: its starting minute quote, `usage_based`,
resource limits, startup credit hold and idle timeout. LeanWarp selects the
worker. You can set a spending cap when you connect, or in the dashboard.
Existing workspaces keep their saved billing terms.

While a worker runs, the service holds (reserves) credit. It returns the unused
part once shutdown and final usage are confirmed. A stopped workspace can keep
reserved credit until that billing cleanup finishes.

`leanwarp connect --max-spend 2` creates a workspace capped at $2 in total,
including reserved credit. USD amounts allow up to six decimal places, without
rounding. Python accepts `max_spend_microusd=2_000_000` on `connect`,
`create_workspace` and `create_workspace_for_project`. MCP `connect` accepts
`max_spend_microusd` in the same units. The allowed range is zero to
$1,000,000.

If you omit the cap, a first connection creates an uncapped workspace, and a
reconnect keeps the existing cap. A reconnect with a different cap is rejected.
Change an existing cap in the dashboard. A cap of zero stops any worker from
starting.

When `usage_based` is true, CPU and RAM charges rise independently above their
reserved baselines. The starting rate remains a paid minimum while the worker
runs, including idle time. Bursting depends on available
host capacity. When `usage_based` is false, the quoted minute rate applies for
the time the worker runs.

A worker starts with the first operation. It stays warm (running) between
operations until the idle timeout, its maximum lifetime or `stop`. Closing the
SDK client only closes its HTTP connection. It does not stop the worker or
billing. `stop` discards in-memory state but keeps uploaded files and completed
results. The next operation rebuilds the worker without another upload.

A cap change applies to future worker starts and elastic lease renewals. If the
next credit hold cannot fit, the worker stops, and an unfinished operation
reports `leanwarp_budget_exceeded`.

`account` reports `balance_microusd`, `reserved_microusd` and
`available_microusd` as strings ($1 = 1000000).

| Limit | Value |
| --- | --- |
| Upload | 256 `.lean` files and 1 MiB per project, including paths |
| Module paths | ASCII letters, digits and `_`; not under `Init`, `Lean`, `Lake`, `Std` or `Mathlib` |
| Statement context | 128 KiB |
| Inline wait | 0 to 40 seconds (`wait` can poll longer) |
| Operation time limit | 1 to 600 seconds |
| Operations per workspace | One at a time |
| Operation history | The newest 1,000 operations per workspace\* |
| File sync history | The newest 1,000 syncs per workspace\* |
| Stored results | 64 MiB per account, across workspaces\*. Each running operation reserves 1 MiB |
| Stored source | 64 MiB per account, across workspaces\* |
| Workspaces | 100 per account\* |
| Feedback | 60 requests an hour and 300 a day per account, repeats included. Of these, 20 an hour and 100 a day may be new reports. A summary of 8 to 200 characters, and up to 4,000 characters of details. Reports expire 180 days after their last repeat. At most 500 are kept per account, and they are deleted with their workspace |
| Active API keys | 100 per account\* |

\* The default. These limits are per account, and an account's own limits may be
higher. Email contact@isagoge.in to raise them. `status` reports the account's
history limits.

History is a window, not a lifetime allowance. When a new operation or sync
would pass a history limit, LeanWarp removes the oldest finished operations and
sync records that finished more than an hour ago, but only if that makes enough
room. Otherwise nothing is removed, and the request reports the
limit. LeanWarp never removes queued or running work, each workspace's newest
operation and sync, or your uploaded files.

Retrying a request with the same idempotency key within the hour returns the
original result. Later, a removed operation reads as `not_found`. For at least 7
days, resubmitting it returns `operation_expired` without running it again.
Keep results you need with `export`. `status` reports `history_usage`: each
limit's `used` and `limit`.

Hidden directories, `.lake`, `lakefile.lean` and symlinks are never uploaded,
and symlinked Lean files are rejected. Lake scripts and dependency builds never
run.

Patterns in the project's top-level `.gitignore` and `.leanwarpignore` exclude
files, using git's syntax. `.leanwarpignore` is read last. So it can exclude
something git tracks, such as an `archive/` folder, or re-include a file git
ignores with `!path`. A file inside an excluded directory can't be re-included.
`files` lists exactly what would upload.

# LeanWarp reference

## Commands

Run commands from your Lean project, or pass `leanwarp --project /path/to/project`.
Use `leanwarp COMMAND --help` for arguments.

| Command | Purpose |
| --- | --- |
| `doctor` | Match the exact toolchain and dependency lockfile to a supported bundle. |
| `account`, `resources`, `versions` | Read available credit, compute profiles and supported bundles. |
| `connect` | Save a workspace without starting compute. |
| `check FILE` | Check a Lean file. |
| `inspect FILE --line N --column N` | Read proof state at a one-based position. |
| `try-tactics FILE --line N --column N --tactic simp` | Test a tactic without editing the file. |
| `verify FILE --declaration NAME --target 'PROPOSITION'` | Verify a declaration against a fixed statement. |
| `wait`, `status` | Read the current operation's result or progress. |
| `recover` | Replay an interrupted request from the local journal. |
| `cancel`, `stop`, `disconnect` | Cancel work, stop compute, or forget a stopped connection. |
| `skill`, `skill --reference` | Read bundled agent instructions or this reference; no account required. |

`check`, `inspect`, `try-tactics` and `verify` synchronize changed files and
explicit deletions before submitting. The project session saves workspace,
revision and operation IDs automatically.

Verification reuses warm compute by default. `verify --fresh` uses a temporary
allocation; choosing a different `--profile` also does so, within the connected
workspace's `--max-profile`. Temporary compute stops after verification. Shared
compute remains billable until stopped or retired by the server's idle/lifetime
policy. A restarted worker restores acknowledged files but loses in-memory handles.

## Results

Commands return JSON; operations run on the server after submission. MCP operation
tools wait up to `wait_seconds` (default 20, maximum 40) and return the result when
it finishes in time. CLI operations accept `--wait SECONDS` for the same behavior.
A non-terminal state means the work is still running: call `wait`, never resubmit.
A terminal operation can still contain a rejected proof or compiler errors.

| Operation | Result to inspect |
| --- | --- |
| `check` | `result.result.status` and compiler diagnostics. |
| `inspect` | `proof_state` or `metadata_only`. |
| `try_tactics` | Each candidate in `result.result.results`. |
| `verify_target` | Status `ok` and receipt policy `fixed_target_kernel_check_v1`. |

`metadata_only` means inspection returned metadata without a proof state; it does
not mean the goal is solved. Inspect a supported proof position or use fixed-target
verification to establish that the intended statement was proved.

Match results to the operation ID, source revision and `result.generation`.
`workspace_generation` identifies the shared workspace generation; fresh
verification can use a different allocation. Python's `OperationOutcome` checks
the result identity and distinguishes completed work from successful verification.
The SDK rejects an operation response whose ID differs from the requested ID.
Within a result, the receipt envelope's ID and revision must match the operation.
Project sessions also save each new submission's revision and reject results
that disagree with it, even after later file synchronization advances the workspace.
The executed allocation's generation is `result.generation`; do not compare it to
`workspace_generation` for a fresh verification.

CLI exit codes: `0` successful command or result, `1` unsuccessful terminal result
or incompatible project, `2` request/local error, `3` polling timeout (including an
operation still running when `--wait` expires). A successful submission alone does
not mean the proof passed.

### Verification policy

`fixed_target_kernel_check_v1` elaborates your target independently, then checks
the candidate proof in that fixed context with Lean's kernel. Candidate helpers
cannot redefine what the target means. Supply the target's required imports and
definitions through `target_context` (CLI: `--context-file`).

The candidate and checked theorem are audited for transitive axioms. Only
`propext`, `Classical.choice` and `Quot.sound` are allowed; `sorry`, `admit` and
project-defined axioms are rejected, including disallowed axioms reached through
imports. The supported source language excludes `native_decide`, `set_option`
and source-defined elaborators. Declaration selectors must be ASCII dotted names.
An unsupported construct is a rejection, not permission to weaken the theorem.

The receipt records hashes and environment identities for this check. It is not
a portable signed proof certificate. Lean and the imported toolchain remain
trusted; this check does not sandbox arbitrary hostile metaprograms.

## MCP

Configure your client to launch this local stdio server:

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

Use the absolute `leanwarp` executable path if it is outside the client's `PATH`.
MCP reads the CLI's saved credentials or injected environment variables. Keep keys
out of this JSON and tool arguments. CLI and MCP share the same project journal;
avoid overlapping writes.

Read the MCP resources `leanwarp://guide` and `leanwarp://reference` for the same
instructions bundled with the CLI. Reading them does not call the hosted API or
start compute.

Tool names include `connect`, `account`, `check`, `inspect`, `try_tactics`,
`verify_target`, `wait`, `recover`, `cancel` and `stop`. For `verify_target`, provide `file`, `candidate_declaration`, `target_statement` and,
when needed, `target_context` containing the imports and definitions for the target.

## Python

Install the library in your Python 3.12+ application's environment:

```sh
uv add 'leanwarp-sdk @ git+https://github.com/Isagoge-Labs/LeanWarp-SDK.git'
```

For an existing virtual environment without uv, use
`python -m pip install 'leanwarp-sdk @ git+https://github.com/Isagoge-Labs/LeanWarp-SDK.git'`.
The isolated CLI installation does not make Python imports available to your
application.

This example uses the `LeanWarpExample.lean` declaration from
[Getting started](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/getting-started.md)
and credentials configured through `leanwarp auth login` or the environment:

```python
from leanwarp_cloud import OperationOutcome, ProjectSession
from leanwarp_cloud.config import load_client

with load_client() as cloud:
    project = ProjectSession(cloud, "/path/to/your/lean-project")
    project.connect()
    result = project.submit_and_wait("verify_target", {
        "file": "LeanWarpExample.lean",
        "candidate_declaration": "add_zero_example",
        "target_statement": "∀ n : Nat, n + 0 = n",
        "execution_mode": "reusable",
    }, wait_seconds=30)
    if not OperationOutcome(result).terminal:
        result = project.wait(timeout=300)
    project.stop()
    if not OperationOutcome(result).verified:
        raise RuntimeError("Verification failed", result)
```

Once a result is returned, this stops compute before reporting a rejected proof.
If submission or polling raises an error, use the recovery steps below; closing
`load_client()` only closes the HTTP client. An active operation must finish or be
cancelled before stopping compute.

`LeanWarpCloud(api_key)` is the lower-level HTTP client. Use it when your
application owns persistence: save workspace/revision/operation IDs and the exact
payload and idempotency key before create, sync or submit. Reuse that key and
payload after an uncertain response; do not silently rebase conflicting writes.

## Recovery

| Situation | Action |
| --- | --- |
| Create, sync or submit response lost | `recover` replays that exact saved request, even if local files changed. Inspect which phase it recovered, as described below. |
| Polling timed out | Run `wait` again. To cancel, run `cancel` and wait for a terminal result. |
| Another local process is writing | Let it finish; the project lock prevents overlapping writes. |
| Revision conflict from another device | Reconcile with the other writer; preserve the journal. |
| Key rotated | Authenticate with a new key for the same account, then recover. |
| Toolchain or lockfile changed | Stop, disconnect, run `doctor`, then connect again. |
| Malformed journal | Preserve it for recovery; do not delete uncertain requests. |
| Insufficient credit | Inspect `account`, replenish credit, and retry after any requested shutdown finishes. |

Submission first synchronizes files, then sends the operation. These are separate
requests. If `recover` returns an `operation_id`, submission was recovered: wait
for that operation instead of submitting it again. A receipt containing only a
workspace ID and revision confirms creation or file sync, so resume the intended
command; no new operation was submitted by that recovery. With no pending request,
`recover` returns the same nested workspace/operation view as `status`.

`cancel` and `stop` are not journaled. If their response is lost, inspect `status`.
For cancellation, keep polling and retry `cancel` if work is still queued or
running. For shutdown, retry `stop` once no operation is active. Do not treat a
lost response or a closed client as confirmation that billing stopped.

A definite revision-conflict rejection clears the rejected request, but keeps
the project connection and source hashes. `recover` does not resolve that conflict.
Coordinate with the other writer before choosing which source to keep; do not
delete the journal to force an overwrite.

The journal is local to the project directory. Cloning the Lean project on another
machine does not copy it and `connect` creates another workspace there. There is
no attach-by-ID command. For a deliberate handoff, finish or cancel work, confirm
stop, then securely transfer the same project and `.leanwarp/` directory to the
same account and service. Authenticate separately; do not run concurrent writers.

`disconnect` requires a confirmed stop and no queued or running operation,
including temporary verification. A stopped shared worker alone is insufficient.

## Credentials, credit and limits

Saved credentials live in `$XDG_CONFIG_HOME/leanwarp/credentials.json`, defaulting
to `~/.config/leanwarp/credentials.json`, readable only by their owner. Setting
`LEANWARP_API_KEY` overrides that file. The key selects the service automatically. `auth logout` removes
the saved file; revoke the key in the dashboard to invalidate it. Environment
credentials are unaffected.

Manage funding, workspace lifetime caps and usage in the website. The SDK does not
set spending limits. Changing a cap does not cancel already reserved work.
`account` returns posted balance, remaining reservations and available credit as
strings in microdollars (`$1 = 1000000`). Workers reserve their maximum admitted
lifetime charge before starting. Actual usage consumes the reservation; confirmed
shutdown releases the unused amount. A positive posted balance may therefore be
insufficient for another allocation.

`--execution-timeout` sets the server deadline (1–600 seconds). `wait --timeout`
and an operation's `--wait` limit local polling; HTTP request timeouts are separate. Uploads are limited to
256 files / 1 MiB including path bytes. Hidden/dependency/cache directories and
`lakefile.lean` are excluded. Visible symlinked source directories and symlinked
Lean files are rejected. Module path components must start with an ASCII letter
or underscore and contain only ASCII letters, digits or underscores; files must
end in `.lean`. Top-level `Init`, `Lean`, `Lake`, `Std` and `Mathlib` are reserved.
`doctor` checks environment metadata, not source uploadability. Arbitrary Lake
scripts and dependency builds are not executed.

# LeanWarp reference

## Commands

Run commands from your Lean project, or pass `leanwarp --project /path/to/project`.
Use `leanwarp COMMAND --help` for arguments.

| Command | Purpose |
| --- | --- |
| `doctor` | Match the exact toolchain and dependency lockfile to a supported bundle. |
| `account`, `resources`, `versions` | Read available credit, compute profiles and supported bundles. |
| `connect --max-spend 5` | Save a workspace with a $5 spending limit. No compute starts yet. |
| `check FILE` | Check a Lean file. |
| `inspect FILE --line N --column N` | Read proof state at a one-based position. |
| `try-tactics FILE --line N --column N --tactic simp` | Test a tactic without editing the file. |
| `verify FILE --declaration NAME --target 'PROPOSITION'` | Verify a declaration against a fixed statement. |
| `wait`, `status` | Read the current operation's result or progress. |
| `recover` | Replay an interrupted request from the local journal. |
| `cancel`, `stop`, `disconnect` | Cancel work, stop compute, or forget a stopped connection. |

`check`, `inspect`, `try-tactics` and `verify` synchronize changed files and
explicit deletions before submitting. The project session saves workspace,
revision and operation IDs automatically.

Verification reuses warm compute by default. `verify --fresh` uses a temporary
allocation; choosing a different `--profile` also does so, within the connected
workspace's `--max-profile`. Temporary compute stops after verification. Shared
compute remains billable until stopped or retired by the server's idle/lifetime
policy. A restarted worker restores acknowledged files but loses in-memory handles.

## Results

Commands return JSON; operations are asynchronous. Call `wait` after submission.
A terminal operation can still contain a rejected proof or compiler errors.

| Operation | Result to inspect |
| --- | --- |
| `check` | `result.result.status` and compiler diagnostics. |
| `inspect` | `proof_state` or `metadata_only`. |
| `try_tactics` | Each candidate in `result.result.results`. |
| `verify_target` | Status `ok` and receipt policy `fixed_target_kernel_check_v1`. |

Match results to the operation ID, source revision and `result.generation`.
`workspace_generation` identifies the shared workspace generation; fresh
verification can use a different allocation. Python's `OperationOutcome` checks
the result identity and distinguishes completed work from successful verification.

CLI exit codes: `0` successful command or result, `1` unsuccessful terminal result
or incompatible project, `2` request/local error, `3` polling timeout. A successful
submission alone does not mean the proof passed.

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

Tool names include `connect`, `account`, `check`, `inspect`, `try_tactics`,
`verify_target`, `wait`, `recover`, `cancel` and `stop`. MCP budgets use integer
microdollars: `connect(max_spend_microusd=5000000)` sets a $5 ceiling. For
`verify_target`, provide `file`, `candidate_declaration`, `target_statement` and,
when needed, `target_context` containing the imports and definitions for the target.

## Python

This example uses the `LeanWarpExample.lean` declaration from the README and
credentials configured through `leanwarp auth login` or the environment:

```python
from leanwarp_cloud import OperationOutcome, ProjectSession
from leanwarp_cloud.config import load_client

with load_client() as cloud:
    project = ProjectSession(cloud, "/path/to/your/lean-project")
    project.connect(max_spend_microusd=5_000_000)
    project.submit("verify_target", {
        "file": "LeanWarpExample.lean",
        "candidate_declaration": "add_zero_example",
        "target_statement": "∀ n : Nat, n + 0 = n",
        "execution_mode": "reusable",
    })
    result = project.wait(timeout=300)
    project.stop()
    if not OperationOutcome(result).verified:
        raise RuntimeError("Verification failed", result)
```

Once a result is returned, this stops compute before reporting a rejected proof.
If submission or polling raises an error, use the recovery steps below; closing
`load_client()` only closes the HTTP client. An active operation must finish or be
cancelled before stopping compute.

`LeanWarpCloud(base_url, api_key)` is the lower-level HTTP client. Use it when your
application owns persistence: save workspace/revision/operation IDs and the exact
payload and idempotency key before create, sync or submit. Reuse that key and
payload after an uncertain response; do not silently rebase conflicting writes.

## Recovery

| Situation | Action |
| --- | --- |
| Response lost or process interrupted | `recover` replays the saved request, even if local files changed. |
| Polling timed out | Run `wait` again. To cancel, run `cancel` and wait for a terminal result. |
| Another local process is writing | Let it finish; the project lock prevents overlapping writes. |
| Revision conflict from another device | Reconcile with the other writer; preserve the journal. |
| Key rotated | Authenticate with a new key for the same account, then recover. |
| Toolchain or lockfile changed | Stop, disconnect, run `doctor`, then connect with an approved ceiling. |
| Malformed journal | Preserve it for recovery; do not delete uncertain requests. |
| Insufficient credit | Inspect `account`, replenish credit, and retry after any requested shutdown finishes. |

`disconnect` requires a confirmed stop and no queued or running operation,
including temporary verification. A stopped shared worker alone is insufficient.

## Credentials, credit and limits

Saved credentials live in `$XDG_CONFIG_HOME/leanwarp/credentials.json`, defaulting
to `~/.config/leanwarp/credentials.json`, readable only by their owner. Setting both
`LEANWARP_BASE_URL` and `LEANWARP_API_KEY` overrides that file. `auth logout` removes
the saved file; revoke the key in the dashboard to invalidate it. Environment
credentials are unaffected.

`account` returns posted balance, remaining reservations and available credit as
strings in microdollars (`$1 = 1000000`). Workers reserve their maximum admitted
lifetime charge before starting. Actual usage consumes the reservation; confirmed
shutdown releases the unused amount. A positive posted balance may therefore be
insufficient for another allocation.

`--execution-timeout` sets the server deadline (1–600 seconds). `wait --timeout`
limits local polling; HTTP request timeouts are separate. Uploads are limited to
256 files / 1 MiB including path bytes. Hidden/dependency/cache directories,
symlinks and `lakefile.lean` are excluded. Arbitrary Lake scripts and dependency
builds are not executed.

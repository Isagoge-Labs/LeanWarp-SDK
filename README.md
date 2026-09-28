# LeanWarp SDK

Check and verify Lean proofs from the terminal, Python, or an AI agent. Work in
your existing Lean project; LeanWarp runs verification remotely and reuses
compatible imports across edits.

Requires Python 3.12+ on Linux, macOS or WSL, and a supported Lean project.

## Install

```sh
git clone https://github.com/Isagoge-Labs/LeanWarp-SDK.git
cd LeanWarp-SDK
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[mcp]'
```

Get an API key and API URL from your LeanWarp dashboard, then sign in:

```sh
leanwarp auth login --base-url https://YOUR_API_ORIGIN
```

Paste the key at the hidden prompt. For automation, set `LEANWARP_BASE_URL` and
`LEANWARP_API_KEY` through your environment or secret manager.

## Verify a proof

Open your Lean project and connect it to LeanWarp:

```sh
cd /path/to/your/lean-project
leanwarp doctor
leanwarp connect --max-spend 5
```

`doctor` checks project compatibility. `connect` sets a $5 workspace spending
limit; execution also requires available account credit. Add `.leanwarp/` to
`.gitignore` to keep local session state out of version control.

Save this as `LeanWarpExample.lean`:

```lean
theorem add_zero_example (n : Nat) : n + 0 = n := by
  rfl
```

Verify the declaration against its intended statement:

```sh
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
leanwarp wait
```

For this verification, `wait` exits with code `0` when the proof passes and `1`
when it fails. Code `3` means polling timed out: run `wait` again. The JSON output
contains the result and verification receipt.

Edit the file and repeat `verify` and `wait`. Changed files upload automatically,
and the same workspace is reused. When finished:

```sh
leanwarp stop
```

Warm compute remains billable until stopped. If an operation is still running,
wait for it or cancel it and wait before stopping.

## Use with an agent

Have your agent read `leanwarp skill`, or install the
[skill directory](src/leanwarp_cloud/skills/leanwarp) using its skill installer.
It covers the edit–verify loop, reuse, recovery and stopping compute.

For MCP, configure your client to run `leanwarp --project /path/to/project mcp`.
The CLI and MCP share the same credentials and project session.

## Reference

- [Commands, results and recovery](src/leanwarp_cloud/skills/leanwarp/references/usage.md)
- [MCP configuration](src/leanwarp_cloud/skills/leanwarp/references/usage.md#mcp)
- [Python usage](src/leanwarp_cloud/skills/leanwarp/references/usage.md#python)

Run `leanwarp --help` or `leanwarp COMMAND --help` for command options.

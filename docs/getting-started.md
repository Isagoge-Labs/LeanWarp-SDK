# Getting started

[Install the CLI](../README.md#install), then get an API key and API URL from your
LeanWarp dashboard. Your account needs available credit to run compute.

If your shell cannot find `leanwarp` after installation, run `uv tool update-shell`
and open a new terminal.

## Sign in

Replace the placeholder with the API origin shown in your dashboard:

```sh
leanwarp auth login --base-url https://YOUR_API_ORIGIN
```

Paste the key at the hidden prompt. For automation, inject `LEANWARP_BASE_URL`
and `LEANWARP_API_KEY` through your environment or secret manager. Keep the key
out of source files and agent conversations.

## Connect your project

```sh
cd /path/to/your/lean-project
leanwarp doctor
leanwarp connect --max-spend 5
```

`doctor` checks whether the project's Lean toolchain and dependency lockfile match
a supported bundle. Continue only if it reports `compatible: true`. LeanWarp does
not build arbitrary project dependencies.

`connect` saves a workspace with a $5 spending limit; it does not start compute.
Execution also requires available account credit. Add `.leanwarp/` to `.gitignore`
to keep local session state out of version control.

## Verify a proof

Save this as `LeanWarpExample.lean` in your project:

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

Submission starts the work; `wait` retrieves its result. For this verification,
`wait` exits with code `0` when the proof passes and `1` when it fails. Code `3`
means polling timed out: run `wait` again. The JSON output contains the result and
verification receipt. See [results and exit codes](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#results).

Edit the file and repeat `verify` and `wait`. Changed files upload automatically,
and the same workspace is reused. Compatible imports can stay warm across edits.
You do not need to manage workspace IDs yourself.

## Stop compute

When finished:

```sh
leanwarp stop
```

Warm compute remains billable until stopped or retired by the server. If an
operation is still running, wait for it or cancel it and wait before stopping.
If a response is lost, follow [recovery](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#recovery)
before submitting replacement work.

Next: [agent setup](agents.md) or the [reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md).

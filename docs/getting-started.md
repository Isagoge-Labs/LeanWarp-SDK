# Getting started

[Install the CLI](../README.md#install), then get an API key from your
LeanWarp dashboard. Your account needs available credit to run compute.

Keys from the [isagoge.in dashboard](https://isagoge.in/dashboard) use the
production service. Keys issued for test access use the hosted test service.
The key alone selects the service; there is no API URL to configure.

If your shell cannot find `leanwarp` after installation, run `uv tool update-shell`
and open a new terminal.

## Sign in

Sign in with your key:

```sh
leanwarp auth login
```

Paste the key at the hidden prompt. The SDK selects the service automatically.
For automation, inject `LEANWARP_API_KEY` through your environment or secret manager. Keep the key
out of source files and agent conversations.

## Connect your project

For an existing project, keep its dependencies unchanged and run the commands
below. Compatibility requires the exact toolchain and dependency lockfile of a
supported bundle, including the lockfile's bytes. Equivalent version numbers
alone are not enough. If `doctor` reports a mismatch, the project cannot run on
that bundle; use a separate supported project or request support for its environment.
Do not replace an existing project's lockfile just to pass this check.

For a first experiment, the repository includes an example project for each Lean
environment: [Lean 4.26](../examples/lean-4.26) and [Lean 4.34](../examples/lean-4.34).
Each has the exact metadata and `LeanWarpExample.lean`. `leanwarp doctor` reports
whether the service currently offers that environment. Clone the SDK repository
and use one of these directories as your project:

```sh
git clone https://github.com/Isagoge-Labs/LeanWarp-SDK.git
cd LeanWarp-SDK/examples/lean-4.26
```

From your chosen project root:

```sh
leanwarp doctor
leanwarp connect
```

`doctor` checks whether the project's Lean toolchain and dependency lockfile match
a supported bundle. Continue only if it reports `compatible: true`. LeanWarp does
not build arbitrary project dependencies.

`connect` saves a workspace without starting compute. Manage funding and view
usage in the website. You can set an optional workspace lifetime cap under
Dashboard → Usage & credits before running work. Execution requires available prepaid credit. Add
`.leanwarp/` to `.gitignore` to keep local session state out of version control.

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

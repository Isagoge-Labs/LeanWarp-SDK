# Getting started

This guide takes you from an API key to a verified proof in a few minutes. You
need the `leanwarp` command ([install](../README.md#install)); you don't need Lean
installed locally.

If your shell can't find `leanwarp` after installing, run `uv tool update-shell`
and open a new terminal.

## 1. Sign in

Create an API key in the [dashboard](https://isagoge.in/dashboard), then run:

```sh
leanwarp auth login
```

Paste the key at the hidden prompt. In CI or other automation, set
`LEANWARP_API_KEY` from your secret manager instead. Keep keys out of source files
and agent conversations.

Running operations uses prepaid credit, which you add in the dashboard.

## 2. Choose a project

LeanWarp uploads only your `.lean` files and runs them in an **environment**: a
Lean release with Mathlib, already built on LeanWarp's workers. List them with:

```sh
leanwarp environments
```

The quickest start is an example project that matches an environment. Copy one of
the [examples](../examples) and work inside it:

```sh
git clone https://github.com/Isagoge-Labs/LeanWarp-SDK.git
cd LeanWarp-SDK/examples/lean-4.26
```

For your own project, run `leanwarp doctor` from its root. It reports the
environment the project will use, without starting compute:

- a project whose `lean-toolchain` and `lake-manifest.json` match an environment
  uses it, so results agree with your local build;
- a folder of `.lean` files with neither file uses the only served environment, or the newest served stable Lean release when there are several;
- a project that matches none is told which environments exist. Pass
  `--environment NAME` to `doctor` and `connect` to use one anyway; your files then
  compile against that environment's Lean and Mathlib.

When several environments share the newest stable Lean release, choose one
explicitly. A lockfile must use a supported format with pinned Git dependencies;
local path dependencies are not uploaded. `doctor` reports the saved environment
of an already connected project, even when the default later changes.

Then create the project's workspace. This starts no compute:

```sh
leanwarp connect
```

Add `.leanwarp/` to `.gitignore`; it holds this project's connection.

## 3. Check and verify

`LeanWarpExample.lean` in the example contains:

```lean
theorem add_zero_example (n : Nat) : n + 0 = n := by
  rfl
```

Check it, then verify that the theorem proves the statement you intend:

```sh
leanwarp check LeanWarpExample.lean
```

On `success: null` (exit `3`), run `leanwarp wait` until the check finishes.
Then verify:

```sh
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
```

The first operation starts a worker, which takes a little longer; later ones reuse
it. Each command uploads changed files, waits up to 30 seconds and prints the
result. Abbreviated:

```json
{
  "success": true,
  "kind": "verify_target",
  "state": "completed",
  "revision": 1,
  "result": {
    "result": {
      "status": "ok",
      "receipt": {
        "policy": "fixed_target_kernel_check_v1",
        "candidate_declaration": "add_zero_example"
      }
    }
  }
}
```

`success` is `true` when the check passed or the proof was verified and `false`
when it did not; Lean's messages are in `result.result`. If the work takes longer
than the wait, `success` is `null` and the exit code is `3`: run `leanwarp wait`
for the result. Don't submit it again.

Edit the file and run the command again. The same workspace and warm worker are
reused, so later checks start where the last one ended.

## 4. Verify a Mathlib statement

When the statement needs Mathlib or your own definitions, put what it needs in a
context file. Save this as `SqNonneg.lean`:

```lean
import Mathlib

theorem sq_nonneg_example (x : ℝ) : 0 ≤ x ^ 2 := by
  positivity
```

and the statement's imports as `statement-context.txt`:

```lean
import Mathlib
```

Then verify:

```sh
leanwarp verify SqNonneg.lean --declaration sq_nonneg_example \
  --target '∀ x : ℝ, 0 ≤ x ^ 2' --context-file statement-context.txt
```

LeanWarp elaborates the statement on its own, with only that context, and checks
the proof against it with Lean's kernel. The proof can't change what the
statement means. `sorry`, `admit`, `native_decide`, `set_option`, custom
elaborators and new axioms are rejected.

## 5. Stop the worker

```sh
leanwarp stop
```

A worker also stops on its own after 5 minutes without activity. You pay for the
time it runs, including those idle minutes, so stop it when you're done. Your
files stay in the workspace for next time.

Next: [set up an agent](agents.md), or read the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md) for every
command, limit and recovery step.

# Getting started

This guide takes you from an API key to a verified proof in a few minutes. You
need the `leanwarp` command ([install](../README.md#install)), but you don't need
Lean on your machine.

If your shell can't find `leanwarp` after you install it, run
`uv tool update-shell` and open a new terminal.

You can check whether a project will run on LeanWarp before you sign up: run
`leanwarp doctor` in the project's folder. Without a key, it compares your
project with the environments LeanWarp offers. It uploads nothing.

## 1. Sign in

Run this in your own terminal:

```sh
leanwarp auth login
```

It opens the [dashboard](https://isagoge.in/dashboard?section=keys), where you
create an API key. Paste the key at the prompt; it stays hidden as you type. Add
`--no-browser` to print the link instead of opening it.

In CI or other automation, set `LEANWARP_API_KEY` from your secret manager
instead. Never put a key in a source file or in a conversation with an agent.

Commands that run Lean use prepaid credit, which you add in the dashboard.

## 2. Choose a project

LeanWarp uploads only your `.lean` files. It runs them in an **environment**: a
Lean release with fixed versions of libraries such as Mathlib, already built on
LeanWarp's servers. To see the environments:

```sh
leanwarp environments
```

The quickest way to start is an example project. Copy one of the
[examples](../examples) and work inside it:

```sh
git clone https://github.com/Isagoge-Labs/LeanWarp-SDK.git
cd LeanWarp-SDK/examples/lean-4.35
```

For your own project, run `leanwarp doctor` in its folder. It tells you which
environment the project will use, without starting a server:

- If the project's `lean-toolchain` and `lake-manifest.json` match an
  environment, LeanWarp uses that one, so your results match your local build.
- If the folder has neither file, LeanWarp uses the newest stable Lean it offers.
- If nothing matches, `doctor` lists the environments that exist and reports
  `compatible: false`. To use one anyway, pass `--environment NAME` to
  `doctor` and `connect`. Your files then compile against that environment's
  Lean and Mathlib.

When several environments offer the newest Lean, choose one with
`--environment`. Once a project is connected, it keeps its environment, even if
the default changes later.

LeanWarp reads both Git and local path dependencies in `lake-manifest.json`, in
newer manifest formats too. Git dependencies must be pinned to full commit
hashes. LeanWarp does not upload or build dependency folders: the environment
must already provide every library your files import.

Then create the project's workspace. This doesn't start a server or cost anything:

```sh
leanwarp connect
```

Add `.leanwarp/` to `.gitignore`. It holds this project's connection.

A project can upload at most 256 `.lean` files and 1 MiB in total.
`leanwarp files` lists what will upload. Files listed in `.gitignore` are left
out. To leave out more, such as an archive folder, list it in
`.leanwarpignore` in the project's root, using the same syntax as `.gitignore`
(`archive/`).

## 3. Check and verify

The example's `LeanWarpExample.lean` contains:

```lean
theorem add_zero_example (n : Nat) : n + 0 = n := by
  rfl
```

First check the file:

```sh
leanwarp check LeanWarpExample.lean
```

Each command waits up to 30 seconds. If the check returns `success: null`
(exit code `3`), run `leanwarp wait` to wait for that same check to finish;
don't submit it again. If it fails, fix the reported errors and check again.

Once the check succeeds, verify that the theorem proves the statement you mean:

```sh
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
```

The first command starts a server, called a worker, so it takes a little
longer. Later commands reuse it. Each command uploads the files you changed,
waits up to 30 seconds and prints the result. A shortened example:

```json
{
  "success": true,
  "kind": "verify_target",
  "state": "completed",
  "revision": 1,
  "result": {
    "result": {
      "file": "LeanWarpExample.lean",
      "status": "ok",
      "receipt": {
        "policy": "fixed_target_kernel_check_v1",
        "candidate_declaration": "add_zero_example"
      }
    }
  },
  "execution": {"worker": "warm"},
  "timing": {"queued_ms": 3, "worker_ms": 41, "upload_ms": 18, "run_ms": 612}
}
```

How to read it:

- `success` is `true` when the check passed or the proof was verified, and
  `false` when it wasn't. Lean's messages are in `result.result`.
- If the command takes longer than the wait, `success` is `null` and the exit
  code is `3`. Run `leanwarp wait` to get the result. Don't run the command
  again.
- `execution` says what LeanWarp reused. Here, `"worker": "warm"` means the
  worker the check started was still running, with Lean loaded.
- `timing` says where the time went.

While you edit, use `leanwarp check --draft LeanWarpExample.lean`. The worker
keeps the file open, so after each edit Lean rechecks only what changed, and
`sorry` is just a warning. A plain `check` rechecks the whole file, as a final
build would. Run it, or `verify`, when you're done.

## 4. Verify a Mathlib statement

When the statement needs Mathlib or your own definitions, put what it needs in a
context file. Save this as `SqNonneg.lean`:

```lean
import Mathlib

theorem sq_nonneg_example (x : ℝ) : 0 ≤ x ^ 2 := by
  positivity
```

and save the statement's imports as `statement-context.txt`:

```lean
import Mathlib
```

Then verify:

```sh
leanwarp verify SqNonneg.lean --declaration sq_nonneg_example \
  --target '∀ x : ℝ, 0 ≤ x ^ 2' --context-file statement-context.txt
```

LeanWarp reads the statement on its own, with only that context, and checks the
proof against it with Lean's kernel. So the proof can't change what the
statement means. Proofs that use `sorry`, `admit`, `native_decide`,
`set_option`, custom elaborators or new axioms are rejected.

## 5. Stop the worker

```sh
leanwarp stop
```

A worker also stops by itself after it has been idle for a while;
`leanwarp resources` shows how long. You pay for the time it runs, idle minutes
included, so stop it when you're done. Closing your terminal does not stop it.

Stopping keeps your uploaded files and results in the workspace. The next
command starts a new worker.

Next: [set up an agent](agents.md), or read the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md) for every
command, limit and recovery step.

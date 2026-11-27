# Getting started

This guide takes you from an API key to a verified proof in a few minutes. You
need the `leanwarp` command, but you don't need Lean on your machine. Install it
with [uv](https://docs.astral.sh/uv/getting-started/installation/), on Linux,
macOS or WSL with Python 3.10 to 3.14:

```sh
uv tool install leanwarp && leanwarp setup
```

If your shell can't find `leanwarp` after you install it, run
`uv tool update-shell` and open a new terminal.

You can check whether a project will run on LeanWarp before you sign up: run
`leanwarp doctor` in the project's folder. Without a key, it compares your
project with the environments LeanWarp offers. It uploads nothing.

New connections require LeanWarp SDK 0.2.10 or later. Check your installed version with `leanwarp --version`.

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

LeanWarp uploads only your `.lean` files to a workspace. It runs them in your
project's own **environment**, built once from what a local `lake build` reads:
`lean-toolchain`, the lakefile, `lake-manifest.json` and any path dependencies.
A path dependency sends its files except what its own `.gitignore` and
`.leanwarpignore` leave out, build outputs, version control and credential
files; `leanwarp files --dependencies` lists them. LeanWarp fetches pinned Git
dependencies itself and never runs Lake on your machine. To see the supported
Lean toolchains:

```sh
leanwarp versions
```

For a first proof, create an empty folder outside any Lake project. Save this as
`LeanWarpExample.lean` in that folder:

```lean
theorem add_zero_example (n : Nat) : n + 0 = n := by
  rfl
```

From that folder run:

```sh
leanwarp connect
```

Plain Lean files use a ready environment with Mathlib. Connecting them is free;
the first proof operation starts a worker and uses prepaid credit.

**Using your own Lake project?** Run `leanwarp doctor` in its folder to check
the toolchain. Where project builds are available, `leanwarp connect` uses
prepaid credit to build a new environment and reuses a matching ready one for
free. A workspace spending cap applies to workspace operations; the environment
build is billed separately. If the service returns `project_builds_unavailable`,
you can still try the plain-file example above.

A Lake project needs a `lake-manifest.json`; run `lake update` locally if it is
missing. Git dependencies use pinned commits. To use a private GitHub repository,
connect it in the dashboard with a GitHub account that administers it. If access
is paused, reconnect it there to use your existing workspaces again. Path dependencies are sent
by content. If a build fails, fix the reported problem and run
`leanwarp connect --retry`.

Add `.leanwarp/` to `.gitignore`. It holds this project's connection.

A workspace source sync accepts at most 256 `.lean` files and 1 MiB in total.
The Lean options your `lakefile` sets, such as `maxSynthPendingDepth`, apply to
every check and verification, as Lake would apply them. Options that could weaken
checking, such as `debug.*` and `warn.sorry`, are not applied: `leanwarp status`
lists them under `lean_options_ignored`. LeanWarp checks every file with one set
of options, so an option that only some of your libraries or executables set, or
that they set differently, is listed there too instead of reaching the others. A `lakefile.lean`
must write its options as literal lists, or definitions of them, for the SDK to read them
from your local file. If LeanWarp cannot read every option the lakefile sets, including a
`-D` argument in `moreLeanArgs`, it applies none and lists `leanOptions` there.

`leanwarp files` lists workspace source files for sync. Environment inputs are sent separately. Files listed in `.gitignore` are left
out. To leave out more, such as an archive folder, list it in
`.leanwarpignore` in the project's root, using the same syntax as `.gitignore`
(`archive/`).

## 3. Check and verify

Check the file from step 2:

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
  "decided": true,
  "kind": "verify_target",
  "state": "completed",
  "revision": 1,
  "result": {
    "result": {
      "file": "LeanWarpExample.lean",
      "status": "ok",
      "receipt": {
        "policy": "fixed_target_kernel_check_v2",
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
- `decided` is `false` when there was no verdict: the check timed out
  (`timeout`), LeanWarp could not complete it (`infrastructure_failure`) or it
  was not checked (`not_checked`). The exit code is then `4`: run it again rather
  than changing the proof. A wrong proof is `decided: true` with exit code `1`.
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
proof against it with Lean's kernel, in a separate checker that never runs your
source. So the proof can't change what the statement means. The context may
import only the environment's packages, such as Mathlib. Proofs that use
`sorry`, `admit`, `native_decide` or new axioms are rejected.

### Verify a dataset of proofs

From Python, `verify_many` checks each proof against its own statement and
writes one JSON line per verdict as it arrives:

```python
from leanwarp_cloud import LeanWarpCloud

items = [
    {
        "id": "sq-1",
        "proof": "import Mathlib\ntheorem t (x : ℝ) : 0 ≤ x ^ 2 := by positivity",
        "target": {
            "context": "import Mathlib",
            "statement": "∀ x : ℝ, 0 ≤ x ^ 2",
            "declaration": "t",
        },
    },
]
with LeanWarpCloud(api_key) as client:
    summary = client.verify_many(
        items, output="verdicts.jsonl", environment_id=environment_id, workspaces=4
    )
```

If the run stops, run it again with the same `output`: written items are
skipped, and a chunk that was running is read back rather than run and charged
again. A resumed run must give each item the same proof and target, and the same
`strict`, `timeout` and `timeout_seconds`; if any differ, it stops before sending
anything, so write changed items to a new `output`. An output is verified in one
environment: once its first proof chunk is journaled or a verdict written, a
resumed run with another `environment_id`, or with workspaces in another, is
refused. One run at a time uses an output; a second one started
meanwhile is refused at once. Each line keeps the item's `id`, `passed`,
`decided`, `environment_id`, the `target_sha256` of the statement it was checked
against and the `input_sha256` of its proof, target and settings. `decided` is
false for `timeout`, `infrastructure_failure` and `not_checked`: no verdict was
reached, and the summary counts these as `undecided`, not `failed`. A resumed run
skips them too, so write those items to a new `output` to try them again.
`AsyncLeanWarpCloud` offers every client method as a coroutine.

## 5. Stop the worker

```sh
leanwarp stop
```

A worker also stops by itself after it has been idle for a while;
`leanwarp resources` shows how long. You pay for the time it runs, idle minutes
included, so stop it when you're done. Closing your terminal does not stop it.

Stopping keeps your uploaded files and results in the workspace. The next
command starts a new worker.

## 6. Check a project in CI

Three commands check every file in a fresh checkout and leave nothing behind:

- `leanwarp connect --ephemeral` creates a workspace for this run alone.
- `leanwarp check --all` checks every file that uploads, strictly, in as many
  batches of 64 as it needs, and prints one result. It waits until the last
  batch finishes; its exit code is `0` if every file passed, `1` if any failed,
  `4` if a file was left without a verdict (timed out or not completed), and `3`
  if the work outlived the wait.
- `leanwarp stop --delete` stops the worker, cancels any running operation, then
  deletes the workspace and its results. It deletes only the workspace that
  `--ephemeral` created. If a request whose response was lost cannot be resent,
  it leaves the workspace stopped, keeps the request for `leanwarp recover`, and
  exits `2`; `errors` says which step failed.

A GitHub Actions job, with your key saved as the secret `LEANWARP_API_KEY`:

```yaml
jobs:
  lean:
    runs-on: ubuntu-latest
    env:
      LEANWARP_API_KEY: ${{ secrets.LEANWARP_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv tool install leanwarp
      - run: leanwarp connect --ephemeral --max-spend 1
      - run: leanwarp check --all
      - if: always()
        run: leanwarp stop --delete
```

Any other operation takes `--block` to wait until it finishes in the same way,
for example `leanwarp verify Main.lean --declaration NAME --target '…' --block`,
and so does `leanwarp wait --block`. A blocking wait ends at the operation's
`--execution-timeout` (600 seconds unless you set one) plus five minutes to start
a worker.

Next: [set up an agent](agents.md), or read the
[reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md) for every
command, limit and recovery step.

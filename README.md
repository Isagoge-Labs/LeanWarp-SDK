<h1><img src="https://raw.githubusercontent.com/Isagoge-Labs/LeanWarp-SDK/main/docs/assets/leanwarp-banner.svg" alt="LeanWarp by Isagoge Labs" width="960"></h1>

LeanWarp is hosted Lean 4 for formal verification. It checks and verifies proofs
on workers that stay warm between edits, against Mathlib or the exact libraries
your Lake project pins, private GitHub repositories included. This SDK is how you
and your agents use it: the `leanwarp` command, an MCP server and a Python
library.

**[Get started](https://isagoge.in/leanwarp/docs/getting-started)** · [Agent setup](https://isagoge.in/leanwarp/docs/agents) ·
[Reference](https://isagoge.in/leanwarp/docs/reference) · [HTTP API](https://isagoge.in/leanwarp/docs/http-api) ·
[Documentation](https://isagoge.in/leanwarp/docs) · [Website](https://isagoge.in/leanwarp)

<!-- mcp-name: io.github.Isagoge-Labs/leanwarp -->

## What it does

| Operation | What you get | CLI |
| --- | --- | --- |
| Check | Lean's errors and warnings for a file; `--draft` reuses Lean's work on the unchanged part while you edit. | `leanwarp check` |
| Inspect | Lean’s goals and hypotheses at a tactic or proof hole. | `leanwarp inspect` |
| Try tactics | Several tactics from the same captured goal: which close it, which leave goals, which fail. The file is not edited. | `leanwarp try-tactics` |
| Verify | Whether a declaration proves exactly the statement you give, checked by Lean's kernel. | `leanwarp verify` |
| Holes | Every `sorry` and `?_` in a file, with its position, and the goals of the first 16. | `leanwarp holes` |
| Proof states | Every tactic in a file with its goals before and after, a page at a time, in one call instead of one `inspect` per tactic. | `leanwarp proof-states` |
| Suggest | A tactic that closes a hole, accepted only if nothing else changes and the proof gains no unproved lemma or extra axiom. `apply` writes it into the file. | `leanwarp suggest`, `leanwarp apply` |
| Sorry to lemma | The proof of each theorem that still has a `sorry` moved into a new named lemma the theorem then uses; the lemma keeps its `sorry`. `apply` writes it. | `leanwarp sorry2lemma` |
| Have to lemma | Each named `have` in a theorem's proof moved into its own lemma, which the `have` then uses. `apply` writes it. | `leanwarp have2lemma` |
| Simplify proofs | Theorem proofs replaced by shorter tactic proofs that still check. `apply` writes it. | `leanwarp simplify-proofs` |
| Repair proofs | Try tactics on `sorry` holes within the repair limit and time budget. If `source_changed` is true, run `apply`, then `check` to see what remains. | `leanwarp repair-proofs` |
| Declarations | The declarations a file adds, with their types and dependencies. | `leanwarp declarations` |
| Check files | Up to 64 files checked in one operation. | `leanwarp check-files` |
| Verify batch | Up to 64 inline proofs checked in one operation, without uploading them as files; each result keeps its `custom_id`. | `leanwarp verify-batch` |
| Verify a dataset | Every proof in a dataset verified against its own statement, over several workspaces, with verdicts streamed to a file and a rerun resuming where it stopped. | Python `verify_many` |

For a hole-free proof in `Main.lean`, inspect the start of `exact` on line 2.

```lean
example (h : True) : True := by
  exact h
```

`leanwarp inspect Main.lean --line 2 --column 3` shows the goal `True` and
the hypothesis `h : True`. A `goal_status` of `no_goals` means the selected
tactic state has no remaining goals. With `omitted`, the goals did not fit
and `context_omitted.goal_contexts` gives their count.

A per-tactic timeout above 60,000 ms is capped at 60,000 ms.
Tactic trials default to 30 seconds plus the sum of those tactic
budgets and 100 ms, rounded up and bounded to 120–600 seconds. An explicit
operation limit shorter than the sum scales down the per-tactic budgets.

The [reference](https://isagoge.in/leanwarp/docs/reference#commands)
lists every command and option; `leanwarp skill --reference` prints it.

LeanWarp returns a rewritten file only after checking it: no errors, every
statement unchanged, nothing added but the reported lemmas, and every theorem a
repair or simplification edits complete, with no axiom beyond Lean's own.

## Install in two steps

New connections and the examples below require `leanwarp` 0.2.10 or later.

**1. Install.** In a terminal, with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install leanwarp && leanwarp setup
```

This installs the `leanwarp` command and its MCP server from
[PyPI](https://pypi.org/project/leanwarp/). `leanwarp setup` adds the LeanWarp
skill and MCP server to Claude Code, Codex and Cursor where they are installed,
and the skill to `~/.agents/skills` if that folder exists; `leanwarp setup
--remove` takes them out again. It runs on Linux, macOS and WSL, with Python
3.10 to 3.14. You don't need Lean installed: LeanWarp runs Lean and Mathlib for
you.

Or paste this into your agent:

> Install LeanWarp: run `uv tool install leanwarp && leanwarp setup`, then ask me
> to sign in.

Already have LeanWarp 0.2 from GitHub? Its `leanwarp-sdk` tool owns the
`leanwarp` command, so replace it instead:

```sh
uv tool uninstall leanwarp-sdk && uv tool install leanwarp && leanwarp setup
```

**2. Sign in**, in your own terminal:

```sh
leanwarp auth login
```

It opens the dashboard to create an API key; paste the key at the hidden prompt.
Never paste it into an agent conversation. Then restart your agent.

When a new release is out, results include `update_available`; run
`leanwarp update` to upgrade and refresh the installed skills.

## Quick start

Save [LeanWarpExample.lean](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/examples/lean-4.35/LeanWarpExample.lean) by itself in a
folder outside any Lake project and run these commands there. Plain files connect
for free. Checking them uses prepaid credit.

```sh
leanwarp doctor              # which environment this project runs on
leanwarp connect             # create the project's workspace
leanwarp check LeanWarpExample.lean
```

If a command returns `success: null` (exit `3`), run `leanwarp wait` until it
finishes before submitting another operation. Then verify:

```sh
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
```

Wait for that operation to finish too, then run `leanwarp stop` when you are done.

Every command prints one JSON object. Results start with `success`: `true` when
the check passed or the proof was verified, `false` when it did not, and `null`
while the work is still running, in which case run `leanwarp wait`. `decided` is
`false` when there was no verdict (a timeout, or a check LeanWarp could not
complete): retry it rather than changing the proof. The exit code says the same
thing: `0` passed, `1` did not pass, `2` the command failed, `3` still running,
`4` no verdict. For `connect`, exit `3` means only its wait ended: run `connect`
again, not `wait`.

In CI, `leanwarp connect --ephemeral`, `leanwarp check --all` and, always last,
`leanwarp stop --delete` check every file and leave nothing behind; see the
[GitHub Actions recipe](https://isagoge.in/leanwarp/docs/getting-started#6-check-a-project-in-ci).

## Use with an agent

Give your agent the project directory and this instruction:

> Run `leanwarp skill` and follow it. Use LeanWarp to check this Lean project
> and verify proofs against their intended statements. Reuse the workspace
> while you edit, and run `leanwarp stop` when you are done.

For MCP clients, add the server `leanwarp --project /absolute/path/to/project mcp`,
or `uvx leanwarp --project /absolute/path/to/project mcp`
without installing. It uses the same key and project state as the CLI. See [agent setup](https://isagoge.in/leanwarp/docs/agents).
Keep API keys out of prompts, source files and tool arguments.

## Your project and its environment

An **environment** is the Lean toolchain and built libraries your proofs compile
against. You don't pick one from a list: your project defines it.

- **Plain `.lean` files** outside any Lake project run on a ready-built Mathlib
  on the newest supported Lean, or the supported toolchain a `lean-toolchain`
  beside them names. Nothing to build.
- **A Lake project** runs against the Lean toolchain and libraries it pins:
  `leanwarp connect` sends its `lean-toolchain`, lakefile, `lake-manifest.json`
  and any path dependencies, LeanWarp builds the pinned libraries once, and
  every later `connect` with the same inputs reuses them.
  Lake never runs on your machine. A path dependency sends its files except
  what its own `.gitignore` and `.leanwarpignore` leave out, build outputs,
  version control and credential files; `leanwarp files --dependencies` lists
  them.
- **Private GitHub dependencies** are built like the rest once you connect them
  in the [dashboard](https://isagoge.in/dashboard), under Private repositories,
  with a GitHub account that administers them. Until then `connect` stops with
  `grant_required`; once access is connected, run `leanwarp connect --retry`.

`connect` waits up to four hours for a build; `--timeout SECONDS` changes that.
If the wait ends first (exit `3`, `connect_timeout`), run `connect` again with
the same inputs: if a build is under way, it rejoins it and answers any files
it asks for.
If a build fails, the error names the package or file; fix it locally and run
`leanwarp connect --retry`. `leanwarp environments` lists your environments.

Only your `.lean` files are uploaded to the workspace. `leanwarp files` lists
them; patterns in `.gitignore` and `.leanwarpignore` leave files out.

### What a build reads

A Git dependency on a repository on your own machine is sent as Lake's checkout
of it, which must be at the commit the lockfile pins (run `lake build` once).
Path dependencies Lake copies (`copy = true`) work as they do locally.

If your lakefile reads files outside the project, `connect` answers those reads
while the build runs, so keep it running until it finishes. It answers only
inside your path dependencies, except what their ignore files leave out, and the
directories listed in `.leanwarpread` at the project's top level, one per line
relative to it (such as `../shared`); any other read stops the connect and names
the file. Files committed in their git
repository are read as the commit holds them. Edit any other file the build read
while `connect` runs and it stops, so you can run it again or commit.

Workers load the project from `lean-toolchain`, the lakefile and
`lake-manifest.json` alone. A lakefile that reads other files when Lake loads it
(outside the project, or files of the project itself) is refused with
`configuration_not_reproducible`, naming the file.

Each dependency is built as `lake build` of it would build it: its default
libraries. Modules only in its other libraries (tests, examples, documentation)
are not in the environment, and a check that imports one is refused with an
error naming the module and its package.

### Lean versions

The LeanWarp engine supports every stable Lean 4 release from 4.26.0.
`leanwarp doctor` shows whether your project's toolchain is supported, and
`leanwarp versions` lists the toolchains the service offers now. The ready-built Mathlib environments are
[listed on the website](https://isagoge.in/leanwarp/docs/environments). To start
a new Mathlib project, copy one of the
[examples](https://github.com/Isagoge-Labs/LeanWarp-SDK/tree/main/examples). If
you need a Lean release that isn't supported,
[open an issue](https://github.com/Isagoge-Labs/LeanWarp-SDK/issues) with your
project's `lean-toolchain`.

## Python

Install the library in your application's environment with `uv add leanwarp`
(or `pip install leanwarp`). To verify a dataset, give each proof its own
statement:

```python
from leanwarp_cloud import load_client

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
with load_client() as client:
    # The ready Mathlib environment. Save its ID and pass the same one to resume.
    environment_id = client.toolchain_environment()["environment_id"]
    summary = client.verify_many(
        items, output="verdicts.jsonl", environment_id=environment_id
    )
```

Each verdict is written to `verdicts.jsonl` as it arrives. If the run stops,
run it again with the same `output` and `environment_id`: finished items are
skipped, and a chunk that was running is recovered from the `.journal` file
beside it where it can be (the reference says when). Proofs may
import the environment's packages but not a project's own modules; for a Lake
project's libraries, use its `environment_id` from `leanwarp environments`.
`ProjectSession` drives a project the way the CLI does,
and `AsyncLeanWarpCloud` offers every client method as a coroutine; see the
[Python reference](https://isagoge.in/leanwarp/docs/reference#python).

## Billing

You pay from prepaid credit, billed by the second while a worker runs. Run
`leanwarp resources` to see the published worker's starting minute price, whether
charges depend on usage, startup credit hold and idle timeout. LeanWarp selects
the worker. Use `leanwarp connect --max-spend 2` to create a workspace with a $2
cap, or edit an existing cap in the dashboard. When available, a first-time Lake
project build uses prepaid credit separately from this cap. Reusing a ready environment is free. Reconnecting without a cap keeps
the saved cap.

When the worker uses usage billing, CPU and RAM charges increase independently
above their reserved baselines. The starting rate is a paid minimum while the
worker runs, including idle time. Bursting depends on available host capacity.
Otherwise, the listed minute rate applies for the time the worker runs. Existing
workspaces keep their saved billing terms.

A worker starts with your first operation and stays warm between calls. Run
`leanwarp stop` when finished; closing the SDK client or your terminal does not
stop it. Unused held credit is released once shutdown and final usage are confirmed.
Stopping keeps your uploaded files and
completed results; the next operation reconstructs the worker. Set a spending cap
for each workspace in the [dashboard](https://isagoge.in/dashboard).

LeanWarp is in beta. For more capacity, larger workers, higher limits or a custom
setup for your team, email contact@isagoge.in or tell us with `leanwarp feedback`.

## Concepts

| Term | Meaning |
| --- | --- |
| Environment | The Lean toolchain and libraries a workspace uses. Plain Lean files use a ready environment with Mathlib. |
| Workspace | Your project's files on LeanWarp. One per project; creating it starts no compute. |
| Worker | The machine that runs Lean for a workspace. It starts on demand and stays warm. |
| Operation | One request, such as a check or a verification, and its result. |
| Revision | A numbered version of the workspace's files. Each result names the one it checked. |
| Receipt | The part of a verification result that records exactly what was checked. |

## Documentation

| Start here | Reference |
| --- | --- |
| [Verify your first proof](https://isagoge.in/leanwarp/docs/getting-started) | [Commands](https://isagoge.in/leanwarp/docs/reference#commands) |
| [Set up an agent with the CLI or MCP](https://isagoge.in/leanwarp/docs/agents) | [Results and exit codes](https://isagoge.in/leanwarp/docs/reference#results) |
| [Use the Python library](https://isagoge.in/leanwarp/docs/reference#python) | [Recover an interrupted request](https://isagoge.in/leanwarp/docs/reference#recovery) |
| [Call the HTTP API directly](https://isagoge.in/leanwarp/docs/http-api) | [Credentials, credit and limits](https://isagoge.in/leanwarp/docs/reference#credentials-credit-and-limits) |

The agent instructions ship with the package: `leanwarp skill` prints the
workflow and `leanwarp skill --reference` the full reference. Run
`leanwarp --help` or `leanwarp COMMAND --help` for arguments.

<h1><img src="https://raw.githubusercontent.com/Isagoge-Labs/LeanWarp-SDK/main/docs/assets/leanwarp-banner.svg" alt="LeanWarp" width="960"></h1>

LeanWarp checks and verifies Lean 4 proofs on hosted workers that stay warm
between edits. This SDK is how you and your agents use it: the `leanwarp`
command, an MCP server and a Python library.

**[Get started](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/getting-started.md)** · [Agent setup](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/agents.md) ·
[Reference](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md) ·
[HTTP API](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/http-api.md) · [Website](https://isagoge.in/leanwarp)

## What it does

| Operation | What you get | CLI |
| --- | --- | --- |
| Check | Lean's errors and warnings for a file; `--draft` reuses Lean's work on the unchanged part while you edit. | `leanwarp check` |
| Inspect | The goals and local context at a position in a proof. | `leanwarp inspect` |
| Try tactics | Several tactics from the same captured goal: which close it, which leave goals, which fail. The file is not edited. | `leanwarp try-tactics` |
| Verify | Whether a declaration proves exactly the statement you give, checked by Lean's kernel. | `leanwarp verify` |
| Holes | Every `sorry` and `?_` in a file, with its position, and the goals of the first 16. | `leanwarp holes` |
| Suggest | A tactic that closes a hole, accepted only if nothing else changes and the proof gains no unproved lemma or extra axiom. `apply` writes it into the file. | `leanwarp suggest`, `leanwarp apply` |
| Sorry to lemma | The proof of each theorem that still has a `sorry` moved into a new named lemma the theorem then uses; the lemma keeps its `sorry`. `apply` writes it. | `leanwarp sorry2lemma` |
| Declarations | The declarations a file adds, with their types and dependencies. | `leanwarp declarations` |
| Check files | Up to 64 files checked in one operation. | `leanwarp check-files` |

The [reference](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#commands)
lists every command and option; `leanwarp skill --reference` prints it.

### Coming soon

These are planned and not available yet:

- **Proof simplification**: replace a proof with a shorter one that still checks.
- **`have` extraction**: lift named `have` blocks into top-level lemmas.
- **Batch repair**: fill several holes in one operation.

## Install in two steps

**1. Install.** In a terminal, with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install leanwarp && leanwarp setup
```

This installs the `leanwarp` command and its MCP server from
[PyPI](https://pypi.org/project/leanwarp/). `leanwarp setup` adds the LeanWarp
skill and MCP server to Claude Code, Codex and Cursor where they are installed,
and the skill to `~/.agents/skills`. It runs on Linux, macOS and WSL. You don't
need Lean installed: LeanWarp runs Lean and Mathlib for you.

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

From your Lean project or one of the [examples](https://github.com/Isagoge-Labs/LeanWarp-SDK/tree/main/examples):

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
while the work is still running, in which case run `leanwarp wait`. The exit code
says the same thing: `0` passed, `1` did not pass, `2` the command failed, `3`
still running.

## Use with an agent

Give your agent the project directory and this instruction:

> Run `leanwarp skill` and follow it. Use LeanWarp to check this Lean project
> and verify proofs against their intended statements. Reuse the workspace
> while you edit, and run `leanwarp stop` when you are done.

For MCP clients, add the server `leanwarp --project /absolute/path/to/project mcp`,
or `uvx leanwarp --project /absolute/path/to/project mcp`
without installing. It uses the same key and project state as the CLI. See [agent setup](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/agents.md).
Keep API keys out of prompts, source files and tool arguments.

## Your project and its environment

LeanWarp uploads only your `.lean` files. Lean, Mathlib and every other
dependency come from an **environment**: a Lean release with Mathlib, already
built on LeanWarp's workers. Run `leanwarp environments` to list them.
`leanwarp files` lists what would upload; patterns in `.gitignore` and
`.leanwarpignore` leave files out.

`leanwarp connect` picks the environment for you:

- If your project's `lean-toolchain` and `lake-manifest.json` match an
  environment, it uses that one, so results agree with your local build.
- If your project has neither file, as with a folder of `.lean` files, it uses
  the only served environment, or the newest served stable Lean release when there are several.
- If they match no environment, it stops and lists the ones available. Run
  `leanwarp connect --environment NAME` to check your files there anyway; they
  then compile against that environment's Lean and Mathlib.

To start a new project, copy one of the [examples](https://github.com/Isagoge-Labs/LeanWarp-SDK/tree/main/examples), which match the
environments LeanWarp serves.

## Billing

You pay from prepaid credit, billed by the second while a worker runs. Run
`leanwarp resources` to see the published worker's starting minute price, whether
charges depend on usage, startup credit hold and idle timeout. LeanWarp selects
the worker. Use `leanwarp connect --max-spend 2` to create a workspace with a $2
cap, or edit an existing cap in the dashboard. Reconnecting without a cap keeps
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

## Concepts

| Term | Meaning |
| --- | --- |
| Environment | A Lean release with Mathlib, prebuilt on LeanWarp, such as `lean-4.26-mathlib`. |
| Workspace | Your project's files on LeanWarp. One per project; creating it starts no compute. |
| Worker | The machine that runs Lean for a workspace. It starts on demand and stays warm. |
| Operation | One request, such as a check or a verification, and its result. |
| Revision | A numbered version of the workspace's files. Each result names the one it checked. |
| Receipt | The part of a verification result that records exactly what was checked. |

## Documentation

| Start here | Reference |
| --- | --- |
| [Verify your first proof](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/getting-started.md) | [Commands](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#commands) |
| [Set up an agent with the CLI or MCP](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/agents.md) | [Results and exit codes](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#results) |
| [Use the Python library](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#python) | [Recover an interrupted request](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#recovery) |
| [Call the HTTP API directly](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/http-api.md) | [Credentials, credit and limits](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/src/leanwarp_cloud/skills/leanwarp/references/usage.md#credentials-credit-and-limits) |

The agent instructions ship with the package: `leanwarp skill` prints the
workflow and `leanwarp skill --reference` the full reference. Run
`leanwarp --help` or `leanwarp COMMAND --help` for arguments.

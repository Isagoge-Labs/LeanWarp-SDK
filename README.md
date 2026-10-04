<h1><img src="docs/assets/leanwarp-banner.svg" alt="LeanWarp" width="960"></h1>

LeanWarp checks and verifies Lean 4 proofs on hosted workers that stay warm
between edits. This SDK is how you and your agents use it: the `leanwarp`
command, an MCP server and a Python library.

**[Get started](docs/getting-started.md)** · [Agent setup](docs/agents.md) ·
[Reference](src/leanwarp_cloud/skills/leanwarp/references/usage.md) ·
[HTTP API](docs/http-api.md) · [Website](https://isagoge.in/leanwarp)

## What it does

| Operation | What you get | CLI |
| --- | --- | --- |
| Check | Lean's errors and warnings for a file. | `leanwarp check` |
| Inspect | The goals and local context at a position in a proof. | `leanwarp inspect` |
| Try tactics | What each candidate tactic does, without editing the file. | `leanwarp try-tactics` |
| Verify | Whether a declaration proves exactly the statement you give, checked by Lean's kernel. | `leanwarp verify` |

### Coming soon

These operations are planned and not available yet:

- **Proof repair**: fill `sorry` placeholders with candidate proofs that Lean rechecks.
- **Proof simplification**: replace a proof with a shorter one that still checks.
- **Lemma extraction**: lift `sorry` goals or named `have` blocks into top-level lemmas.
- **Declaration extraction**: list the declarations a file adds, with their types.
- **Proof search**: explore tactic branches from a goal in the warm workspace.

## Install

With [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install --python 3.12 \
  'leanwarp-sdk @ git+https://github.com/Isagoge-Labs/LeanWarp-SDK.git'
```

This installs the `leanwarp` command and its MCP server. It runs on Linux, macOS
and WSL. You don't need Lean installed: LeanWarp runs Lean and Mathlib for you.

## Quick start

Create an API key in the [dashboard](https://isagoge.in/dashboard). Then, from
your Lean project or one of the [examples](examples):

```sh
leanwarp auth login          # paste the key at the hidden prompt
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

For MCP clients, add the server `leanwarp --project /absolute/path/to/project mcp`.
It uses the same key and project state as the CLI. See [agent setup](docs/agents.md).
Keep API keys out of prompts, source files and tool arguments.

## Your project and its environment

LeanWarp uploads only your `.lean` files. Lean, Mathlib and every other
dependency come from an **environment**: a Lean release with Mathlib, already
built on LeanWarp's workers. Run `leanwarp environments` to list them.

`leanwarp connect` picks the environment for you:

- If your project's `lean-toolchain` and `lake-manifest.json` match an
  environment, it uses that one, so results agree with your local build.
- If your project has neither file, as with a folder of `.lean` files, it uses
  the only served environment, or the newest served stable Lean release when there are several.
- If they match no environment, it stops and lists the ones available. Run
  `leanwarp connect --environment NAME` to check your files there anyway; they
  then compile against that environment's Lean and Mathlib.

To start a new project, copy one of the [examples](examples), which match the
environments LeanWarp serves.

## Billing

You pay for the time a worker runs, by the second, from prepaid credit. A
worker starts with your first operation, stays warm between calls, and stops
after 5 minutes without activity or when you run `leanwarp stop`. While it runs,
an hour of usage is held from your credit and the unused part is returned when it
stops. Set a spending cap for each workspace in the
[dashboard](https://isagoge.in/dashboard).

## Concepts

| Term | Meaning |
| --- | --- |
| Environment | A Lean release with Mathlib, prebuilt on LeanWarp, such as `lean-4.26-mathlib`. |
| Workspace | Your project's files on LeanWarp. One per project; creating it starts no compute. |
| Worker | The machine that runs Lean for a workspace. It starts on demand and stays warm. |
| Operation | One check, inspect, try-tactics or verify request and its result. |
| Revision | A numbered version of the workspace's files. Each result names the one it checked. |
| Receipt | The part of a verification result that records exactly what was checked. |

## Documentation

| Start here | Reference |
| --- | --- |
| [Verify your first proof](docs/getting-started.md) | [Commands](src/leanwarp_cloud/skills/leanwarp/references/usage.md#commands) |
| [Set up an agent with the CLI or MCP](docs/agents.md) | [Results and exit codes](src/leanwarp_cloud/skills/leanwarp/references/usage.md#results) |
| [Use the Python library](src/leanwarp_cloud/skills/leanwarp/references/usage.md#python) | [Recover an interrupted request](src/leanwarp_cloud/skills/leanwarp/references/usage.md#recovery) |
| [Call the HTTP API directly](docs/http-api.md) | [Credentials, credit and limits](src/leanwarp_cloud/skills/leanwarp/references/usage.md#credentials-credit-and-limits) |

The agent instructions ship with the package: `leanwarp skill` prints the
workflow and `leanwarp skill --reference` the full reference. Run
`leanwarp --help` or `leanwarp COMMAND --help` for arguments.

# Use LeanWarp with an agent

An agent uses LeanWarp through the `leanwarp` command or its MCP server. Both
share the same key and the same project state in `.leanwarp/`, so you can switch
between them. [Install and sign in](getting-started.md) first, then choose the Lean
project the agent may work in.

## Set up your agents

```sh
leanwarp setup
```

This installs the LeanWarp skill for Claude Code (`~/.claude/skills`), Codex
(`~/.codex/skills`) and other agents (`~/.agents/skills`), and adds the MCP
server to Claude Code (`claude mcp add`), Codex (`~/.codex/config.toml`) and
Cursor (`~/.cursor/mcp.json`). Only agents whose configuration folder exists are
set up; `--agent NAME` sets up one anyway, and `--no-mcp` installs skills only.
An existing `leanwarp` MCP entry is never replaced. Run `leanwarp auth login`
yourself, then restart the agent. `leanwarp update` upgrades the command and
refreshes the installed skills.

## The instruction

Give the agent the project directory and this instruction:

> Run `leanwarp skill` and follow it. Use LeanWarp to check this Lean project
> and verify proofs against their intended statements. Reuse the workspace
> while you edit, and run `leanwarp stop` when you are done.

`leanwarp skill` prints the workflow: connecting, checking, reading results,
verifying against a fixed statement, recovering from lost responses and stopping
the worker. It also asks the agent to report confirmed problems with LeanWarp
itself through `leanwarp feedback`, naming the operation instead of quoting your
source, and to tell you when it does. `leanwarp skill --reference` prints the full reference. Neither needs
a key or starts compute.

Sign in yourself with `leanwarp auth login`, or inject `LEANWARP_API_KEY` into the
agent's environment. Never put the key in the instruction.

## Agents with shell access

The CLI is built for agents: every command prints one JSON object, results start
with `success` (`true`, `false`, or `null` while running), and exit codes separate
"did not pass" (`1`) from "could not run" (`2`) and "still running" (`3`).

`leanwarp setup` installs the instructions as a skill agents discover
automatically. For another agent, point its skill installer at the
[skill directory](../src/leanwarp_cloud/skills/leanwarp), including `references/`.

## MCP

Add this server to your MCP client, with the absolute path of your project:

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

Use the absolute path of `leanwarp` if the client can't find it on its `PATH`.
To run the server without installing the command, use
[uv](https://docs.astral.sh/uv/getting-started/installation/) to fetch it from PyPI:

```json
{
  "mcpServers": {
    "leanwarp": {
      "command": "uvx",
      "args": ["leanwarp", "--project", "/absolute/path/to/lean-project", "mcp"]
    }
  }
}
```

The server starts even before you sign in, so the agent can read its guides; tools
that call LeanWarp then explain how to sign in.

If the user specifies a workspace budget, pass it to `connect` as
`max_spend_microusd` ($1 = 1,000,000). Reconnecting without that argument keeps
the existing cap; a supplied different cap is rejected. Existing caps can be
changed in the dashboard.

The agent should read the resources `leanwarp://guide` and `leanwarp://reference`
first. The tools mirror the CLI commands: setup (`doctor`, `files`,
`environments`, `connect`), proof operations (`check`, `inspect`, `try_tactics`,
`verify_target`, `holes`, `suggest`, `sorry2lemma`, `extract_decls`,
`check_files`, and `apply` to write a result into its file), operation control
(`wait`, `status`, `cancel`, `stop`, `recover`), and workspace and account
management. The [MCP reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#mcp)
lists the tools and their arguments. Operation tools wait up to 20 seconds for
their result by default (`wait_seconds`, at most 40).

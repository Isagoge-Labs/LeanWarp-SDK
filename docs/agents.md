# Use LeanWarp with an agent

An agent uses LeanWarp through the `leanwarp` command or its MCP server. Both use
the same key and the same project state in `.leanwarp/`, so the agent can switch
between them. [Install and sign in](getting-started.md) first, then choose the
Lean project the agent may work in.

## Set up your agents

```sh
leanwarp setup
```

This does two things for each agent it finds:

- It installs the LeanWarp skill: the instructions an agent follows. It goes in
  `~/.claude/skills` for Claude Code, `~/.codex/skills` for Codex and
  `~/.agents/skills` for other agents.
- It adds the MCP server to Claude Code (with `claude mcp add`), Codex
  (`~/.codex/config.toml`) and Cursor (`~/.cursor/mcp.json`).

Setup only touches agents whose configuration folder already exists. Use
`--agent NAME` to set up one anyway, or `--no-mcp` to install only the skills.
It never replaces an existing `leanwarp` MCP entry.

Then run `leanwarp auth login` yourself and restart the agent. Later,
`leanwarp update` upgrades the command and refreshes the installed skills.

## The instruction

Open the agent in your Lean project and give it this instruction:

> Run `leanwarp skill` and follow it. Use LeanWarp to check this Lean project
> and verify proofs against their intended statements. Reuse the workspace
> while you edit, and run `leanwarp stop` when you are done.

`leanwarp skill` prints the workflow the agent follows: connect, check, read
results, verify against a fixed statement, recover from lost responses and stop
the worker. `leanwarp skill --reference` prints the full reference. Neither needs
a key or costs anything.

The skill also asks the agent to report real problems with LeanWarp itself
through `leanwarp feedback`, and to tell you when it does. A report names the
command that went wrong; it doesn't quote your source.

Sign in yourself with `leanwarp auth login`, or give the agent's environment a
`LEANWARP_API_KEY`. Never put the key in the instruction.

## Agents with shell access

The command line is built for agents:

- Proof commands print a JSON result with `success`: `true`, `false`, or `null`
  while the operation is still running. On `null`, run `leanwarp wait` for the
  same operation; don't submit it again.
- Exit codes tell apart "did not pass" (`1`), "could not run" (`2`) and "still
  running" (`3`).

For an agent that `leanwarp setup` doesn't know, point its skill installer at
the [skill folder](../src/leanwarp_cloud/skills/leanwarp), including
`references/`.

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

If the client can't find `leanwarp`, use its absolute path. To run the server
without installing the command, let
[uv](https://docs.astral.sh/uv/getting-started/installation/) fetch it from PyPI:

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

The server starts even before you sign in, so the agent can read its guides.
Tools that need LeanWarp then explain how to sign in.

The agent should first read the resources `leanwarp://guide` and
`leanwarp://reference`. The tools match the CLI commands:

- **Setup:** `doctor`, `files`, `environments`, `connect`.
- **Proofs:** `check`, `inspect`, `try_tactics`, `verify_target`, `holes`,
  `suggest`, `sorry2lemma`, `extract_decls`, `check_files`, `verify_batch`, and
  `apply` to write a result into its file.
- **Control:** `wait`, `status`, `cancel`, `stop`, `recover`.
- **Workspaces and account:** the rest.

The [MCP reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#mcp)
lists every tool and its arguments. Proof tools wait up to 20 seconds for their
result by default; set `wait_seconds` up to 40.

### Spending caps

If you give the agent a budget for the workspace, it passes it to `connect` as
`max_spend_microusd` ($1 is 1,000,000). Reconnecting without that argument keeps
the existing cap, and a different cap is refused. You can change a cap in the
dashboard.

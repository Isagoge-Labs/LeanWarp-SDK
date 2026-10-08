# Use LeanWarp with an agent

An agent uses LeanWarp through the `leanwarp` command or its MCP server. Both
share the same key and the same project state in `.leanwarp/`, so you can switch
between them. [Install and sign in](getting-started.md) first, then choose the Lean
project the agent may work in.

## The instruction

Give the agent the project directory and this instruction:

> Run `leanwarp skill` and follow it. Use LeanWarp to check this Lean project
> and verify proofs against their intended statements. Reuse the workspace
> while you edit, and run `leanwarp stop` when you are done.

`leanwarp skill` prints the workflow: connecting, checking, reading results,
verifying against a fixed statement, recovering from lost responses and stopping
the worker. `leanwarp skill --reference` prints the full reference. Neither needs
a key or starts compute.

Sign in yourself with `leanwarp auth login`, or inject `LEANWARP_API_KEY` into the
agent's environment. Never put the key in the instruction.

## Agents with shell access

The CLI is built for agents: every command prints one JSON object, results start
with `success` (`true`, `false`, or `null` while running), and exit codes separate
"did not pass" (`1`) from "could not run" (`2`) and "still running" (`3`).

To install the instructions as a skill your agent discovers automatically, point
its skill installer at the [skill directory](../src/leanwarp_cloud/skills/leanwarp).
Install the whole directory, including `references/`.

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
The server starts even before you sign in, so the agent can read its guides; tools
that call LeanWarp then explain how to sign in.

If the user specifies a workspace budget, pass it to `connect` as
`max_spend_microusd` ($1 = 1,000,000). Reconnecting without that argument keeps
the existing cap; a supplied different cap is rejected. Existing caps can be
changed in the dashboard.

The agent should read the resources `leanwarp://guide` and `leanwarp://reference`
first. The tools are `doctor`, `environments`, `connect`, `check`, `inspect`,
`try_tactics`, `verify_target`, `wait`, `status`, `cancel`, `stop`, `recover`,
`disconnect`, `account` and `resources`. Operation tools wait up to 20 seconds for
their result by default (`wait_seconds`, at most 40).

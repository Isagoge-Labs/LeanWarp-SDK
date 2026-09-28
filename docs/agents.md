# Use LeanWarp with an agent

[Install the CLI and authenticate](getting-started.md) before giving an agent
access. Choose the Lean project it may use. The CLI, Python project
session and MCP adapter share credentials and `.leanwarp/session.json`.

## CLI and skill

For an agent with shell access, ask it to read:

```sh
leanwarp skill
```

This prints the edit–verify workflow, result checks, recovery rules and compute
shutdown guidance. It does not install a skill into your agent's configuration.
The complete reference is available on demand:

```sh
leanwarp skill --reference
```

For automatic skill discovery, use your agent's skill installer with the
[LeanWarp skill directory](../src/leanwarp_cloud/skills/leanwarp). Install the whole
directory, including `references/`, rather than copying only `SKILL.md`.

A starting instruction for the agent:

> Read the LeanWarp skill. Work in this Lean project.
> Verify my proof against its intended statement, reuse the workspace across
> edits, and stop compute when finished.

Manage funding in the website. Provide credentials through the hidden
login prompt or environment, never in the instruction.

## MCP

MCP is included in the standard installation. Add the
[MCP configuration](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#mcp)
to your client, with your project's absolute path. It starts the local stdio
server using `leanwarp --project /absolute/path/to/lean-project mcp`.

Use the executable's absolute path if the client cannot find `leanwarp` on its
`PATH`. The server reads the same saved credentials as the CLI, or environment
variables injected into its process. No API key belongs in tool arguments.

MCP exposes typed tools and their descriptions, plus the bundled instructions at
`leanwarp://guide` and `leanwarp://reference`. Have the agent read those resources
before working. Installing the
LeanWarp skill alongside it supplies the overall workflow; your client must
support skills to load it automatically. See the [MCP reference](../src/leanwarp_cloud/skills/leanwarp/references/usage.md#mcp)
for tool names and parameters.

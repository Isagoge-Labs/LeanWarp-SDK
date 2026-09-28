# LeanWarp SDK

Check and verify Lean proofs from the terminal, Python, or an AI agent. Work in
your existing Lean project; LeanWarp runs verification remotely and reuses
compatible imports across edits.

[Documentation](docs/README.md) · [Getting started](docs/getting-started.md) ·
[Agent setup](docs/agents.md) · [Website](https://isagoge.in)

- **Check files** and inspect proof goals as you edit.
- **Try tactics** before applying them to your source.
- **Verify proofs** against the statement you intended to prove.

The CLI and MCP adapter manage file uploads, workspace reuse and request recovery.
The Python client is available for integrations.

## Install

With [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install --python 3.12 \
  'leanwarp-cloud[mcp] @ git+https://github.com/Isagoge-Labs/LeanWarp-SDK.git'
```

This installs the `leanwarp` command and MCP adapter in an isolated environment.
Supported on Linux, macOS and WSL. Execution requires a supported Lean project,
an API key and funded account credit.

Follow [Getting started](docs/getting-started.md) to authenticate and verify your
first proof, or [set up your agent](docs/agents.md). Python applications use the
[library installation](src/leanwarp_cloud/skills/leanwarp/references/usage.md#python).

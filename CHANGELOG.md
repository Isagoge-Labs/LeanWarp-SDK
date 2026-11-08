# Changelog

Update an installed SDK with `leanwarp update`.

## 0.2.6

- Changed files and the operation that uses them now go to the service in one
  request, so checks and other operations start sooner.
- The HTTP API reference covers `POST /workspaces/{workspace_id}/run`, which
  uploads files and submits an operation together.
- The reference explains how `timeout` works in `check_files` and `verify_batch`:
  it starts once imports have loaded, except for sources using `native_decide`,
  and a short `timeout_seconds` still returns finished results.
- Results now document `engine_` errors on a single batch entry and the
  `elaboration_ms`, `proof_state_ms` and `tactics_ms` timings.
- The getting-started guide explains how to restate Lean options from your
  `lakefile` as `set_option` lines.

## 0.2.5

- The MCP server is listed in the official
  [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.Isagoge-Labs/leanwarp`.
- The documentation is now at
  [isagoge.in/leanwarp/docs](https://isagoge.in/leanwarp/docs). The README and
  the package's links point there.
- The getting-started guide, agent setup, reference and HTTP API pages are
  rewritten to be shorter and simpler.

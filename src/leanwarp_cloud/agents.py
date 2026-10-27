"""Install the LeanWarp skill and MCP server for the agents on this machine.

Installing a Python package cannot run code, so `leanwarp setup` does this step.
Each agent is set up only when its configuration directory exists, unless named.
The skill directory is LeanWarp's own and is replaced; an agent's MCP
configuration only gains a `leanwarp` entry when it has none, and is never
otherwise changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

AgentName = Literal["claude-code", "codex", "cursor", "agents"]
AGENTS: tuple[AgentName, ...] = ("claude-code", "codex", "cursor", "agents")
_SKILL_FILES = ("SKILL.md", "references/usage.md")

Run = Callable[..., subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        list(command), capture_output=True, text=True, check=False, timeout=60, cwd=cwd
    )


@dataclass(frozen=True)
class _Agent:
    name: AgentName
    home: Path
    # Where its skills live, if it reads skills.
    skills: Path | None


def _agents(home: Path, environ: dict[str, str]) -> dict[AgentName, _Agent]:
    claude = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = Path(environ.get("CODEX_HOME") or home / ".codex")
    return {
        "claude-code": _Agent("claude-code", claude, claude / "skills"),
        "codex": _Agent("codex", codex, codex / "skills"),
        "cursor": _Agent("cursor", home / ".cursor", None),
        "agents": _Agent("agents", home / ".agents", home / ".agents" / "skills"),
    }


def _install_skill(skills: Path) -> str:
    target = skills / "leanwarp"
    for name in _SKILL_FILES:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = files("leanwarp_cloud").joinpath(f"skills/leanwarp/{name}")
        path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return str(target)


def _command() -> str:
    """The `leanwarp` this machine runs, so an MCP client finds it without a PATH."""
    return shutil.which("leanwarp") or "leanwarp"


def _claude_mcp(home: Path, run: Run) -> str:
    """Add the server for the user, from the home folder, so an entry in the current
    project's `.mcp.json` is never taken for the user-wide one."""
    try:
        added = run(
            ["claude", "mcp", "add", "--scope", "user", "leanwarp", "--", _command(), "mcp"],
            cwd=home,
        )
    except FileNotFoundError:
        return "claude_cli_not_found"
    except (OSError, subprocess.SubprocessError):
        return "failed"
    if added.returncode == 0:
        return "configured"
    # Claude Code reports an existing user entry as "already exists in user config".
    return "already_configured" if "already exists" in added.stdout + added.stderr else "failed"


def _codex_servers(text: str) -> dict[str, Any] | None:
    """The config's MCP servers as TOML reads them, or None if it cannot be read."""
    try:
        servers = tomllib.loads(text).get("mcp_servers", {})
    except tomllib.TOMLDecodeError:
        return None
    return servers if isinstance(servers, dict) else None


def _codex_mcp(home: Path) -> str:
    config = home / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    # Read the TOML, not its spelling: `[mcp_servers."leanwarp"]` is the same table.
    servers = _codex_servers(text)
    if servers is None:
        return "unreadable_config"
    if "leanwarp" in servers:
        return "already_configured"
    entry = f'[mcp_servers.leanwarp]\ncommand = {json.dumps(_command())}\nargs = ["mcp"]\n'
    if text:
        entry = ("\n" if text.endswith("\n") else "\n\n") + entry
    # Write only a config that still reads, with exactly the entry added.
    updated = _codex_servers(text + entry)
    if updated is None or set(updated) != set(servers) | {"leanwarp"}:
        return "unreadable_config"
    config.write_text(text + entry, encoding="utf-8")
    return "configured"


def _cursor_mcp(home: Path) -> str:
    config = home / "mcp.json"
    try:
        document = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except ValueError:
        return "unreadable_config"
    servers = document.setdefault("mcpServers", {}) if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return "unreadable_config"
    if "leanwarp" in servers:
        return "already_configured"
    servers["leanwarp"] = {
        "command": _command(),
        "args": ["--project", "${workspaceFolder}", "mcp"],
    }
    config.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return "configured"


def setup(
    *,
    agents: Sequence[AgentName] = (),
    mcp: bool = True,
    refresh: bool = False,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
    run: Run = _run,
) -> dict[str, Any]:
    """Set up each named agent, or every agent found. With `refresh`, only replace
    skills that are already installed and leave MCP configuration alone."""
    home = home or Path.home()
    known = _agents(home, dict(os.environ if environ is None else environ))
    results: list[dict[str, Any]] = []
    for name in AGENTS:
        agent = known[name]
        named = name in agents
        if agents and not named:
            continue
        installed = agent.skills is not None and (agent.skills / "leanwarp").is_dir()
        if refresh and not installed:
            continue
        if not refresh and not named and name != "agents" and not agent.home.is_dir():
            results.append({"agent": name, "status": "not_found"})
            continue
        result: dict[str, Any] = {"agent": name, "status": "ok"}
        if agent.skills is not None:
            result["skill"] = _install_skill(agent.skills)
        if mcp and not refresh and name != "agents":
            agent.home.mkdir(parents=True, exist_ok=True)
            if name == "claude-code":
                result["mcp"] = _claude_mcp(home, run)
            elif name == "codex":
                result["mcp"] = _codex_mcp(agent.home)
            else:
                result["mcp"] = _cursor_mcp(agent.home)
        results.append(result)
    return {"agents": results}

"""Install the LeanWarp skill and MCP server for the agents on this machine, or remove them.

Installing a Python package cannot run code, so `leanwarp setup` does this step.
Each agent is set up only when its configuration directory exists, unless named.
The skill directory is LeanWarp's own and is replaced; an agent's MCP
configuration only gains a `leanwarp` entry when it has none, and is never
otherwise changed. Every file is replaced in one step, keeping its permissions,
and a symlinked file is written through its link.

Setup is a recorded transaction. Under an exclusive lock on its record,
`~/.config/leanwarp/setup.json`, each change is first written to the record as an
intent, durably: the path, the target it resolves to, and exactly what will be
written there. Only then is the change made. Removal, under the same lock, undoes
an item only if the path still resolves to that target and what is there is
exactly what the record says setup wrote; anything else stays and is reported.
So an intent that was never carried out is harmless, and an interrupted setup or
removal converges when run again.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from . import _toml
from .errors import SessionError

AgentName = Literal["claude-code", "codex", "cursor", "agents"]
AGENTS: tuple[AgentName, ...] = ("claude-code", "codex", "cursor", "agents")
_SKILL_FILES = ("SKILL.md", "references/usage.md")
_CODEX_ARGS = ["mcp"]
_CURSOR_ARGS = ["--project", "${workspaceFolder}", "mcp"]
# What an item's undo says; only these two mean setup's change is gone.
_UNDONE = frozenset({"removed", "absent"})
# Outcomes after which the record no longer needs the item.
_SETTLED = _UNDONE | {"preexisting"}

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


def _agents(home: Path, environ: Mapping[str, str]) -> dict[AgentName, _Agent]:
    claude = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = Path(environ.get("CODEX_HOME") or home / ".codex")
    return {
        "claude-code": _Agent("claude-code", claude, claude / "skills"),
        "codex": _Agent("codex", codex, codex / "skills"),
        "cursor": _Agent("cursor", home / ".cursor", None),
        "agents": _Agent("agents", home / ".agents", home / ".agents" / "skills"),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _missing(directory: Path) -> list[Path]:
    """The directory and each missing parent, outermost first."""
    missing: list[Path] = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    return missing[::-1]


def _create_directory(path: Path, mode: int = 0o777) -> None:
    """Create one directory durably: its entry in the parent survives a power loss."""
    path.mkdir(mode=mode, exist_ok=True)
    _fsync_directory(path.parent)


def _write(path: Path, text: str, *, mode: int, through_link: bool = True) -> None:
    """Replace a file in one step and durably, so a reader sees the old file or the new.

    A symlinked file is written through its link, which stays, unless
    ``through_link`` is false: then the entry at ``path`` itself is replaced, so
    nothing a link there points to is ever written. An existing file keeps its
    permissions; a new one gets ``mode``.
    """
    target = path.resolve() if through_link else path.parent.resolve() / path.name
    if target.is_file() and not target.is_symlink():
        mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _Ledger:
    """The record of what setup did, by path; each intent is saved before it is acted on."""

    def __init__(self, path: Path, items: dict[str, dict[str, Any]]) -> None:
        self.path, self.items = path, items

    def intend(self, agent: AgentName, kind: str, path: Path, **written: Any) -> None:
        target = path.resolve()
        parent = os.stat(target.parent)
        self.items[str(path)] = {
            "agent": agent,
            "kind": kind,
            "path": str(path),
            "target": str(target),
            # The directory removal must find there: renaming or replacing it, or an
            # ancestor, makes removal leave the item instead of acting elsewhere.
            "parent": [parent.st_dev, parent.st_ino],
            **written,
        }
        self.save()

    def forget(self, path: Path) -> None:
        if self.items.pop(str(path), None) is not None:
            self.save()

    def save(self) -> None:
        if self.items:
            _write(self.path, json.dumps({"items": self.items}, indent=2) + "\n", mode=0o600)
        else:
            self.path.unlink(missing_ok=True)


def _record_path(home: Path, environ: Mapping[str, str]) -> Path:
    return Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "leanwarp" / "setup.json"


@contextmanager
def _ledger(path: Path) -> Iterator[_Ledger]:
    """The record, read and written under an exclusive lock on its directory."""
    # Durably, before any change is recorded, so the record outlives what it records.
    for directory in _missing(path.parent):
        _create_directory(directory, 0o700)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = '{"items": {}}'
        try:
            items = json.loads(raw)["items"]
            if not isinstance(items, dict):
                raise TypeError("items")
        except (ValueError, KeyError, TypeError) as error:
            raise SessionError(
                f"{path} is damaged; keep it, since it records what setup installed"
            ) from error
        yield _Ledger(path, items)
    finally:
        os.close(descriptor)


def _make_directories(directory: Path, agent: AgentName, ledger: _Ledger) -> None:
    """Create a directory and any missing parents, recording each before creating it."""
    for path in _missing(directory):
        ledger.intend(agent, "directory", path)
        _create_directory(path)


def _install_skill(
    agent: AgentName, skills: Path, ledger: _Ledger
) -> tuple[str, list[dict[str, str]]]:
    """Write the skill's files, and say which existing ones were left as they are.

    Setup writes a file only where none exists, or over one it created and that
    is still exactly as it wrote it. Anything else at the path, such as the user's
    own file or a symlink, is never written through: it is left, reported, and
    recorded only if it already holds this release's text, as not setup's own.
    """
    target = skills / "leanwarp"
    left: list[dict[str, str]] = []
    for name in _SKILL_FILES:
        path = target / name
        _make_directories(path.parent, agent, ledger)
        text = files("leanwarp_cloud").joinpath(f"skills/leanwarp/{name}").read_text("utf-8")
        digest = _sha256(text.encode("utf-8"))
        if path.is_symlink() or path.exists():
            state = _existing_state(path, ledger.items.get(str(path)), digest)
            if state == "current_not_setups":
                ledger.intend(agent, "file", path, sha256=digest, created=False)
            if state != "setups_unchanged":
                if state not in {"current", "current_not_setups"}:
                    left.append({"path": str(path), "state": state})
                continue
        ledger.intend(agent, "file", path, sha256=digest, created=True)
        # Setup's own file, or nothing, was here a moment ago: replace the entry itself.
        _write(path, text, mode=0o644, through_link=False)
    return str(target), left


def _existing_state(path: Path, owned: Mapping[str, Any] | None, digest: str) -> str:
    """What is at a skill path before setup writes: whether setup may replace it."""
    if path.is_symlink():
        return "symlink"
    if not path.is_file():
        return "not_a_file"
    current = _sha256(path.read_bytes())
    if (
        owned is None
        or owned["kind"] != "file"
        or owned.get("created") is not True
        or owned["target"] != str(path.resolve())
    ):
        return "current_not_setups" if current == digest else "user_file"
    if current == digest:
        return "current"
    return "setups_unchanged" if current == owned["sha256"] else "modified"


def _command() -> str:
    """The `leanwarp` this machine runs, so an MCP client finds it without a PATH."""
    return shutil.which("leanwarp") or "leanwarp"


def _claude_config(home: Path, environ: Mapping[str, str]) -> Path:
    directory = environ.get("CLAUDE_CONFIG_DIR")
    return (Path(directory) if directory else home) / ".claude.json"


def _claude_entry(config: Path) -> Any:
    """The user-wide `leanwarp` entry Claude Code saved, or None."""
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    return servers.get("leanwarp") if isinstance(servers, dict) else None


def _claude_mcp(home: Path, environ: Mapping[str, str], run: Run, ledger: _Ledger) -> str:
    """Add the server for the user, from the home folder, so an entry in the current
    project's `.mcp.json` is never taken for the user-wide one."""
    config = _claude_config(home, environ)
    try:
        if _claude_entry(config) is not None:
            return "already_configured"
    except ValueError:
        return "unreadable_config"
    command = _command()
    # Exactly what `claude mcp add` saves for a stdio server.
    entry = {"type": "stdio", "command": command, "args": _CODEX_ARGS, "env": {}}
    ledger.intend("claude-code", "claude", config, entry=entry)
    try:
        added = run(
            ["claude", "mcp", "add", "--scope", "user", "leanwarp", "--", command, "mcp"],
            cwd=home,
        )
    except FileNotFoundError:
        ledger.forget(config)
        return "claude_cli_not_found"
    except (OSError, subprocess.SubprocessError):
        ledger.forget(config)
        return "failed"
    if added.returncode == 0:
        return "configured"
    ledger.forget(config)
    # Claude Code reports an existing user entry as "already exists in user config".
    return "already_configured" if "already exists" in added.stdout + added.stderr else "failed"


def _codex_document(text: str) -> dict[str, Any] | None:
    try:
        return _toml.loads(text)
    except _toml.TOMLDecodeError:
        return None


def _codex_entry(command: str) -> str:
    """The exact table setup appends to a Codex config."""
    arguments = json.dumps(_CODEX_ARGS)
    return f"[mcp_servers.leanwarp]\ncommand = {json.dumps(command)}\nargs = {arguments}\n"


def _without_leanwarp(document: dict[str, Any]) -> dict[str, Any]:
    """A Codex config as it reads with no `leanwarp` server and no empty server table."""
    document = copy.deepcopy(document)
    servers = document.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop("leanwarp", None)
        if not servers:
            del document["mcp_servers"]
    return document


def _codex_mcp(home: Path, ledger: _Ledger) -> str:
    config = home / "config.toml"
    created = not config.exists()
    text = "" if created else config.read_text(encoding="utf-8")
    # Read the TOML, not its spelling: `[mcp_servers."leanwarp"]` is the same table.
    document = _codex_document(text)
    servers = document.get("mcp_servers", {}) if document is not None else None
    if document is None or not isinstance(servers, dict):
        return "unreadable_config"
    if "leanwarp" in servers:
        return "already_configured"
    block = _codex_entry(_command())
    separator = ("\n" if text.endswith("\n") else "\n\n") if text else ""
    # Write only a config that still reads, with exactly the entry added.
    updated = _codex_document(text + separator + block)
    if updated is None or _without_leanwarp(updated) != _without_leanwarp(document):
        return "unreadable_config"
    ledger.intend("codex", "codex", config, block=block, separator=separator, created=created)
    _write(config, text + separator + block, mode=0o600)
    return "configured"


def _cursor_document(config: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _cursor_mcp(home: Path, ledger: _Ledger) -> str:
    config = home / "mcp.json"
    created = not config.exists()
    document = _cursor_document(config)
    if document is None or not isinstance(document.get("mcpServers", {}), dict):
        return "unreadable_config"
    servers_created = "mcpServers" not in document
    servers = document.setdefault("mcpServers", {})
    if "leanwarp" in servers:
        return "already_configured"
    entry = {"command": _command(), "args": _CURSOR_ARGS}
    ledger.intend(
        "cursor", "cursor", config, entry=entry, created=created, servers_created=servers_created
    )
    servers["leanwarp"] = entry
    _write(config, json.dumps(document, indent=2) + "\n", mode=0o600)
    return "configured"


# What removal does relative to an open directory; without all of them it refuses.
_PINNED_OPERATIONS = (os.open, os.stat, os.unlink, os.rmdir, os.rename)


class _NotAFile(Exception):
    """Something other than a regular file is where setup wrote one."""


class _Pinned:
    """A recorded target, reached only through its parent directory held open.

    Every read and change is relative to that descriptor and never follows a final
    symlink, so a change to the path's ancestors after validation cannot redirect it.
    """

    def __init__(self, directory: int, name: str) -> None:
        self.directory, self.name = directory, name

    def read(self) -> bytes | None:
        """The file's bytes, or None if there is none."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(self.name, flags, dir_fd=self.directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise _NotAFile from error
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _NotAFile
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            return stream.read()

    def unlink(self) -> None:
        os.unlink(self.name, dir_fd=self.directory)
        os.fsync(self.directory)

    def rmdir(self) -> None:
        os.rmdir(self.name, dir_fd=self.directory)
        os.fsync(self.directory)

    def replace(self, text: str) -> None:
        """Replace the file in one step, keeping its permissions."""
        mode = stat.S_IMODE(
            os.stat(self.name, dir_fd=self.directory, follow_symlinks=False).st_mode
        )
        temporary = f".{self.name}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.directory)
            raise
        os.fsync(self.directory)


def _undo(item: Mapping[str, Any], home: Path, run: Run) -> str:
    """Undo one recorded change if it is still exactly as recorded; say what happened.

    The path must still resolve to the recorded target, and the directory then
    opened must be the very one recorded (device and inode); every step after
    that goes through that open directory. A platform that cannot do so refuses.
    """
    if not all(operation in os.supports_dir_fd for operation in _PINNED_OPERATIONS):
        return "unsupported"
    path, target = Path(item["path"]), Path(item["target"])
    try:
        if path.resolve() != target:
            # The path now leads somewhere else, so what is there is not setup's.
            return "moved"
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            opened = os.fstat(directory)
            if [opened.st_dev, opened.st_ino] != item["parent"]:
                return "moved"
            return _UNDO[item["kind"]](item, _Pinned(directory, target.name), home, run)
        finally:
            os.close(directory)
    except _NotAFile:
        return "modified"
    except (OSError, UnicodeDecodeError, ValueError, KeyError):
        return "unreadable"


def _undo_directory(item: Mapping[str, Any], pinned: _Pinned, *_: Any) -> str:
    try:
        pinned.rmdir()
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return "kept"
        raise
    return "removed"


def _undo_file(item: Mapping[str, Any], pinned: _Pinned, *_: Any) -> str:
    if not item["created"]:
        # It was there before setup ran; it is the user's to keep.
        return "preexisting"
    data = pinned.read()
    if data is None:
        return "absent"
    if _sha256(data) != item["sha256"]:
        return "modified"
    pinned.unlink()
    return "removed"


def _rewrite(item: Mapping[str, Any], pinned: _Pinned, text: str, empty: bool) -> None:
    """Write a config that lost setup's entry; delete it only if setup created it.

    A symlink is never removed: the link and the file it names are the user's.
    """
    if empty and item["created"] and item["path"] == item["target"]:
        pinned.unlink()
    else:
        pinned.replace(text)


def _undo_codex(item: Mapping[str, Any], pinned: _Pinned, *_: Any) -> str:
    """Cut out the exact table setup appended, only while it reads exactly as appended."""
    data = pinned.read()
    if data is None:
        return "absent"
    text = data.decode("utf-8")
    document = _codex_document(text)
    if document is None:
        return "unreadable_config"
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict) or "leanwarp" not in servers:
        return "absent"
    expected = _toml.loads(item["block"])["mcp_servers"]["leanwarp"]
    added = item["separator"] + item["block"]
    cut = added if added in text else item["block"]
    if servers["leanwarp"] != expected or cut not in text:
        return "modified"
    start = text.rindex(cut)
    updated = text[:start] + text[start + len(cut) :]
    remaining = _codex_document(updated)
    if remaining is None or _without_leanwarp(remaining) != _without_leanwarp(document):
        return "modified"
    _rewrite(item, pinned, updated, not updated.strip())
    return "removed"


def _json_document(pinned: _Pinned) -> dict[str, Any] | None:
    """The file's JSON object; raises ValueError for anything that is not one."""
    data = pinned.read()
    if data is None:
        return None
    document = json.loads(data)
    if not isinstance(document, dict):
        raise ValueError("not a JSON object")
    return document


def _undo_cursor(item: Mapping[str, Any], pinned: _Pinned, *_: Any) -> str:
    try:
        document = _json_document(pinned)
    except ValueError:
        return "unreadable_config"
    if document is None:
        return "absent"
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return "unreadable_config"
    if "leanwarp" not in servers:
        return "absent"
    if servers["leanwarp"] != item["entry"]:
        return "modified"
    del servers["leanwarp"]
    if not servers and item["servers_created"]:
        del document["mcpServers"]
    # A retained file stays valid JSON, even when it is now an empty object.
    _rewrite(item, pinned, json.dumps(document, indent=2) + "\n", not document)
    return "removed"


def _undo_claude(item: Mapping[str, Any], pinned: _Pinned, home: Path, run: Run) -> str:
    try:
        document = _json_document(pinned)
    except ValueError:
        return "unreadable_config"
    servers = document.get("mcpServers") if document is not None else None
    entry = servers.get("leanwarp") if isinstance(servers, dict) else None
    if entry is None:
        return "absent"
    if entry != item["entry"]:
        return "modified"
    # Claude Code edits its own file; only the check above is setup's.
    try:
        removed = run(["claude", "mcp", "remove", "--scope", "user", "leanwarp"], cwd=home)
    except FileNotFoundError:
        return "claude_cli_not_found"
    except subprocess.SubprocessError:
        return "failed"
    return "removed" if removed.returncode == 0 else "failed"


_UNDO: dict[str, Callable[..., str]] = {
    "directory": _undo_directory,
    "file": _undo_file,
    "codex": _undo_codex,
    "cursor": _undo_cursor,
    "claude": _undo_claude,
}


def setup(
    *,
    agents: Sequence[AgentName] = (),
    mcp: bool = True,
    refresh: bool = False,
    remove: bool = False,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    run: Run = _run,
) -> dict[str, Any]:
    """Set up each named agent, or every agent found. With `refresh`, only replace
    skills that are already installed and leave MCP configuration alone. With
    `remove`, undo what the record says setup did, for each named agent or all."""
    home = home or Path.home()
    environ = dict(os.environ if environ is None else environ)
    record = _record_path(home, environ)
    if remove and not record.exists():
        return {
            "agents": [],
            "status": "no_record",
            "message": f"{record} does not exist, so nothing setup installed is known; "
            "nothing was removed",
        }
    known = _agents(home, environ)
    selected = [known[name] for name in AGENTS if not agents or name in agents]
    with _ledger(record) as ledger:
        if remove:
            return {"agents": _remove(selected, ledger, home, run)}
        results = [
            _install(agent, agents, mcp, refresh, ledger, home, environ, run) for agent in selected
        ]
    return {"agents": [result for result in results if result is not None]}


def _install(
    agent: _Agent,
    named: Sequence[AgentName],
    mcp: bool,
    refresh: bool,
    ledger: _Ledger,
    home: Path,
    environ: Mapping[str, str],
    run: Run,
) -> dict[str, Any] | None:
    installed = agent.skills is not None and (agent.skills / "leanwarp").is_dir()
    if refresh and not installed:
        return None
    if not refresh and agent.name not in named and not agent.home.is_dir():
        return {"agent": agent.name, "status": "not_found"}
    result: dict[str, Any] = {"agent": agent.name, "status": "ok"}
    if agent.skills is not None:
        result["skill"], left = _install_skill(agent.name, agent.skills, ledger)
        if left:
            result["skill_files_left"] = left
    if mcp and not refresh and agent.name != "agents":
        _make_directories(agent.home, agent.name, ledger)
        if agent.name == "claude-code":
            result["mcp"] = _claude_mcp(home, environ, run, ledger)
        elif agent.name == "codex":
            result["mcp"] = _codex_mcp(agent.home, ledger)
        else:
            result["mcp"] = _cursor_mcp(agent.home, ledger)
    return result


def _remove(
    selected: Sequence[_Agent], ledger: _Ledger, home: Path, run: Run
) -> list[dict[str, Any]]:
    """Undo the selected agents' recorded items: files and entries, then folders deepest first."""
    names = {agent.name for agent in selected}
    items = sorted(
        (item for item in ledger.items.values() if item["agent"] in names),
        key=lambda item: (item["kind"] == "directory", -len(Path(item["path"]).parts)),
    )
    outcomes: dict[str, dict[str, list[str]]] = {name: {"skill": [], "mcp": []} for name in names}
    for item in items:
        outcome = _undo(item, home, run)
        if outcome in _SETTLED:
            ledger.forget(Path(item["path"]))
        if item["kind"] != "directory":
            outcomes[item["agent"]]["skill" if item["kind"] == "file" else "mcp"].append(outcome)
    results = []
    for agent in selected:
        result: dict[str, Any] = {"agent": agent.name}
        if agent.skills is not None:
            result["skill"] = _summary(outcomes[agent.name]["skill"], "not_installed")
        if agent.name != "agents":
            result["mcp"] = _summary(outcomes[agent.name]["mcp"], "not_recorded")
        removed = "removed" in {result.get("skill"), result.get("mcp")}
        result["status"] = "removed" if removed else "nothing_to_remove"
        results.append(result)
    return results


def _summary(outcomes: Sequence[str], empty: str) -> str:
    """One word for several items: the first one left in place, else removed."""
    left = [outcome for outcome in outcomes if outcome not in _UNDONE]
    if left:
        return left[0]
    return "removed" if "removed" in outcomes else empty

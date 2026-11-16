"""Setup is atomic and reversible, and feedback can be turned off."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from leanwarp_cloud import agents, cli
from leanwarp_cloud.agents import setup

CODEX_BEFORE = 'model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n'
CURSOR_BEFORE = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}


class Claude:
    """Claude Code's `mcp add` and `mcp remove`, saving user entries as Claude Code does."""

    def __init__(self, home: Path) -> None:
        self.config = home / ".claude.json"
        self.calls: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], cwd: object = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        document = json.loads(self.config.read_text()) if self.config.exists() else {}
        servers = document.setdefault("mcpServers", {})
        if command[2] == "add":
            executable, *arguments = command[command.index("--") + 1 :]
            servers["leanwarp"] = {
                "type": "stdio",
                "command": executable,
                "args": arguments,
                "env": {},
            }
        else:
            servers.pop("leanwarp")
        self.config.write_text(json.dumps(document))
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def machine(tmp_path: Path):
    for folder in (".claude", ".codex", ".cursor", ".agents"):
        (tmp_path / folder).mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(CODEX_BEFORE)
    (tmp_path / ".cursor" / "mcp.json").write_text(json.dumps(CURSOR_BEFORE))
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {}, "theme": "light"}))
    return tmp_path, Claude(tmp_path)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file and folder, except LeanWarp's own configuration folder for its record."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else b"<dir>"
        for path in sorted(root.rglob("*"))
        if path.relative_to(root).parts[0] != ".config"
    }


def test_remove_undoes_exactly_what_setup_added(machine):
    home, claude = machine
    before = snapshot(home)
    added = setup(home=home, environ={}, run=claude)
    assert {entry["mcp"] for entry in added["agents"] if "mcp" in entry} == {"configured"}
    assert snapshot(home) != before
    removed = setup(remove=True, home=home, environ={}, run=claude)
    statuses = {entry["agent"]: entry for entry in removed["agents"]}
    assert all(entry["status"] == "removed" for entry in statuses.values())
    assert statuses["claude-code"]["mcp"] == statuses["codex"]["mcp"] == "removed"
    after = snapshot(home)
    # Cursor's file is JSON that setup itself reformats; its content is what it was.
    assert json.loads(after.pop(".cursor/mcp.json")) == CURSOR_BEFORE
    assert json.loads(after.pop(".claude.json")) == {"mcpServers": {}, "theme": "light"}
    before.pop(".cursor/mcp.json")
    before.pop(".claude.json")
    assert after == before
    assert claude.calls[-1][:5] == ["claude", "mcp", "remove", "--scope", "user"]


def test_remove_deletes_configs_setup_created(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".cursor").mkdir()
    setup(agents=["codex", "cursor"], home=tmp_path, environ={})
    assert (tmp_path / ".codex/config.toml").exists() and (tmp_path / ".cursor/mcp.json").exists()
    setup(agents=["codex", "cursor"], remove=True, home=tmp_path, environ={})
    assert sorted(snapshot(tmp_path)) == [".codex", ".cursor"]


def test_remove_keeps_entries_the_user_changed_and_files_they_added(machine):
    home, claude = machine
    setup(home=home, environ={}, run=claude)
    codex = home / ".codex/config.toml"
    edited = codex.read_text().replace('args = ["mcp"]', 'args = ["--project", "/p", "mcp"]')
    codex.write_text(edited)
    cursor = home / ".cursor/mcp.json"
    document = json.loads(cursor.read_text())
    document["mcpServers"]["leanwarp"]["env"] = {"LEANWARP_FEEDBACK": "off"}
    cursor.write_text(json.dumps(document))
    note = home / ".agents/skills/leanwarp/NOTES.md"
    note.write_text("mine")
    removed = {
        entry["agent"]: entry for entry in setup(remove=True, home=home, environ={})["agents"]
    }
    assert removed["codex"]["mcp"] == removed["cursor"]["mcp"] == "modified"
    assert codex.read_text() == edited and json.loads(cursor.read_text()) == document
    assert note.read_text() == "mine"
    assert not (home / ".agents/skills/leanwarp/SKILL.md").exists()


@pytest.mark.parametrize(
    ("codex", "cursor"),
    [
        ('[mcp_servers.leanwarp]\ncommand = "leanwarp"\n', {"command": "leanwarp"}),
        ('mcp_servers.leanwarp = "leanwarp"\n', "leanwarp"),
    ],
    ids=["no-arguments", "not-a-table"],
)
def test_remove_keeps_hand_written_entries_of_any_shape(tmp_path, codex, cursor):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".codex/config.toml").write_text(codex)
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"leanwarp": cursor}}))
    before = snapshot(tmp_path)
    report = setup(agents=["codex", "cursor"], remove=True, home=tmp_path, environ={})
    # Setup never installed them, so it has no record and touches nothing.
    assert report["status"] == "no_record" and report["agents"] == []
    assert snapshot(tmp_path) == before


def test_remove_of_nothing_changes_nothing(machine):
    home, claude = machine
    before = snapshot(home)
    report = setup(remove=True, home=home, environ={}, run=claude)
    assert report["status"] == "no_record" and "nothing was removed" in report["message"]
    assert snapshot(home) == before and claude.calls == []


def test_config_writes_keep_permissions_and_links(machine):
    home, claude = machine
    real = home / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text(CODEX_BEFORE)
    real.chmod(0o640)
    link = home / ".codex/config.toml"
    link.unlink()
    link.symlink_to(real)
    setup(agents=["codex"], home=home, environ={}, run=claude)
    assert link.is_symlink() and "[mcp_servers.leanwarp]" in real.read_text()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


def test_an_interrupted_write_leaves_the_original_whole(machine, monkeypatch):
    home, _ = machine

    def crash(source: str, target: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(agents.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        agents._codex_mcp(home / ".codex", agents._Ledger(home / "record.json", {}))
    assert (home / ".codex/config.toml").read_text() == CODEX_BEFORE
    assert [path.name for path in (home / ".codex").iterdir()] == ["config.toml"]


def test_new_configs_are_private(tmp_path):
    (tmp_path / ".cursor").mkdir()
    setup(agents=["cursor"], home=tmp_path, environ={})
    assert stat.S_IMODE((tmp_path / ".cursor/mcp.json").stat().st_mode) == 0o600


def test_cli_setup_remove_takes_only_agents(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["setup", "--remove", "--refresh"])
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


def test_cli_setup_remove_reports_and_says_nothing_about_signing_in(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "setup", lambda **options: calls.append(options) or {"agents": []})
    assert cli.main(["setup", "--remove", "--agent", "codex"]) == 0
    assert calls[0]["remove"] is True and calls[0]["agents"] == ["codex"]
    assert "next" not in json.loads(capsys.readouterr().out)


# LEANWARP_FEEDBACK=off


def test_feedback_off_disables_the_command_without_sending(monkeypatch, capsys):
    monkeypatch.setenv("LEANWARP_FEEDBACK", "off")
    monkeypatch.setattr(cli, "load_client", lambda **_: pytest.fail("nothing may be sent"))
    code = cli.main(["feedback", "--kind", "bug", "--summary", "something went wrong"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "recorded": False,
        "reason": "feedback_disabled",
    }


@pytest.mark.parametrize(("value", "offered"), [("off", False), ("OFF", False), ("on", True)])
def test_feedback_off_removes_the_tool_and_its_instruction(tmp_path, monkeypatch, value, offered):
    from leanwarp_cloud.mcp_server import create_server

    monkeypatch.setenv("LEANWARP_FEEDBACK", value)
    server = create_server(lambda: None, str(tmp_path))  # type: ignore[arg-type, return-value]
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert ("feedback" in names) is offered
    assert ("feedback" in (server.instructions or "")) is offered


def test_the_skill_says_how_to_turn_feedback_off():
    skill = (Path(cli.__file__).parent / "skills/leanwarp/SKILL.md").read_text()
    assert "LEANWARP_FEEDBACK=off" in skill


def test_setup_keeps_permissions_of_existing_skill_files(machine):
    home, claude = machine
    setup(agents=["agents"], home=home, environ={}, run=claude)
    skill = home / ".agents/skills/leanwarp/SKILL.md"
    # As an earlier release wrote it, and recorded it.
    skill.write_text("an earlier release's guide")
    record = home / ".config/leanwarp/setup.json"
    document = json.loads(record.read_text())
    document["items"][str(skill)]["sha256"] = hashlib.sha256(skill.read_bytes()).hexdigest()
    record.write_text(json.dumps(document))
    skill.chmod(0o600)
    setup(agents=["agents"], refresh=True, home=home, environ={}, run=claude)
    assert skill.read_text() != "an earlier release's guide"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o600


def test_remove_keeps_a_skill_file_the_user_edited(machine):
    home, claude = machine
    setup(agents=["agents"], home=home, environ={}, run=claude)
    skill = home / ".agents/skills/leanwarp/SKILL.md"
    skill.write_text(skill.read_text() + "\nMy own notes.\n")
    report = setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    assert report["agents"][0]["skill"] == "modified"
    assert skill.read_text().endswith("My own notes.\n")
    assert not (home / ".agents/skills/leanwarp/references/usage.md").exists()


def test_remove_keeps_entries_setup_found_already_there(machine):
    home, claude = machine
    command = agents._command()
    codex = home / ".codex/config.toml"
    codex.write_text(CODEX_BEFORE + "\n" + agents._codex_entry(command))
    cursor = home / ".cursor/mcp.json"
    cursor.write_text(
        json.dumps({"mcpServers": {"leanwarp": {"command": command, "args": agents._CURSOR_ARGS}}})
    )
    before = {path: path.read_text() for path in (codex, cursor)}
    added = {e["agent"]: e for e in setup(home=home, environ={}, run=claude)["agents"]}
    assert added["codex"]["mcp"] == added["cursor"]["mcp"] == "already_configured"
    removed = {
        e["agent"]: e for e in setup(remove=True, home=home, environ={}, run=claude)["agents"]
    }
    assert removed["codex"]["mcp"] == removed["cursor"]["mcp"] == "not_recorded"
    assert {path: path.read_text() for path in before} == before


@pytest.mark.parametrize("agent", ["codex", "cursor"])
def test_remove_edits_a_symlinked_config_through_its_link(tmp_path, agent):
    name = {"codex": "config.toml", "cursor": "mcp.json"}[agent]
    original = {"codex": "", "cursor": "{}\n"}[agent]
    real = tmp_path / "dotfiles" / name
    real.parent.mkdir()
    real.write_text(original)
    folder = tmp_path / {"codex": ".codex", "cursor": ".cursor"}[agent]
    folder.mkdir()
    (folder / name).symlink_to(real)
    setup(agents=[agent], home=tmp_path, environ={})
    assert "leanwarp" in real.read_text()
    report = setup(agents=[agent], remove=True, home=tmp_path, environ={})
    assert report["agents"][0]["mcp"] == "removed"
    assert (folder / name).is_symlink() and (folder / name).resolve() == real
    assert real.read_text() == original


def test_remove_keeps_a_claude_entry_the_user_added_settings_to(machine):
    home, claude = machine
    setup(agents=["claude-code"], home=home, environ={}, run=claude)
    config = home / ".claude.json"
    document = json.loads(config.read_text())
    document["mcpServers"]["leanwarp"]["env"] = {"LEANWARP_FEEDBACK": "off"}
    config.write_text(json.dumps(document))
    report = setup(agents=["claude-code"], remove=True, home=home, environ={}, run=claude)
    assert report["agents"][0]["mcp"] == "modified"
    assert json.loads(config.read_text()) == document
    assert [call[2] for call in claude.calls] == ["add"]


def test_remove_keeps_a_codex_entry_the_user_added_settings_to(machine):
    home, claude = machine
    setup(agents=["codex"], home=home, environ={}, run=claude)
    codex = home / ".codex/config.toml"
    edited = codex.read_text() + 'env = { LEANWARP_FEEDBACK = "off" }\n'
    codex.write_text(edited)
    report = setup(agents=["codex"], remove=True, home=home, environ={}, run=claude)
    assert report["agents"][0]["mcp"] == "modified"
    assert codex.read_text() == edited


@pytest.mark.parametrize("linked", [False, True], ids=["file", "symlink"])
def test_a_retained_cursor_config_stays_valid_json(tmp_path, linked):
    (tmp_path / ".cursor").mkdir()
    real = tmp_path / ("dotfiles/mcp.json" if linked else ".cursor/mcp.json")
    real.parent.mkdir(exist_ok=True)
    real.write_text("{}")
    if linked:
        (tmp_path / ".cursor/mcp.json").symlink_to(real)
    setup(agents=["cursor"], home=tmp_path, environ={})
    setup(agents=["cursor"], remove=True, home=tmp_path, environ={})
    assert json.loads(real.read_text()) == {}


def test_remove_leaves_skill_files_whose_folder_now_leads_elsewhere(machine):
    home, claude = machine
    setup(agents=["agents"], home=home, environ={}, run=claude)
    installed = home / ".agents/skills/leanwarp"
    elsewhere = home / "elsewhere"
    installed.rename(elsewhere)
    installed.symlink_to(elsewhere)
    before = snapshot(elsewhere)
    report = setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    assert report["agents"][0]["skill"] == "moved"
    assert snapshot(elsewhere) == before and installed.is_symlink()


def test_concurrent_setups_both_record_what_they_installed(tmp_path, monkeypatch):
    import threading
    import time

    (tmp_path / ".codex").mkdir()
    (tmp_path / ".cursor").mkdir()
    write = agents._write
    cursor_started = threading.Event()

    def slow(path: Path, text: str, **how: Any) -> None:
        # Hold the Codex setup inside its config write until the Cursor one has begun.
        if path.name == "config.toml":
            cursor_started.wait(2)
            time.sleep(0.2)
        write(path, text, **how)

    monkeypatch.setattr(agents, "_write", slow)
    codex = threading.Thread(
        target=setup, kwargs={"agents": ["codex"], "home": tmp_path, "environ": {}}
    )
    codex.start()
    time.sleep(0.1)
    cursor_started.set()
    setup(agents=["cursor"], home=tmp_path, environ={})
    codex.join()
    monkeypatch.setattr(agents, "_write", write)
    report = setup(agents=["codex", "cursor"], remove=True, home=tmp_path, environ={})
    assert {entry["agent"]: entry["mcp"] for entry in report["agents"]} == {
        "codex": "removed",
        "cursor": "removed",
    }


class Killed(BaseException):
    """A process killed between two steps."""


def _failing_write(monkeypatch, name: str, *, after: bool):
    write = agents._write

    def failing(path: Path, text: str, **how: Any) -> None:
        if path.name == name and not after:
            raise Killed
        write(path, text, **how)
        if path.name == name and after:
            raise Killed

    monkeypatch.setattr(agents, "_write", failing)
    return write


def test_a_record_that_cannot_be_written_changes_nothing(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(CODEX_BEFORE)
    _failing_write(monkeypatch, "setup.json", after=False)
    with pytest.raises(Killed):
        setup(agents=["codex"], home=tmp_path, environ={})
    assert (tmp_path / ".codex/config.toml").read_text() == CODEX_BEFORE
    assert not (tmp_path / ".codex/skills").exists()


def test_a_change_interrupted_after_it_is_made_can_still_be_undone(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    config = tmp_path / ".codex/config.toml"
    config.write_text(CODEX_BEFORE)
    write = _failing_write(monkeypatch, "config.toml", after=True)
    with pytest.raises(Killed):
        setup(agents=["codex"], home=tmp_path, environ={})
    assert "[mcp_servers.leanwarp]" in config.read_text()
    monkeypatch.setattr(agents, "_write", write)
    # Running setup again finds the entry already there; the record still owns it.
    setup(agents=["codex"], home=tmp_path, environ={})
    report = setup(agents=["codex"], remove=True, home=tmp_path, environ={})
    assert report["agents"][0]["mcp"] == "removed" and config.read_text() == CODEX_BEFORE


def test_a_change_interrupted_before_it_is_made_is_harmless(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    config = tmp_path / ".codex/config.toml"
    config.write_text(CODEX_BEFORE)
    write = _failing_write(monkeypatch, "config.toml", after=False)
    with pytest.raises(Killed):
        setup(agents=["codex"], home=tmp_path, environ={})
    monkeypatch.setattr(agents, "_write", write)
    assert config.read_text() == CODEX_BEFORE
    report = setup(agents=["codex"], remove=True, home=tmp_path, environ={})
    assert config.read_text() == CODEX_BEFORE and report["agents"][0]["mcp"] != "removed"
    # A partial removal then setup converge.
    setup(agents=["codex"], home=tmp_path, environ={})
    assert (
        setup(agents=["codex"], remove=True, home=tmp_path, environ={})["agents"][0]["mcp"]
        == "removed"
    )
    assert config.read_text() == CODEX_BEFORE


def test_every_directory_setup_creates_is_durable_before_anything_is_written(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    events: list[tuple[str, Path]] = []
    fsync, write = agents._fsync_directory, agents._write

    def synced(path: Path) -> None:
        events.append(("fsync", Path(path)))
        fsync(path)

    def written(path: Path, text: str, **how: Any) -> None:
        events.append(("write", Path(path)))
        write(path, text, **how)

    monkeypatch.setattr(agents, "_fsync_directory", synced)
    monkeypatch.setattr(agents, "_write", written)
    setup(agents=["codex"], home=tmp_path, environ={})
    first_write = next(index for index, (kind, _) in enumerate(events) if kind == "write")
    synced_before = {path for kind, path in events[:first_write] if kind == "fsync"}
    # The record's folders were new: each one's parent was synced before the first write.
    assert {tmp_path, tmp_path / ".config"} <= synced_before


def test_setup_never_writes_through_a_skill_path_that_was_already_a_symlink(machine):
    home, claude = machine
    mine = home / "notes" / "my-skill.md"
    mine.parent.mkdir()
    mine.write_text("my own skill")
    skill = home / ".agents/skills/leanwarp/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.symlink_to(mine)
    report = setup(agents=["agents"], home=home, environ={}, run=claude)
    assert report["agents"][0]["skill_files_left"] == [{"path": str(skill), "state": "symlink"}]
    assert mine.read_text() == "my own skill"
    setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    assert mine.read_text() == "my own skill" and skill.is_symlink()


def test_setup_leaves_a_users_skill_file_and_removal_keeps_an_identical_one(machine):
    home, claude = machine
    skills = home / ".agents/skills/leanwarp"
    (skills / "references").mkdir(parents=True)
    (skills / "SKILL.md").write_text("the user's version")
    shipped = (Path(agents.__file__).parent / "skills/leanwarp/references/usage.md").read_text()
    (skills / "references/usage.md").write_text(shipped)
    report = setup(agents=["agents"], home=home, environ={}, run=claude)
    assert report["agents"][0]["skill_files_left"] == [
        {"path": str(skills / "SKILL.md"), "state": "user_file"}
    ]
    assert (skills / "SKILL.md").read_text() == "the user's version"
    removed = setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    # The identical copy was there before setup ran, so it stays too.
    assert removed["agents"][0]["skill"] == "preexisting"
    assert (skills / "references/usage.md").read_text() == shipped
    assert (skills / "SKILL.md").read_text() == "the user's version"


def test_a_parent_swapped_after_validation_leaves_the_other_folder_untouched(machine, monkeypatch):
    home, claude = machine
    setup(agents=["agents"], home=home, environ={}, run=claude)
    skill = home / ".agents/skills/leanwarp/SKILL.md"
    folder = skill.parent
    copy = skill.read_bytes()
    resolve = Path.resolve
    swapped: list[Path] = []

    def swapping(self: Path, strict: bool = False) -> Path:
        resolved = resolve(self, strict)
        # Removal has just validated the skill's path: now another folder takes the
        # parent's place, holding a file byte-identical to the one setup wrote.
        if self == skill and not swapped:
            swapped.append(folder.rename(home / "setups-folder"))
            folder.mkdir()
            (folder / "SKILL.md").write_bytes(copy)
        return resolved

    monkeypatch.setattr(Path, "resolve", swapping)
    report = setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    monkeypatch.undo()
    assert swapped and report["agents"][0]["skill"] == "moved"
    assert (folder / "SKILL.md").read_bytes() == copy


def test_removal_refuses_where_it_cannot_work_relative_to_a_folder(machine, monkeypatch):
    home, claude = machine
    setup(agents=["agents"], home=home, environ={}, run=claude)
    monkeypatch.setattr(agents.os, "supports_dir_fd", set())
    report = setup(agents=["agents"], remove=True, home=home, environ={}, run=claude)
    assert report["agents"][0]["skill"] == "unsupported"
    assert (home / ".agents/skills/leanwarp/SKILL.md").exists()


def test_a_skill_write_replaces_a_link_at_its_path_and_never_its_destination(tmp_path: Path):
    """A link that appears at the path after setup looked is replaced, not followed."""
    theirs = tmp_path / "theirs.md"
    theirs.write_text("the user's own file\n", encoding="utf-8")
    path = tmp_path / "skill" / "SKILL.md"
    path.parent.mkdir()
    path.symlink_to(theirs)
    agents._write(path, "setup's text\n", mode=0o644, through_link=False)
    assert theirs.read_text(encoding="utf-8") == "the user's own file\n"
    assert not path.is_symlink() and path.read_text(encoding="utf-8") == "setup's text\n"


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="counts descriptors through procfs")
def test_reading_a_recorded_file_that_became_a_directory_leaks_no_descriptor(tmp_path: Path):
    (tmp_path / "SKILL.md").mkdir()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(8):
            with pytest.raises(agents._NotAFile):
                agents._Pinned(directory, "SKILL.md").read()
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        os.close(directory)

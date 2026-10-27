from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, __version__, cli, updates
from leanwarp_cloud.agents import setup


@pytest.fixture(autouse=True)
def no_release_seen():
    updates._latest.clear()
    yield
    updates._latest.clear()


def completed(code: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


def no_claude(command: Sequence[str], cwd: object = None) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(command[0])


def test_setup_installs_skills_and_adds_mcp_entries_without_replacing_any(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "o4"\n')
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}')

    report = setup(home=tmp_path, environ={}, run=no_claude)
    results = {entry["agent"]: entry for entry in report["agents"]}
    for agent, folder in (("claude-code", ".claude"), ("codex", ".codex"), ("agents", ".agents")):
        skill = tmp_path / folder / "skills" / "leanwarp"
        assert results[agent]["skill"] == str(skill)
        assert (skill / "SKILL.md").read_text().startswith("---\nname: leanwarp")
        assert (skill / "references" / "usage.md").exists()
    assert results["claude-code"]["mcp"] == "claude_cli_not_found"
    assert results["codex"]["mcp"] == "configured"
    codex = (tmp_path / ".codex" / "config.toml").read_text()
    assert (
        codex.startswith('model = "o4"\n\n[mcp_servers.leanwarp]\n') and 'args = ["mcp"]' in codex
    )
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert cursor["other"] == {"command": "x"}
    assert cursor["leanwarp"]["args"] == ["--project", "${workspaceFolder}", "mcp"]

    again = {
        entry["agent"]: entry for entry in setup(home=tmp_path, environ={}, run=no_claude)["agents"]
    }
    assert again["codex"]["mcp"] == again["cursor"]["mcp"] == "already_configured"
    assert (tmp_path / ".codex" / "config.toml").read_text() == codex


def test_setup_registers_claude_code_for_the_user_from_the_home_folder(tmp_path):
    (tmp_path / ".claude").mkdir()
    calls: list[tuple[list[str], object]] = []
    registered: set[str] = set()

    # Claude Code's own replies: added once, then "already exists" for a user entry.
    def claude(command: Sequence[str], cwd: object = None) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), cwd))
        if "leanwarp" in registered:
            return subprocess.CompletedProcess(
                [], 1, "", "MCP server leanwarp already exists in user config\n"
            )
        registered.add("leanwarp")
        return completed()

    first = setup(agents=["claude-code"], home=tmp_path, environ={}, run=claude)
    again = setup(agents=["claude-code"], home=tmp_path, environ={}, run=claude)
    assert first["agents"][0]["mcp"] == "configured"
    assert again["agents"][0]["mcp"] == "already_configured"
    command, cwd = calls[0]
    assert command[:6] == ["claude", "mcp", "add", "--scope", "user", "leanwarp"]
    assert command[-1] == "mcp"
    # Run from home, never a project whose .mcp.json could answer for the user scope.
    assert cwd == tmp_path
    broken = setup(
        agents=["claude-code"],
        home=tmp_path,
        environ={},
        run=lambda command, cwd=None: subprocess.CompletedProcess([], 1, "", "boom"),
    )
    assert broken["agents"][0]["mcp"] == "failed"


def test_setup_skips_missing_agents_unless_named_and_honours_their_homes(tmp_path):
    codex_home = tmp_path / "elsewhere"
    report = setup(home=tmp_path, environ={"CODEX_HOME": str(codex_home)}, run=no_claude)
    statuses = {entry["agent"]: entry["status"] for entry in report["agents"]}
    assert statuses == {
        "claude-code": "not_found",
        "codex": "not_found",
        "cursor": "not_found",
        "agents": "ok",
    }
    named = setup(agents=["codex"], home=tmp_path, environ={"CODEX_HOME": str(codex_home)})
    assert named["agents"][0]["skill"] == str(codex_home / "skills" / "leanwarp")
    assert (codex_home / "config.toml").read_text().startswith("[mcp_servers.leanwarp]")


def test_refresh_replaces_installed_skills_only_and_leaves_mcp_alone(tmp_path):
    (tmp_path / ".cursor").mkdir()
    stale = tmp_path / ".agents" / "skills" / "leanwarp" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old guide")
    report = setup(refresh=True, home=tmp_path, environ={}, run=no_claude)
    assert [entry["agent"] for entry in report["agents"]] == ["agents"]
    assert stale.read_text() != "old guide"
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_upgrade_command_follows_the_installation(tmp_path):
    command = updates.upgrade_command
    assert command(tmp_path, "py", '{"dir_info": {"editable": true}}', has_pip=True) is None
    assert command(tmp_path, "py", None, has_pip=True) == [
        "py",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "leanwarp",
    ]
    assert command(tmp_path, "py", None, has_pip=False)[:3] == ["uv", "pip", "install"]
    (tmp_path / "pipx_metadata.json").write_text("{}")
    assert command(tmp_path, "py", None, has_pip=True) == ["pipx", "upgrade", "leanwarp"]
    (tmp_path / "uv-receipt.toml").write_text("")
    assert command(tmp_path, "py", "not json", has_pip=True) == [
        "uv",
        "tool",
        "upgrade",
        "leanwarp",
    ]


def test_update_upgrades_then_refreshes_skills_with_the_new_release(monkeypatch):
    monkeypatch.setattr(
        updates, "upgrade_command", lambda *_a, **_k: ["uv", "tool", "upgrade", "leanwarp"]
    )
    commands: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[-1] == "--version":
            return completed(stdout="leanwarp 9.9.9\n")
        if command[-1] == "--refresh":
            return completed(stdout=json.dumps({"agents": [{"agent": "agents", "skill": "/s"}]}))
        return completed()

    report = updates.update(run=run)
    assert report["status"] == "updated" and report["version"] == "9.9.9"
    assert report["previous"] == __version__ and report["skills_refreshed"] == ["/s"]
    assert commands[0] == ["uv", "tool", "upgrade", "leanwarp"]
    assert commands[1][0].endswith("/leanwarp") and commands[2][-2:] == ["setup", "--refresh"]
    failed = updates.update(run=lambda command: completed(1))
    assert failed["status"] == "failed" and failed["command"] == "uv tool upgrade leanwarp"


def test_update_refuses_a_development_checkout(monkeypatch):
    class Editable:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return '{"url": "file:///src", "dir_info": {"editable": true}}'

    # Whatever installed the tests, the update reads this installation's record.
    monkeypatch.setattr(updates, "distribution", lambda name: Editable())
    assert updates.update(run=no_claude)["status"] == "development_install"


def serving(monkeypatch, response: httpx.Response) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    monkeypatch.setattr(
        cli,
        "load_client",
        lambda *, interface: LeanWarpCloud(
            "secret", base_url="https://api.example", transport=httpx.MockTransport(handle)
        ),
    )
    return requests


def test_results_say_when_a_newer_release_exists(monkeypatch, capsys):
    funds = {"owner_id": "o", "balance_microusd": "0"}
    serving(monkeypatch, httpx.Response(200, json=funds, headers={"LeanWarp-SDK-Latest": "99.0"}))
    assert cli.main(["account"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["update_available"] == {
        "current": __version__,
        "latest": "99.0",
        "command": "leanwarp update",
    }
    serving(
        monkeypatch, httpx.Response(200, json=funds, headers={"LeanWarp-SDK-Latest": __version__})
    )
    assert cli.main(["account"]) == 0
    assert "update_available" not in json.loads(capsys.readouterr().out)


def test_an_sdk_below_the_minimum_reports_how_to_update(monkeypatch, capsys):
    refusal = {"error": {"code": "sdk_upgrade_required", "message": "Run `leanwarp update`."}}
    serving(monkeypatch, httpx.Response(426, json=refusal, headers={"LeanWarp-SDK-Latest": "99.0"}))
    assert cli.main(["account"]) == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["error"] == "sdk_upgrade_required" and printed["http_status"] == 426
    assert printed["update_available"]["latest"] == "99.0"


@pytest.mark.parametrize("arguments,opens", [((), True), (("--no-browser",), False)])
def test_login_opens_the_dashboard_keys_then_reads_the_pasted_key(
    monkeypatch, capsys, arguments, opens
):
    opened: list[str] = []
    saved: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "lw_test_key")
    monkeypatch.setattr(cli, "save_credentials", saved.append)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    assert cli.main(["auth", "login", *arguments]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "authenticated"}
    assert saved == ["lw_test_key"]
    assert opened == ([cli.KEYS_URL] if opens else [])
    assert cli.KEYS_URL in output.err and "lw_test_key" not in output.err + output.out


def test_setup_needs_no_key_and_says_what_is_next(monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup", lambda **_: {"agents": [{"agent": "agents", "status": "ok"}]})
    monkeypatch.setattr(cli, "signed_in", lambda: False)
    assert cli.main(["setup"]) == 0
    assert "auth login" in json.loads(capsys.readouterr().out)["next"]


@pytest.mark.parametrize(
    "existing,status",
    [
        ('[mcp_servers."leanwarp"]\ncommand = "leanwarp"\n', "already_configured"),
        ("mcp_servers.leanwarp.command = 'x'\n", "already_configured"),
        ("mcp_servers = { other = { command = 'x' } }\n", "unreadable_config"),
        ("[mcp_servers\nbroken", "unreadable_config"),
    ],
)
def test_codex_config_is_read_as_toml_and_never_left_unreadable(tmp_path, existing, status):
    import tomllib

    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(existing)
    report = setup(agents=["codex"], home=tmp_path, environ={}, run=no_claude)
    assert report["agents"][0]["mcp"] == status
    assert config.read_text() == existing
    if status == "already_configured":
        tomllib.loads(config.read_text())


def test_setup_exits_nonzero_when_a_config_could_not_be_read(monkeypatch, capsys):
    unreadable = {"agents": [{"agent": "codex", "status": "ok", "mcp": "unreadable_config"}]}
    monkeypatch.setattr(cli, "setup", lambda **_: unreadable)
    monkeypatch.setattr(cli, "signed_in", lambda: True)
    assert cli.main(["setup"]) == 2
    assert json.loads(capsys.readouterr().out)["agents"][0]["mcp"] == "unreadable_config"


def test_update_reports_a_timeout_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(
        updates, "upgrade_command", lambda *_a, **_k: ["uv", "tool", "upgrade", "leanwarp"]
    )

    def slow(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(command), 600)

    report = updates.update(run=slow)
    assert report["status"] == "failed" and "timed out" in report["message"]


def test_update_says_when_installed_skills_were_not_refreshed(monkeypatch):
    monkeypatch.setattr(
        updates, "upgrade_command", lambda *_a, **_k: ["uv", "tool", "upgrade", "leanwarp"]
    )

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return completed(stdout="leanwarp 9.9.9\n")
        if command[-1] == "--refresh":
            return completed(2, stdout='{"error": "local_error"}')
        return completed()

    report = updates.update(run=run)
    assert report["status"] == "skills_not_refreshed" and report["version"] == "9.9.9"
    assert "leanwarp setup --refresh" in report["message"]

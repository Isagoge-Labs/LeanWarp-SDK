from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from leanwarp_cloud import OperationOutcome
from leanwarp_cloud.config import load_client
from leanwarp_cloud.session import SessionError

from .test_session import AccountWorkspaces, Service

TEST_KEY = f"lw_test_{'a' * 32}.{'s' * 43}"
# Only the test process redirects the fixed staging origin to its local fixture.
# Installed users authenticate with the key alone; no URL-setting CLI is needed.
CLI_ENTRY = """
import os
from leanwarp_cloud import endpoints
from leanwarp_cloud.cli import main
endpoints._API_ORIGINS['test'] = os.environ['TEST_SERVER_ORIGIN']
raise SystemExit(main())
"""


@contextmanager
def local_api():
    service = Service()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_PUT(self):
            self.respond()

        def respond(self):
            if self.path.endswith("/stop"):
                body = {"workspace_id": "w", "state": "stopped", "revision": service.revision}
                response = httpx.Response(200, json=body)
            else:
                request = httpx.Request(
                    self.command,
                    f"http://127.0.0.1{self.path}",
                    headers=dict(self.headers),
                    content=self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
                assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
                response = service.handle(request)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def project(root):
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (root / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (root / "Main.lean").write_text("theorem candidate : True := by trivial\n")


@pytest.mark.parametrize(
    "arguments,heading",
    [((), "name: leanwarp"), (("--reference",), "# LeanWarp reference")],
)
def test_cli_guidance_works_without_credentials_or_project(tmp_path, arguments, heading):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LEANWARP_BASE_URL", "LEANWARP_API_KEY"}
    }
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    run = subprocess.run(  # noqa: S603 -- fixed module and test arguments
        [sys.executable, "-I", "-m", "leanwarp_cloud.cli", "skill", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert heading in run.stdout
    assert not run.stderr
    assert not (tmp_path / ".leanwarp").exists()
    assert not (tmp_path / "config").exists()


def test_cli_installed_project_workflow_and_skill(tmp_path):
    project(tmp_path)
    with local_api() as (service, origin):
        environment = {
            **os.environ,
            "TEST_SERVER_ORIGIN": origin,
            "LEANWARP_API_KEY": TEST_KEY,
        }

        def command(*args, expected=0):
            run = subprocess.run(  # noqa: S603 -- fixed module and test arguments
                [sys.executable, "-c", CLI_ENTRY, "--project", str(tmp_path), *args],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert TEST_KEY not in run.stdout + run.stderr
            assert run.returncode == expected, run.stderr + run.stdout
            return run.stdout

        assert "name: leanwarp" in command("skill")
        diagnosis = json.loads(command("doctor"))
        assert diagnosis["compatible"] is True
        assert diagnosis["matching_bundles"]
        assert diagnosis["worker"] == {"usage_based": False}
        assert "resources" not in diagnosis and "default_resource_profile" not in diagnosis
        assert diagnosis["base_url"] == origin
        source_bytes = len((tmp_path / "Main.lean").read_bytes()) + len("Main.lean")
        assert diagnosis["upload"] == {
            "file_count": 1,
            "total_bytes": source_bytes,
            "problems": [],
        }
        upload = json.loads(command("files"))
        assert [file["path"] for file in upload["files"]] == ["Main.lean"]
        assert (
            json.loads(command("connect", "--bundle", "b", "--max-spend", "2.000001"))[
                "workspace_id"
            ]
            == "w"
        )
        creation = next(r for r in service.requests if r.method == "POST")
        assert json.loads(creation.content)["max_spend_microusd"] == 2_000_001
        assert json.loads(command("connect"))["max_spend_microusd"] == 2_000_001
        mismatch = json.loads(command("connect", "--max-spend", "3", expected=2))
        assert "different spending cap" in mismatch["message"]
        assert json.loads(command("versions"))["versions"]
        assert json.loads(command("resources")) == {"worker": {"usage_based": False}}
        assert json.loads(command("sync"))["revision"] == 1
        first = json.loads(command("check", "Main.lean", expected=1))
        assert first["kind"] == "check"
        # Results lead with whether they passed; the full server receipt follows.
        assert next(iter(first)) == "success" and first["success"] is False
        assert json.loads(command("wait", expected=1))["operation_id"] == first["operation_id"]

        def submitted() -> dict:
            posted = (r for r in reversed(service.requests) if r.method == "POST")
            return json.loads(next(posted).content)["payload"]

        assert submitted() == {"file": "Main.lean", "strict": True}
        command("check", "Main.lean", "--draft", expected=1)
        assert submitted() == {"file": "Main.lean", "strict": False}
        invalid = json.loads(command("check", "Main.lean", "--wait", "99", expected=2))
        assert invalid["error"] == "invalid_arguments"
        assert "between 0 and 40 seconds" in invalid["message"]
        service.pending_polls = 1_000
        running = json.loads(command("check", "Main.lean", "--wait", "0.2", expected=3))
        assert running["state"] == "queued" and running["success"] is None
        service.pending_polls = 1
        assert json.loads(command("wait", expected=1))["state"] == "completed"
        (tmp_path / "Main.lean").write_text("theorem candidate : True := True.intro\n")
        command("verify", "Main.lean", "--declaration", "candidate", "--target", "True", expected=1)
        assert service.workspace_count == 1
        assert service.revision == 2
        assert "True.intro" in service.sources["Main.lean"]
        body = json.loads(next(r.content for r in reversed(service.requests) if r.method == "POST"))
        assert body["payload"]["execution_mode"] == "reusable"
        assert json.loads(command("stop"))["state"] == "stopped"


def test_mcp_stdio_uses_same_project_session_without_key_arguments(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    project(tmp_path)
    with local_api() as (service, origin):

        async def exercise():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", CLI_ENTRY, "--project", str(tmp_path), "mcp"],
                env={"TEST_SERVER_ORIGIN": origin, "LEANWARP_API_KEY": TEST_KEY},
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as client,
            ):
                initialized = await client.initialize()
                assert "leanwarp://reference" in initialized.instructions
                available = await client.list_resources()
                assert {str(resource.uri) for resource in available.resources} == {
                    "leanwarp://guide",
                    "leanwarp://reference",
                }
                for uri, heading in (
                    ("leanwarp://guide", "# LeanWarp"),
                    ("leanwarp://reference", "# LeanWarp reference"),
                ):
                    resource = await client.read_resource(uri)
                    assert heading in resource.contents[0].text
                    assert TEST_KEY not in resource.contents[0].text
                assert not (tmp_path / ".leanwarp").exists()
                catalog = await client.list_tools()
                assert {
                    "connect",
                    "verify_target",
                    "recover",
                    "stop",
                    "workspaces",
                    "history",
                    "delete",
                } <= {t.name for t in catalog.tools}
                hints = {t.name: t.annotations for t in catalog.tools}
                assert hints["delete"].destructiveHint is True
                assert hints["history"].readOnlyHint is True
                assert "api_key" not in json.dumps([t.inputSchema for t in catalog.tools])
                connect_schema = next(t.inputSchema for t in catalog.tools if t.name == "connect")
                assert "max_spend_microusd" in connect_schema["properties"]
                assert "profile" not in json.dumps([t.inputSchema for t in catalog.tools])
                resources = await client.call_tool("resources", {})
                assert not resources.isError, resources
                assert json.loads(resources.content[0].text) == {"worker": {"usage_based": False}}
                connected = await client.call_tool("connect", {"max_spend_microusd": 2_000_001})
                assert not connected.isError, connected
                body = json.loads(next(r.content for r in service.requests if r.method == "POST"))
                assert body["max_spend_microusd"] == 2_000_001
                conflict = await client.call_tool("connect", {"max_spend_microusd": 3_000_000})
                assert conflict.isError
                assert "different spending cap" in conflict.content[0].text
                submitted = await client.call_tool("check", {"file": "Main.lean"})
                assert not submitted.isError, submitted
                # The default inline wait returns the finished result in the same call.
                assert json.loads(submitted.content[0].text)["state"] == "completed"
                result = await client.call_tool("wait", {"timeout": 1})
                assert not result.isError, result
                stopped = await client.call_tool("stop")
                assert not stopped.isError, stopped

        asyncio.run(exercise())
        assert service.workspace_count == 1
        assert (tmp_path / ".leanwarp" / "session.json").is_file()


def test_mcp_redacts_unexpected_transport_details(tmp_path):
    from leanwarp_cloud import LeanWarpCloud
    from leanwarp_cloud.mcp_server import create_server

    def fail(_request):
        raise httpx.ConnectError("Authorization: Bearer should-never-leak")

    with LeanWarpCloud(
        "should-never-leak",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(fail),
    ) as cloud:
        server = create_server(cloud, str(tmp_path))

        async def call():
            with pytest.raises(Exception) as raised:
                await server.call_tool("account", {})
            assert "should-never-leak" not in str(raised.value)
            assert "transport_error" in str(raised.value)

        asyncio.run(call())


@pytest.mark.parametrize(
    "kind,result,success",
    [
        ("inspect", {"status": "available", "goal_contexts": [{"target": "True"}]}, True),
        ("inspect", {"status": "proof_state", "goal_contexts": [{"target": "True"}]}, True),
        ("inspect", {"status": "metadata_only", "goal_contexts": [{"target": "True"}]}, True),
        (
            "inspect",
            {
                "status": "metadata_only",
                "goal_contexts": [{"target": "True"}],
                "retained_proof_state": False,
                "observation_only": True,
            },
            True,
        ),
        (
            "inspect",
            {
                "status": "metadata_only",
                "goal_status": "available",
                "goal_contexts": [{"target": "True"}],
            },
            True,
        ),
        (
            "inspect",
            {
                "status": "metadata_only",
                "goal_status": "unavailable",
                "goal_contexts": [{"target": "True"}],
            },
            False,
        ),
        ("inspect", {"status": "metadata_only", "goal_contexts": []}, False),
        ("inspect", {"status": "unavailable", "goal_contexts": []}, False),
        ("inspect", {"status": "available", "goal_contexts": [{}]}, False),
        ("inspect", {"status": "error"}, False),
        ("try_tactics", {"results": [{"status": "failed"}]}, True),
        ("check", {"status": "rejected"}, False),
    ],
)
def test_engine_outcomes_do_not_confuse_trial_execution_with_proof(kind, result, success):
    outcome = OperationOutcome(
        {
            "operation_id": "o",
            "revision": 1,
            "state": "completed",
            "kind": kind,
            "result": {"operation_id": "o", "revision": 1, "generation": 1, "result": result},
        }
    )
    assert outcome.successful is success
    assert not outcome.verified


@pytest.mark.parametrize("value", ["nan", "1.0000001", "1000000.000001", "-1"])
def test_cli_cap_rejects_invalid_amounts_before_loading_credentials(value, monkeypatch, capsys):
    from leanwarp_cloud import cli

    def not_loaded(*, interface):
        pytest.fail("invalid cap must not load credentials or contact the service")

    monkeypatch.setattr(cli, "load_client", not_loaded)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["connect", "--max-spend", value])
    assert stopped.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "invalid_arguments"


@pytest.mark.parametrize("cap", [-1, 10**12 + 1, True, 1.0, 1.5, "2000000"])
def test_mcp_rejects_invalid_cap_before_contacting_service(tmp_path, cap):
    from leanwarp_cloud import LeanWarpCloud
    from leanwarp_cloud.mcp_server import create_server

    requests: list[httpx.Request] = []
    with LeanWarpCloud(
        TEST_KEY,
        base_url="https://api.example",
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    ) as cloud:
        server = create_server(cloud, str(tmp_path))

        async def call():
            with pytest.raises(Exception, match="max_spend_microusd"):
                await server.call_tool("connect", {"max_spend_microusd": cap})

        asyncio.run(call())
    assert requests == []
    assert not (tmp_path / ".leanwarp").exists()


def test_credentials_require_key_and_reject_public_readable_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    with pytest.raises(SessionError, match="auth login"):
        load_client()
    path = tmp_path / "leanwarp" / "credentials.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"api_key": TEST_KEY}))
    path.chmod(0o644)
    with pytest.raises(SessionError, match="readable only by you"):
        load_client()
    path.chmod(0o600)
    with load_client() as client:
        assert client.base_url == "https://control-api-staging-3b57.up.railway.app"


def test_cli_help_describes_every_command_and_reports_its_version(capsys):
    from leanwarp_cloud import __version__
    from leanwarp_cloud.cli import parser

    root = parser()
    commands = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    described = {action.dest for action in commands._choices_actions if action.help}
    assert set(commands.choices) - {"versions"} <= described
    for name, command in commands.choices.items():
        for action in command._actions:
            if action.option_strings and action.dest != "help" and action.help is not None:
                assert action.help.strip(), (name, action.dest)
    with pytest.raises(SystemExit) as exited:
        root.parse_args(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"leanwarp {__version__}"


def test_cli_reports_argument_mistakes_as_json(capsys):
    from leanwarp_cloud.cli import main

    with pytest.raises(SystemExit) as exited:
        main(["inspect", "Main.lean", "--line", "1"])
    assert exited.value.code == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"] == "invalid_arguments"
    assert "--column" in error["message"]


def test_cli_keeps_legacy_profile_flags_out_of_customer_help():
    from leanwarp_cloud.cli import parser

    root = parser()
    commands = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    for name in ("connect", "check", "inspect", "try-tactics", "verify"):
        assert "--profile" not in commands.choices[name].format_help()
        assert "--max-profile" not in commands.choices[name].format_help()
    connected = root.parse_args(["connect", "--profile", "standard", "--max-profile", "large"])
    assert connected.profile == "standard" and connected.max_profile == "large"
    checked = root.parse_args(["check", "Main.lean", "--profile", "standard"])
    assert checked.profile == "standard"


def test_cli_forwards_legacy_profile_flags_for_existing_automation(tmp_path, monkeypatch, capsys):
    from leanwarp_cloud import LeanWarpCloud, cli

    project(tmp_path)
    service = Service()
    monkeypatch.setattr(
        cli,
        "load_client",
        lambda *, interface: LeanWarpCloud(
            TEST_KEY,
            base_url="https://api.example",
            transport=httpx.MockTransport(service.handle),
            interface=interface,
        ),
    )
    assert (
        cli.main(
            [
                "--project",
                str(tmp_path),
                "connect",
                "--profile",
                "standard",
                "--max-profile",
                "large",
            ]
        )
        == 0
    )
    create = next(request for request in service.requests if request.method == "POST")
    assert json.loads(create.content) == {
        "bundle_id": "b",
        "resource_profile": "standard",
        "max_resource_profile": "large",
        "max_spend_microusd": None,
    }
    assert cli.main(["--project", str(tmp_path), "check", "Main.lean", "--profile", "large"]) == 1
    submit = next(request for request in reversed(service.requests) if request.method == "POST")
    assert json.loads(submit.content)["resource_profile"] == "large"
    assert TEST_KEY not in capsys.readouterr().out


def test_cli_explains_a_malformed_key(monkeypatch, capsys, tmp_path):
    from leanwarp_cloud.cli import main

    monkeypatch.setenv("LEANWARP_API_KEY", "lw_live_fake")
    (tmp_path / "Main.lean").write_text("example : True := trivial")
    assert main(["--project", str(tmp_path), "doctor"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert "not a LeanWarp API key" in error["message"]


def test_mcp_starts_and_explains_sign_in_before_credentials_exist(tmp_path, monkeypatch):
    from leanwarp_cloud.mcp_server import create_server

    def not_signed_in():
        raise SessionError("not signed in; run `leanwarp auth login` or set LEANWARP_API_KEY")

    server = create_server(not_signed_in, str(tmp_path))

    async def call():
        guide = await server.read_resource("leanwarp://guide")
        assert "# LeanWarp" in next(iter(guide)).content
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert {"doctor", "environments", "connect", "verify_target", "files"} <= set(tools)
        assert "draft" in tools["check"].inputSchema["properties"]
        with pytest.raises(Exception, match="auth login"):
            await server.call_tool("doctor", {})
        # Listing the upload reads only local files, so it works before sign-in.
        listing = await server.call_tool("files", {})
        assert '"file_count": 0' in listing[0][0].text

    asyncio.run(call())


def test_cli_exports_history_without_overwriting_and_deletes_only_when_named(
    tmp_path, monkeypatch, capsys
):
    from leanwarp_cloud import LeanWarpCloud, cli

    project(tmp_path)
    service = AccountWorkspaces()
    monkeypatch.setattr(
        cli,
        "load_client",
        lambda *, interface: LeanWarpCloud(
            TEST_KEY,
            base_url="https://api.example",
            transport=httpx.MockTransport(service.handle),
            interface=interface,
        ),
    )

    def run(*arguments: str) -> tuple[int, dict]:
        code = cli.main(["--project", str(tmp_path), *arguments])
        return code, json.loads(capsys.readouterr().out.splitlines()[-1])

    assert run("connect", "--workspace", "w")[0] == 0
    output = tmp_path / "history.jsonl"
    assert run("export", "--output", str(output)) == (
        0,
        {"status": "exported", "operations": 5, "output": str(output)},
    )
    lines = output.read_text().splitlines()
    assert [json.loads(line)["operation_id"] for line in lines] == [f"op-{n}" for n in range(5)]
    code, refused = run("export", "--output", str(output))
    assert code == 2 and "exists" in refused["message"]
    assert output.read_text().splitlines() == lines
    # A failure part-way leaves no file that could pass for a complete export.
    service.history[4] = {**service.history[4], "workspace_id": "x"}
    partial = tmp_path / "partial.jsonl"
    assert run("export", "--output", str(partial))[0] == 2
    assert not partial.exists()

    with pytest.raises(SystemExit):
        cli.main(["--project", str(tmp_path), "delete"])
    capsys.readouterr()
    assert service.deleted == []
    assert run("delete", "--confirm", "w") == (
        0,
        {"status": "deleted", "workspace_id": "w", "disconnected": True},
    )
    assert TEST_KEY not in output.read_text()


def test_doctor_checks_a_project_against_the_public_catalog_before_sign_in(
    tmp_path, monkeypatch, capsys
):
    from leanwarp_cloud import LeanWarpCloud, cli, endpoints

    project(tmp_path)
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    service = Service()
    monkeypatch.setitem(endpoints._API_ORIGINS, "live", "https://public.example")
    monkeypatch.setattr(
        cli,
        "LeanWarpCloud",
        lambda key, *, interface: LeanWarpCloud(
            key, transport=httpx.MockTransport(service.handle), interface=interface
        ),
    )
    assert cli.main(["--project", str(tmp_path), "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["compatible"] is True and report["signed_in"] is False
    assert report["environment"]["match"] == "exact"
    assert "auth login" in report["next"]
    (request,) = service.requests
    assert request.url == "https://public.example/v1/leanwarp/versions"
    assert "Authorization" not in request.headers
    assert not (tmp_path / ".leanwarp").exists()
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.99.0\n")
    assert cli.main(["--project", str(tmp_path), "doctor"]) == 1
    assert "No LeanWarp environment matches" in json.loads(capsys.readouterr().out)["message"]


def _completed(kind: str, result: dict[str, Any]) -> OperationOutcome:
    return OperationOutcome(
        {
            "operation_id": "o",
            "revision": 1,
            "state": "completed",
            "kind": kind,
            "result": {"operation_id": "o", "revision": 1, "generation": 1, "result": result},
        }
    )


@pytest.mark.parametrize(
    ("kind", "result", "success"),
    [
        ("suggest", {"status": "closed", "verification": "strict", "patch": {}}, True),
        ("suggest", {"status": "closed", "verification": "declaration", "patch": {}}, True),
        ("suggest", {"status": "closed", "verification": "partial", "patch": {}}, True),
        # A tier this client does not know is never taken for a proof.
        ("suggest", {"status": "closed", "verification": "no_new_errors", "patch": {}}, False),
        # A closing tactic without a recheck verdict or patch is not a usable suggestion.
        ("suggest", {"status": "closed", "patch": {}}, False),
        ("suggest", {"status": "closed", "verification": "strict"}, False),
        ("suggest", {"status": "not_closed"}, False),
        ("holes", {"status": "ok", "holes": []}, True),
        # Listing a file that does not compile yet still lists it.
        ("holes", {"status": "error", "holes": []}, True),
        ("holes", {"status": "error"}, False),
        ("extract_decls", {"status": "ok", "declarations": []}, True),
        ("extract_decls", {"status": "error", "declarations": []}, True),
        ("extract_decls", {"status": "timeout", "declarations": []}, False),
        ("sorry2lemma", {"status": "extracted"}, True),
        ("sorry2lemma", {"status": "no_targets"}, True),
        ("sorry2lemma", {"status": "failed"}, False),
        ("check_files", {"status": "ok", "failed": 0}, True),
        ("check_files", {"status": "failed", "failed": 1}, False),
        # Every file must pass, whatever an inconsistent summary claims.
        ("check_files", {"status": "ok", "failed": 1}, False),
    ],
)
def test_source_tool_outcomes_report_success_only_for_usable_results(kind, result, success):
    outcome = _completed(kind, result)
    assert outcome.successful is success
    assert not outcome.verified


@pytest.mark.parametrize(
    ("arguments", "kind", "payload"),
    [
        (
            ["suggest", "Main.lean", "--line", "2", "--column", "3"],
            "suggest",
            {"file": "Main.lean", "line": 2, "column": 3},
        ),
        (
            [
                "suggest",
                "Main.lean",
                "--line",
                "2",
                "--column",
                "3",
                "--tactic",
                "omega",
                "--tactic",
                "simp",
                "--tactic-timeout",
                "500",
            ],
            "suggest",
            {
                "file": "Main.lean",
                "line": 2,
                "column": 3,
                "tactics": ["omega", "simp"],
                "timeout_ms": 500,
            },
        ),
        (["holes", "Main.lean"], "holes", {"file": "Main.lean", "goals": True}),
        (["holes", "Main.lean", "--no-goals"], "holes", {"file": "Main.lean", "goals": False}),
        (["declarations", "Main.lean"], "extract_decls", {"file": "Main.lean"}),
        (
            ["sorry2lemma", "Main.lean", "--max", "3", "--prefix", "goal"],
            "sorry2lemma",
            {
                "file": "Main.lean",
                "max_extractions": 3,
                "strict_final_check": False,
                "name_prefix": "goal",
            },
        ),
        (
            ["check-files", "Main.lean", "Other.lean", "--file-timeout", "30"],
            "check_files",
            {"files": ["Main.lean", "Other.lean"], "strict": True, "timeout": 30},
        ),
        (
            ["check-files", "Main.lean", "--draft"],
            "check_files",
            {"files": ["Main.lean"], "strict": False},
        ),
    ],
)
def test_cli_source_tools_submit_exactly_their_payload(
    tmp_path, monkeypatch, capsys, arguments, kind, payload
):
    from leanwarp_cloud import LeanWarpCloud, cli

    project(tmp_path)
    (tmp_path / "Other.lean").write_text("theorem other : True := trivial\n")
    service = Service()
    monkeypatch.setattr(
        cli,
        "load_client",
        lambda *, interface: LeanWarpCloud(
            TEST_KEY,
            base_url="https://api.example",
            transport=httpx.MockTransport(service.handle),
            interface=interface,
        ),
    )
    assert cli.main(["--project", str(tmp_path), "connect"]) == 0
    cli.main(["--project", str(tmp_path), *arguments, "--wait", "0"])
    submit = next(request for request in reversed(service.requests) if request.method == "POST")
    body = json.loads(submit.content)
    assert body["kind"] == kind and body["payload"] == payload
    assert TEST_KEY not in capsys.readouterr().out


def test_mcp_exposes_every_source_tool(tmp_path):
    from leanwarp_cloud.mcp_server import create_server

    server = create_server(lambda: None, str(tmp_path))  # type: ignore[arg-type, return-value]

    async def names() -> dict[str, Any]:
        return {tool.name: tool for tool in await server.list_tools()}

    tools = asyncio.run(names())
    assert {"suggest", "holes", "extract_decls", "sorry2lemma", "check_files", "apply"} <= set(
        tools
    )
    assert tools["holes"].annotations.readOnlyHint is False
    assert set(tools["check_files"].inputSchema["required"]) == {"files"}
    assert "tactics" in tools["suggest"].inputSchema["properties"]


def test_edits_apply_only_to_the_exact_source_and_promised_result():
    import hashlib

    from leanwarp_cloud.edits import EditError, edited_source

    source = "theorem t : True := by\n  sorry\n"
    start = source.index("sorry")
    edited = source.replace("sorry", "trivial")
    result = {
        "status": "closed",
        "verification": "partial",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "patched_source_sha256": hashlib.sha256(edited.encode()).hexdigest(),
        "patch": {
            "start_utf8_offset": start,
            "end_utf8_offset": start + 5,
            "replacement": "trivial",
        },
    }
    assert edited_source("suggest", result, source) == edited
    for broken, message in (
        ({**result, "source_sha256": "0" * 64}, "changed"),
        ({**result, "patched_source_sha256": "0" * 64}, "checksum"),
        ({**result, "status": "not_closed"}, "closed no hole"),
        # Only a recheck tier this client knows makes a suggestion usable.
        ({**result, "verification": "no_new_errors"}, "closed no hole"),
        ({**result, "patch": {**result["patch"], "end_utf8_offset": 10_000}}, "malformed"),
    ):
        with pytest.raises(EditError, match=message):
            edited_source("suggest", broken, source)
    wide = "theorem t : True := by\n  sorry -- ∀\n"
    split = {
        **result,
        "source_sha256": hashlib.sha256(wide.encode()).hexdigest(),
        "patch": {
            "start_utf8_offset": wide.encode().index("∀".encode()) + 1,
            "end_utf8_offset": wide.encode().index("∀".encode()) + 2,
            "replacement": "",
        },
    }
    with pytest.raises(EditError, match="splits a character"):
        edited_source("suggest", split, wide)
    with pytest.raises(EditError, match="no edit"):
        edited_source("check", result, source)
    with pytest.raises(EditError, match="unchanged"):
        edited_source(
            "sorry2lemma",
            {
                "status": "extracted",
                "source_sha256": result["source_sha256"],
                "source_changed": False,
            },
            source,
        )
    with pytest.raises(EditError, match="did not complete"):
        edited_source(
            "sorry2lemma",
            {"status": "failed", "source_sha256": result["source_sha256"], "source_changed": True},
            source,
        )

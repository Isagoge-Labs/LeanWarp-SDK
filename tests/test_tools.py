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

import httpx
import pytest
from leanwarp_cloud import OperationOutcome
from leanwarp_cloud.config import load_client
from leanwarp_cloud.session import SessionError

from .test_session import Service

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
        assert json.loads(command("connect", "--bundle", "b"))["workspace_id"] == "w"
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
                assert {"connect", "verify_target", "recover", "stop"} <= {
                    t.name for t in catalog.tools
                }
                assert "api_key" not in json.dumps([t.inputSchema for t in catalog.tools])
                assert "max_spend" not in json.dumps([t.inputSchema for t in catalog.tools])
                assert "profile" not in json.dumps([t.inputSchema for t in catalog.tools])
                resources = await client.call_tool("resources", {})
                assert not resources.isError, resources
                assert json.loads(resources.content[0].text) == {"worker": {"usage_based": False}}
                connected = await client.call_tool("connect", {})
                assert not connected.isError, connected
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
        ("inspect", {"status": "proof_state"}, True),
        ("inspect", {"status": "metadata_only"}, True),
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
        lambda: LeanWarpCloud(
            TEST_KEY,
            base_url="https://api.example",
            transport=httpx.MockTransport(service.handle),
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

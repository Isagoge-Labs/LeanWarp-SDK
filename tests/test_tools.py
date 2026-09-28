from __future__ import annotations

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
                assert request.headers["Authorization"] == "Bearer test-tool-key"
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
    (root / "lake-manifest.json").write_text("{}")
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
            "LEANWARP_BASE_URL": origin,
            "LEANWARP_API_KEY": "test-tool-key",
        }

        def command(*args, expected=0):
            run = subprocess.run(  # noqa: S603 -- fixed module and test arguments
                [sys.executable, "-m", "leanwarp_cloud.cli", "--project", str(tmp_path), *args],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert "test-tool-key" not in run.stdout + run.stderr
            assert run.returncode == expected, run.stderr + run.stdout
            return run.stdout

        assert "name: leanwarp" in command("skill")
        assert json.loads(command("connect", "--max-spend", "2"))["workspace_id"] == "w"
        first = json.loads(command("check", "Main.lean", expected=1))
        assert first["kind"] == "check"
        assert json.loads(command("wait", expected=1))["operation_id"] == first["operation_id"]
        (tmp_path / "Main.lean").write_text("theorem candidate : True := True.intro\n")
        command("verify", "Main.lean", "--declaration", "candidate", "--target", "True", expected=1)
        assert service.workspace_count == 1
        assert service.revision == 2
        assert "True.intro" in service.sources["Main.lean"]
        body = json.loads(next(r.content for r in reversed(service.requests) if r.method == "POST"))
        assert body["payload"]["execution_mode"] == "reusable"
        assert json.loads(command("stop"))["state"] == "stopped"


def test_mcp_stdio_uses_same_project_session_without_key_arguments(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    project(tmp_path)
    with local_api() as (service, origin):

        async def exercise():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "leanwarp_cloud.cli", "--project", str(tmp_path), "mcp"],
                env={"LEANWARP_BASE_URL": origin, "LEANWARP_API_KEY": "test-tool-key"},
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                catalog = await client.list_tools()
                assert {"connect", "verify_target", "recover", "stop"} <= {
                    t.name for t in catalog.tools
                }
                assert "api_key" not in json.dumps([t.inputSchema for t in catalog.tools])
                connected = await client.call_tool("connect", {"max_spend_microusd": 2_000_000})
                assert not connected.isError, connected
                submitted = await client.call_tool("check", {"file": "Main.lean"})
                assert not submitted.isError, submitted
                result = await client.call_tool("wait", {"timeout": 1})
                assert not result.isError, result
                stopped = await client.call_tool("stop")
                assert not stopped.isError, stopped

        asyncio.run(exercise())
        assert service.workspace_count == 1
        assert (tmp_path / ".leanwarp" / "session.json").is_file()


def test_mcp_redacts_unexpected_transport_details(tmp_path):
    pytest.importorskip("mcp")
    from leanwarp_cloud import LeanWarpCloud
    from leanwarp_cloud.mcp_server import create_server

    def fail(_request):
        raise httpx.ConnectError("Authorization: Bearer should-never-leak")

    with LeanWarpCloud(
        "https://api.example", "should-never-leak", retries=0, transport=httpx.MockTransport(fail)
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


def test_credentials_reject_partial_environment_and_public_readable_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LEANWARP_BASE_URL", "https://api.example")
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    with pytest.raises(SessionError, match="both"):
        load_client()
    monkeypatch.delenv("LEANWARP_BASE_URL")
    path = tmp_path / "leanwarp" / "credentials.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"base_url": "https://api.example", "api_key": "test-tool-key"}))
    path.chmod(0o644)
    with pytest.raises(SessionError, match="only by its owner"):
        load_client()
    path.chmod(0o600)
    with load_client() as client:
        assert client.base_url == "https://api.example"

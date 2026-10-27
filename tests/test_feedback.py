from __future__ import annotations

import asyncio
import fcntl
import json
import os
from typing import Any

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, ProjectSession, UsageError

BASE = "https://api.example"
WORKSPACE = "4a1e7bb6-62a1-4f8e-9a8b-3c3f4f0b9d10"
OPERATION = "b8f0c3d2-1e5a-4c7b-8d9e-0f1a2b3c4d5e"


def recorder(requests: list[httpx.Request], response: httpx.Response | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if response is not None:
            return response
        return httpx.Response(201, json={"feedback_id": "f", "duplicate": False, "occurrences": 1})

    return LeanWarpCloud(
        "secret", base_url=BASE, transport=httpx.MockTransport(handle), interface="mcp"
    )


def connected(root) -> None:
    (root / ".leanwarp").mkdir(mode=0o700)
    (root / ".leanwarp" / "session.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "base_url": BASE,
                "owner_id": "owner",
                "environment": ["lean-4.26.0", "build"],
                "files": {},
                "pending": None,
                "workspace_id": WORKSPACE,
                "revision": 1,
                "operation_id": OPERATION,
                "operation_revision": 1,
            }
        )
    )


def test_feedback_names_the_latest_operation_even_while_another_command_runs(tmp_path):
    connected(tmp_path)
    requests: list[httpx.Request] = []
    with recorder(requests) as cloud:
        # Another command holds the project lock; a report never waits for it.
        fd = os.open(tmp_path / ".leanwarp" / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = ProjectSession(cloud, tmp_path).feedback(
                "bug",
                "  try_tactics lost\n the goal  " + "x" * 300,
                details="d" * 5000,
                severity="worked_around",
            )
        finally:
            os.close(fd)
    assert result["recorded"] is True and result["shortened"] is True
    assert result["operation_id"] == OPERATION
    (request,) = requests
    assert request.method == "POST" and request.url.path == "/v1/leanwarp/feedback"
    assert len(request.headers["Idempotency-Key"]) >= 8
    assert request.headers["User-Agent"].endswith("(mcp)")
    body = json.loads(request.content)
    assert body["workspace_id"] == WORKSPACE and body["operation_id"] == OPERATION
    assert body["summary"].startswith("try_tactics lost the goal x")
    assert len(body["summary"]) == 200 and body["summary"].endswith("…")
    assert len(body["details"]) == 4000
    assert body["kind"] == "bug" and body["severity"] == "worked_around"


def test_feedback_works_before_connecting_and_refuses_an_empty_summary(tmp_path):
    requests: list[httpx.Request] = []
    with recorder(requests) as cloud:
        session = ProjectSession(cloud, tmp_path)
        with pytest.raises(UsageError, match="at least 8"):
            session.feedback("docs", "  short ")
        result = session.feedback("docs", "The guide omits check-files limits", operation_id=None)
    assert requests and json.loads(requests[-1].content)["workspace_id"] is None
    assert result["operation_id"] is None and result["shortened"] is False


@pytest.mark.parametrize(
    "response,reason",
    [
        (
            httpx.Response(
                404, json={"error": {"code": "endpoint_not_found", "message": "No such"}}
            ),
            "endpoint_not_found",
        ),
        (
            httpx.Response(
                422,
                json={"error": {"code": "feedback_limit_exceeded", "message": "Too many"}},
            ),
            "feedback_limit_exceeded",
        ),
    ],
)
def test_mcp_feedback_reports_failure_without_raising(tmp_path, response, reason):
    from leanwarp_cloud.mcp_server import create_server

    connected(tmp_path)
    requests: list[httpx.Request] = []
    server = create_server(recorder(requests, response), str(tmp_path))

    async def call() -> Any:
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert tools["feedback"].annotations.destructiveHint is False
        schema = tools["feedback"].inputSchema
        # Nothing is required by validation, so no argument can make the tool raise;
        # the schema still tells the model what to send.
        assert "required" not in schema or not schema["required"]
        assert schema["properties"]["summary"]["type"] == "string"
        assert schema["properties"]["summary"]["description"].startswith("Required.")
        assert "bug" in schema["properties"]["kind"]["enum"]
        assert "never" in (tools["feedback"].description or "").lower()
        content, _ = await server.call_tool(
            "feedback", {"kind": "bug", "summary": "suggest accepted a sorry proof"}
        )
        return json.loads(content[0].text)

    assert asyncio.run(call()) == {"recorded": False, "reason": reason}
    # A rejected report is never retried.
    assert len(requests) == 1


def test_mcp_feedback_survives_transport_failure_and_a_missing_key(tmp_path):
    from leanwarp_cloud.mcp_server import create_server
    from leanwarp_cloud.session import SessionError

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    def not_signed_in() -> LeanWarpCloud:
        raise SessionError("not signed in; run `leanwarp auth login`")

    offline = LeanWarpCloud("secret", base_url=BASE, transport=httpx.MockTransport(unreachable))

    async def call(server: Any) -> Any:
        content, _ = await server.call_tool(
            "feedback", {"kind": "performance", "summary": "holes took four minutes"}
        )
        return json.loads(content[0].text)

    assert asyncio.run(call(create_server(offline, str(tmp_path))))["reason"] == "transport_error"
    assert "auth login" in asyncio.run(call(create_server(not_signed_in, str(tmp_path))))["reason"]


def test_cli_feedback_prints_the_receipt(tmp_path, monkeypatch, capsys):
    from leanwarp_cloud import cli

    connected(tmp_path)
    requests: list[httpx.Request] = []
    monkeypatch.setattr(cli, "load_client", lambda *, interface: recorder(requests))
    code = cli.main(
        [
            "--project",
            str(tmp_path),
            "feedback",
            "--kind",
            "missing_feature",
            "--summary",
            "No way to check a single declaration",
            "--operation",
            WORKSPACE,
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["recorded"] is True and printed["feedback_id"] == "f"
    assert json.loads(requests[-1].content)["operation_id"] == WORKSPACE
    with pytest.raises(SystemExit) as refused:
        cli.main(["feedback", "--kind", "praise", "--summary", "Great tool overall"])
    assert refused.value.code == 2


def test_feedback_is_sent_once_even_when_the_service_is_unavailable(tmp_path):
    requests: list[httpx.Request] = []
    busy = httpx.Response(503, json={"error": {"code": "feedback_unavailable", "message": "No"}})
    with recorder(requests, busy) as cloud, pytest.raises(Exception, match="No"):
        ProjectSession(cloud, tmp_path).feedback("bug", "holes reported a stale goal")
    assert len(requests) == 1


def test_mcp_feedback_maps_unlisted_values_and_drops_bad_identifiers(tmp_path):
    from leanwarp_cloud.mcp_server import create_server

    requests: list[httpx.Request] = []
    server = create_server(recorder(requests), str(tmp_path))

    async def call() -> Any:
        tools = {tool.name: tool for tool in await server.list_tools()}
        schema = tools["feedback"].inputSchema["properties"]
        assert "bug" in schema["kind"]["enum"] and "minor" in schema["severity"]["enum"]
        content, _ = await server.call_tool(
            "feedback",
            {
                "kind": "praise",
                "severity": "urgent",
                "summary": "suggest found nothing for omega goals",
                "operation_id": "not-an-id",
            },
        )
        return json.loads(content[0].text)

    assert asyncio.run(call())["recorded"] is True
    body = json.loads(requests[-1].content)
    assert body["kind"] == "other" and body["severity"] == "minor"
    assert body["operation_id"] is None


@pytest.mark.parametrize(
    "arguments",
    [{}, {"summary": None}, {"kind": "bug"}, {"summary": 123}, {"kind": 5, "summary": ["x"]}],
)
def test_mcp_feedback_answers_any_arguments_without_raising(tmp_path, arguments):
    from leanwarp_cloud.mcp_server import create_server

    requests: list[httpx.Request] = []
    server = create_server(recorder(requests), str(tmp_path))

    async def call() -> Any:
        content, _ = await server.call_tool("feedback", arguments)
        return json.loads(content[0].text)

    assert asyncio.run(call()) == {"recorded": False, "reason": "a one-line summary is required"}
    assert requests == []


@pytest.mark.parametrize(
    "kind,severity",
    [(["bug"], "minor"), ({"kind": "bug"}, "minor"), ("bug", ["blocking"]), ("bug", {"x": 1})],
)
def test_mcp_feedback_maps_list_and_object_kinds_and_severities(tmp_path, kind, severity):
    from leanwarp_cloud.mcp_server import create_server

    requests: list[httpx.Request] = []
    server = create_server(recorder(requests), str(tmp_path))

    async def call() -> Any:
        content, _ = await server.call_tool(
            "feedback",
            {"kind": kind, "severity": severity, "summary": "holes reported a stale goal"},
        )
        return json.loads(content[0].text)

    assert asyncio.run(call())["recorded"] is True
    body = json.loads(requests[-1].content)
    assert body["kind"] in {"bug", "other"} and body["severity"] in {"minor", "blocking"}
    assert body["kind"] == ("bug" if kind == "bug" else "other")
    assert body["severity"] == "minor"

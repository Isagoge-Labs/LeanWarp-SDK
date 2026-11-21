"""MCP tools are safe to call in parallel and cheap to read."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud
from leanwarp_cloud.compact import compact
from leanwarp_cloud.mcp_server import create_server

from .test_session import Service


@pytest.fixture
def served(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = Service()
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        # Looked up on each request, so a test can replace the service's handler.
        transport=httpx.MockTransport(lambda request: service.handle(request)),
        interface="mcp",
    ) as cloud:
        yield service, create_server(cloud, str(tmp_path))


def call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        content, _ = await server.call_tool(name, arguments or {})
        return json.loads(content[0].text)

    return asyncio.run(run())


def test_parallel_tool_calls_take_turns_instead_of_failing(served):
    _, server = served
    call(server, "connect")

    async def together() -> list[Any]:
        calls = [
            server.call_tool("check", {"file": "Main.lean"}),
            server.call_tool("status", {}),
            server.call_tool("files", {}),
            server.call_tool("status", {}),
        ]
        results = await asyncio.gather(*calls, return_exceptions=True)
        # `wait` without an id waits on the latest operation; in parallel with the
        # submission that may not exist yet, so it is called once one does.
        results.append(await server.call_tool("wait", {"timeout": 5}))
        return results

    results = asyncio.run(together())
    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures == []


OPERATION_TOOLS = {
    "check": {"file": "Main.lean"},
    "inspect": {"file": "Main.lean", "line": 1, "column": 30},
    "try_tactics": {"file": "Main.lean", "line": 1, "column": 30, "tactics": ["simp"]},
    "verify_target": {
        "file": "Main.lean",
        "candidate_declaration": "candidate",
        "target_statement": "True",
    },
    "suggest": {"file": "Main.lean", "line": 1, "column": 30},
    "holes": {"file": "Main.lean"},
    "extract_decls": {"file": "Main.lean"},
    "sorry2lemma": {"file": "Main.lean"},
    "have2lemma": {"file": "Main.lean"},
    "simplify_proofs": {"file": "Main.lean"},
    "repair_proofs": {"file": "Main.lean", "tactics": ["simp"]},
    "check_files": {"files": ["Main.lean"]},
    "verify_batch": {"codes": [{"custom_id": "a", "proof": "example : True := trivial"}]},
}


@pytest.mark.parametrize("tool", sorted(OPERATION_TOOLS))
def test_every_operation_tool_returns_its_operation_id(served, tool):
    service, server = served
    call(server, "connect")
    result = call(server, tool, OPERATION_TOOLS[tool])
    assert result["operation_id"] == service.operation["operation_id"]
    assert "success" in result and result["kind"] == tool
    if tool in {"check", "inspect", "try_tactics"}:
        call(server, tool, {**OPERATION_TOOLS[tool], "retention": "short"})
        sent = next(request for request in reversed(service.requests) if request.method == "POST")
        assert json.loads(sent.content)["retention"] == "short"


def test_wait_status_and_cancel_accept_an_operation_id(served):
    service, server = served
    call(server, "connect")
    first = call(server, "check", {"file": "Main.lean"})["operation_id"]
    second = call(server, "holes", {"file": "Main.lean"})["operation_id"]
    assert first != second
    service.operation = {**service.operation, "operation_id": first, "kind": "check"}
    assert call(server, "wait", {"operation_id": first})["operation_id"] == first
    assert call(server, "status", {"operation_id": first})["operation"]["operation_id"] == first
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}
    for name in ("wait", "status", "cancel"):
        assert "operation_id" in schemas[name]["properties"]
        assert "operation_id" not in schemas[name].get("required", [])


def test_recover_returns_the_full_operation_on_request(served, tmp_path):
    service, server = served
    service.result_for = noisy
    call(server, "connect")
    for detail, expect_full in (("full", True), ("compact", False)):
        (tmp_path / "Main.lean").write_text(f"theorem t : True := by {detail}\n")
        service.lose = "/run"
        with pytest.raises(Exception, match="transport_error"):
            call(server, "check", {"file": "Main.lean"})
        recovered = call(server, "recover", {"detail": detail})
        assert ("omitted" not in recovered) is expect_full
        if expect_full:
            assert len(recovered["result"]["result"]["diagnostics"]) == 302


def test_cancel_returns_the_full_operation_on_request(served):
    service, server = served
    service.result_for = noisy
    call(server, "connect")
    call(server, "check", {"file": "Main.lean"})
    handle = service.handle

    def cancelling(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json=service.operation)
        return handle(request)

    service.handle = cancelling  # type: ignore[method-assign]
    assert len(call(server, "cancel", {"detail": "full"})["result"]["result"]["diagnostics"]) == 302
    assert "omitted" in call(server, "cancel")


def test_status_never_embeds_a_result(served):
    service, server = served
    service.result_for = lambda body: {"status": "error", "diagnostics": [{"severity": "error"}]}
    call(server, "connect")
    call(server, "check", {"file": "Main.lean"})
    status = call(server, "status")
    assert set(status["operation"]) <= {
        "success",
        "decided",
        "operation_id",
        "kind",
        "state",
        "revision",
        "error_code",
        "error_message",
    }
    assert status["operation"]["success"] is False


def test_wait_is_capped_for_mcp_clients(served):
    _, server = served
    call(server, "connect")
    call(server, "check", {"file": "Main.lean"})
    with pytest.raises(Exception, match="at most 40 seconds"):
        call(server, "wait", {"timeout": 41})


def test_wait_returns_a_running_operation_instead_of_failing(served):
    service, server = served
    call(server, "connect")
    service.pending_polls = 1_000
    submitted = call(server, "check", {"file": "Main.lean", "wait_seconds": 0})
    waited = call(server, "wait", {"timeout": 0.2})
    assert waited["success"] is None and waited["operation_id"] == submitted["operation_id"]


def noisy(body: dict[str, Any]) -> dict[str, Any]:
    warnings = [{"severity": "warning", "message": "unused variable " * 40}] * 300
    errors = [{"severity": "error", "message": f"type mismatch {n}"} for n in range(2)]
    return {"status": "error", "diagnostics": warnings + errors}


def test_results_are_compact_by_default_and_full_on_request(served):
    service, server = served
    service.result_for = noisy
    call(server, "connect")
    short = call(server, "check", {"file": "Main.lean"})
    assert len(json.dumps(short)) <= 16 * 1024
    diagnostics = short["result"]["diagnostics"]
    assert [item["severity"] for item in diagnostics[:2]] == ["error", "error"]
    assert short["counts"] == {"warning": 300, "error": 2}
    assert short["omitted"]["result.diagnostics"] == 302 - len(diagnostics)
    assert short["operation_id"] and short["success"] is False
    full = call(server, "wait", {"detail": "full"})
    assert len(full["result"]["result"]["diagnostics"]) == 302
    goals = [
        {"target": "∀ x, x = x", "hypotheses": ["h : β"] * 25},
        {"target": "β", "hypotheses": []},
    ]
    result = {
        "status": "metadata_only",
        "goal_status": "available",
        "goal_contexts": goals,
        "selected_tactic": {"states": [{"side": "before", "source_range": None}]},
    }
    service.result_for = lambda _: result
    inspect = {"file": "Main.lean", "line": 1, "column": 1}
    assert call(server, "inspect", inspect)["result"] == result
    goals[1]["target"] = "β" * 9000
    hidden = call(server, "inspect", inspect)
    assert (
        hidden["success"]
        and "result" not in hidden
        and hidden["omitted"]["result.goal_contexts"] == 2
    )
    assert call(server, "wait", {"detail": "full"})["result"]["result"] == result


def operation(result: dict[str, Any], kind: str = "check") -> dict[str, Any]:
    return {
        "success": False,
        "operation_id": "o",
        "kind": kind,
        "state": "completed",
        "revision": 1,
        "workspace_id": "w",
        "result": {"operation_id": "o", "revision": 1, "generation": 1, "result": result},
        "timing": {"run_ms": 5},
    }


def test_a_shortened_batch_keeps_its_failing_entries():
    results = [{"file": f"F{n}.lean", "status": "ok", "diagnostics": []} for n in range(64)]
    results[40] = {"file": "F40.lean", "status": "error", "diagnostics": [{"severity": "error"}]}
    shown = compact(operation({"status": "failed", "passed": 63, "failed": 1, "results": results}))
    assert shown["result"]["results"][0]["file"] == "F40.lean"
    assert shown["result"]["failed"] == 1 and shown["omitted"]["result.results"] == 44


def test_a_shortened_trial_list_keeps_the_tactic_that_closed_the_goal():
    trials = [{"tactic": f"t{n}", "outcome": "tactic_error"} for n in range(31)]
    trials.append({"tactic": "omega", "outcome": "closed_proof"})
    shown = compact(operation({"results": trials}, "try_tactics"))
    assert shown["result"]["results"][0]["tactic"] == "omega"


def test_short_results_are_unchanged_but_for_the_envelope():
    result = {"status": "ok", "diagnostics": [], "cached": True}
    assert compact(operation(result)) == {
        "success": False,
        "operation_id": "o",
        "kind": "check",
        "state": "completed",
        "revision": 1,
        "result": result,
    }


def test_a_result_too_large_for_any_shortening_keeps_its_scalars():
    result = {"status": "ok", **{f"field{n}": {"text": "x" * 500} for n in range(500)}}
    shown = compact(operation(result))
    assert shown["result"] == {"status": "ok"}
    assert len(shown["omitted"]) == 500
    assert len(json.dumps(shown)) <= 16 * 1024


def test_a_result_too_large_for_any_shortening_keeps_its_failure():
    # A non-verdict must never read as a verdict, however much is cut.
    failure = {"kind": "internal", "retryable": True, "message": "Retry it."}
    result = {"status": "infrastructure_failure", "failure": failure}
    result.update({f"field{n}": {"text": "x" * 500} for n in range(500)})
    shown = compact(operation(result))
    assert shown["result"] == {"status": "infrastructure_failure", "failure": failure}


def test_the_compact_form_says_whether_the_work_was_decided():
    from leanwarp_cloud.outcome import reported

    undecided = reported(operation({"status": "timeout", "failure": {"kind": "timeout"}}))
    shown = compact(undecided)
    assert (shown["success"], shown["decided"]) == (False, False)
    rejection = {"status": "rejected", "failure": {"kind": "axioms"}, "diagnostics": []}
    refused = compact(reported(operation(rejection)))
    assert (refused["success"], refused["decided"]) == (False, True)

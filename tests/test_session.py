from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from leanwarp_cloud import (
    LeanWarpCloud,
    LeanWarpCloudError,
    OperationOutcome,
    ProjectSession,
    SessionError,
)
from leanwarp_cloud import session as session_module
from leanwarp_cloud.project import MAX_UPLOAD_BYTES, ProjectError
from leanwarp_cloud.session import UsageError


class Service:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.receipts: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self.sources: dict[str, str] = {}
        self.revision = 0
        self.operation: dict[str, Any] = {}
        self.lose = ""
        self.reject = False
        self.workspace_count = 0
        self.max_spend_microusd: int | None = None
        # Operation reads before a submitted operation completes; zero completes on submit.
        self.pending_polls = 0
        self.fail_operation_reads = 0
        self._completion: dict[str, Any] = {}
        # The result a submitted operation completes with, built from its request.
        self.result_for: Any = lambda body: {"status": "rejected"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/account"):
            return httpx.Response(200, json={"owner_id": "owner-a"})
        if path.endswith("/resources"):
            return httpx.Response(200, json={"resources": [{"name": "standard"}]})
        if path.endswith("/versions"):
            return httpx.Response(
                200,
                json={
                    "versions": [
                        {
                            "bundle_id": "b",
                            "lean_toolchain": "leanprover/lean4:v4.26.0",
                            "lake_manifest_sha256": hashlib.sha256(
                                b'{"version":"1.1.0","packages":[]}'
                            ).hexdigest(),
                        }
                    ]
                },
            )
        if request.method == "GET":
            if "/operations/" in path and self.fail_operation_reads:
                self.fail_operation_reads -= 1
                raise httpx.ConnectError("connection reset", request=request)
            if "/operations/" in path and self.pending_polls:
                self.pending_polls -= 1
                if not self.pending_polls:
                    self.operation = self._completion
            value = (
                self.operation
                if "/operations/" in path
                else {
                    "workspace_id": "w",
                    "revision": self.revision,
                    "max_spend_microusd": self.max_spend_microusd,
                }
            )
            return httpx.Response(200, json=value)
        key = request.headers.get("Idempotency-Key", "")
        if key in self.receipts:
            body, result = self.receipts[key]
            assert body == request.content
            return httpx.Response(200, json=result)
        body = json.loads(request.content)
        if self.reject:
            self.reject = False
            return httpx.Response(409, json={"error": {"code": "revision_conflict"}})
        if path.endswith("/workspaces"):
            self.workspace_count += 1
            self.max_spend_microusd = body.get("max_spend_microusd")
            result = {"workspace_id": "w", "revision": 0}
        elif path.endswith("/files"):
            assert body["expected_revision"] == self.revision
            self.sources.update(body["files"])
            for name in body["delete_paths"]:
                del self.sources[name]
            self.revision += 1
            result = {"workspace_id": "w", "revision": self.revision}
        else:
            assert path.endswith("/operations")
            assert body["expected_revision"] == self.revision
            self.operation = {
                "operation_id": f"op-{len(self.receipts)}",
                "workspace_id": "w",
                "kind": body["kind"],
                "state": "completed",
                "revision": self.revision,
                "result": {
                    "operation_id": f"op-{len(self.receipts)}",
                    "revision": self.revision,
                    "generation": 1,
                    "result": self.result_for(body),
                },
            }
            if self.pending_polls:
                self._completion = self.operation
                self.operation = {**self.operation, "state": "queued", "result": None}
            result = self.operation
        self.receipts[key] = request.content, result
        if self.lose and path.endswith(self.lose):
            self.lose = ""
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=result)


@pytest.fixture
def project(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = Service()
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(service.handle),
    ) as cloud:
        yield tmp_path, service, cloud, ProjectSession(cloud, tmp_path)


@pytest.mark.parametrize("kind", ["workspaces", "files", "operations"])
def test_lost_response_recovers_exact_intent_after_restart(project, kind):
    root, service, cloud, session = project
    if kind != "workspaces":
        session.connect()
    if kind == "operations":
        session.sync()
    service.lose = "/" + kind
    with pytest.raises(httpx.ReadTimeout):
        if kind == "workspaces":
            session.connect()
        elif kind == "files":
            session.sync()
        else:
            session.submit(
                "verify_target",
                {
                    "file": "Main.lean",
                    "candidate_declaration": "candidate",
                    "target_statement": "True",
                },
            )
    journal = json.loads((root / ".leanwarp/session.json").read_text())
    assert journal["pending"] and "secret" not in json.dumps(journal)
    (root / "Main.lean").write_text("theorem candidate : False := by sorry\n")
    resumed = ProjectSession(cloud, root)
    with pytest.raises(SessionError, match="recover"):
        resumed.submit("check", {"file": "Main.lean"})
    count = len(service.receipts)
    resumed.recover()
    assert len(service.receipts) == count
    assert service.workspace_count == 1
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None
    if kind == "files":
        assert "True" in service.sources["Main.lean"]
        resumed.sync()
        assert "False" in service.sources["Main.lean"]


def test_changed_sources_deletions_and_proof_rejection_preserve_workspace(project):
    root, service, _, session = project
    session.connect()
    (root / "Helper.lean").write_text("def n := 1")
    session.submit("check", {"file": "Main.lean"})
    revision = service.revision
    session.sync()
    assert service.revision == revision
    (root / "Helper.lean").unlink()
    (root / "Main.lean").write_text("theorem candidate : False := by sorry")
    result = session.submit(
        "verify_target",
        {"file": "Main.lean", "candidate_declaration": "candidate", "target_statement": "True"},
    )
    assert not OperationOutcome(result).verified
    assert "Helper.lean" not in service.sources
    assert service.workspace_count == 1
    assert service.revision == revision + 1


def test_pending_request_owns_nested_payload_and_recovers_exact_snapshot(project, monkeypatch):
    root, service, cloud, session = project
    session.connect()
    session.sync()
    tactics = ["simp"]
    submit = cloud.submit

    def mutate_caller_then_submit(**arguments):
        tactics.append("omega")
        return submit(**arguments)

    monkeypatch.setattr(cloud, "submit", mutate_caller_then_submit)
    service.lose = "/operations"
    with pytest.raises(httpx.ReadTimeout):
        session.submit("try_tactics", {"file": "Main.lean", "tactics": tactics})
    pending = json.loads((root / ".leanwarp/session.json").read_text())["pending"]
    assert pending["arguments"]["payload"]["tactics"] == ["simp"]
    wire, _ = service.receipts[pending["idempotency_key"]]
    assert json.loads(wire)["payload"] == pending["arguments"]["payload"]
    count = len(service.receipts)
    ProjectSession(cloud, root).recover()
    assert len(service.receipts) == count
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None


def test_definite_rejection_does_not_poison_next_request(project):
    _, service, _, session = project
    session.connect()
    service.reject = True
    with pytest.raises(LeanWarpCloudError):
        session.sync()
    session.sync()
    assert service.revision == 1


def test_lock_prevents_two_local_clients_from_mutating(project):
    root, service, cloud, session = project
    session.connect()
    with session._locked(), pytest.raises(SessionError, match="another"):
        ProjectSession(cloud, root).sync()
    assert service.revision == 0


def test_changed_environment_never_silently_upgrades(project):
    root, _, _, session = project
    session.connect()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.27.0")
    with pytest.raises(SessionError, match="toolchain or lockfile changed"):
        session.sync()


def test_connected_project_refuses_a_silent_environment_switch(project):
    _, service, _, session = project
    session.connect()
    with pytest.raises(SessionError, match="disconnect"):
        session.connect(environment="lean-4.34-mathlib")
    assert service.workspace_count == 1


def test_malformed_journal_is_not_overwritten(project):
    root, _, _, session = project
    session.connect()
    journal = root / ".leanwarp/session.json"
    journal.write_text("{broken")
    with pytest.raises(SessionError, match="damaged"):
        session.sync()
    assert journal.read_text() == "{broken"


def test_proof_acceptance_requires_matching_runtime_receipt():
    op = {
        "operation_id": "op",
        "revision": 2,
        "state": "completed",
        "kind": "verify_target",
        "result": {
            "operation_id": "op",
            "revision": 2,
            "generation": 1,
            "result": {"status": "ok"},
        },
    }
    assert not OperationOutcome(op).verified
    op["result"]["result"]["receipt"] = {"policy": "fixed_target_kernel_check_v1"}
    assert OperationOutcome(op).verified
    op["result"]["revision"] = 1
    assert not OperationOutcome(op).verified
    op["result"]["revision"] = 2
    op["state"] = "cancelled"
    assert not OperationOutcome(op).verified


def test_invalid_timeout_never_persists_an_unsendable_request(project):
    root, _, _, session = project
    session.connect()
    with pytest.raises(ValueError):
        session.submit("check", {"file": "Main.lean"}, timeout_seconds=0)
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None
    session.submit("check", {"file": "Main.lean"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", None),
        ("operation_id", ""),
        ("operation_id", " "),
        ("revision", None),
        ("revision", True),
        ("revision", -1),
        ("revision", "2"),
    ],
)
def test_missing_or_malformed_identity_never_verifies(field, value):
    op = {
        "operation_id": "op",
        "revision": 2,
        "state": "completed",
        "kind": "verify_target",
        "result": {
            "operation_id": "op",
            "revision": 2,
            "generation": 1,
            "result": {"status": "ok", "receipt": {"policy": "fixed_target_kernel_check_v1"}},
        },
    }
    op[field] = value
    op["result"][field] = value
    assert not OperationOutcome(op).verified
    del op[field]
    del op["result"][field]
    assert not OperationOutcome(op).verified


def test_boolean_envelope_revision_does_not_match_integer_revision():
    op = {
        "operation_id": "op",
        "revision": 1,
        "state": "completed",
        "kind": "verify_target",
        "result": {
            "operation_id": "op",
            "revision": True,
            "generation": 1,
            "result": {"status": "ok", "receipt": {"policy": "fixed_target_kernel_check_v1"}},
        },
    }
    assert not OperationOutcome(op).verified


@pytest.mark.parametrize("command", ["wait", "status", "cancel"])
def test_saved_submission_revision_rejects_a_coherently_wrong_receipt(
    project, monkeypatch, command
):
    root, service, cloud, session = project
    session.connect()
    submitted = session.submit("verify_target", {"file": "Main.lean"})
    journal = root / ".leanwarp/session.json"
    saved = journal.read_bytes()
    assert json.loads(saved)["operation_revision"] == submitted["revision"]
    service.operation["revision"] = 999
    service.operation["result"]["revision"] = 999
    monkeypatch.setattr(cloud, "cancel", lambda _operation: service.operation)
    restarted = ProjectSession(cloud, root)
    with pytest.raises(SessionError, match="different operation"):
        getattr(restarted, command)()
    assert journal.read_bytes() == saved


def test_operation_revision_survives_later_sync_and_legacy_journals_still_read(project):
    root, _, cloud, session = project
    session.connect()
    submitted = session.submit("check", {"file": "Main.lean"})
    (root / "Main.lean").write_text("theorem changed : True := by trivial\n")
    synced = session.sync()
    assert synced["revision"] > submitted["revision"]
    assert ProjectSession(cloud, root).wait()["revision"] == submitted["revision"]
    journal = root / ".leanwarp/session.json"
    previous = json.loads(journal.read_text())
    del previous["operation_revision"]
    journal.write_text(json.dumps(previous))
    assert ProjectSession(cloud, root).wait()["operation_id"] == submitted["operation_id"]


def test_submit_and_wait_returns_the_result_that_completes_within_the_wait(project, monkeypatch):
    _, service, _, session = project
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", lambda _seconds: None)
    session.connect()
    service.pending_polls = 3
    result = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=30)
    assert result["state"] == "completed"
    assert OperationOutcome(result).result == {"status": "rejected"}
    assert service.pending_polls == 0


def test_submit_and_wait_returns_running_work_without_losing_the_submission(project):
    root, service, cloud, session = project
    session.connect()
    service.pending_polls = 1_000
    pending = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=0.05)
    assert pending["state"] == "queued" and pending["result"] is None
    submissions = [r for r in service.requests if r.url.path.endswith("/operations")]
    assert len(submissions) == 1
    service.pending_polls = 1
    completed = ProjectSession(cloud, root).wait(timeout=1)
    assert completed["operation_id"] == pending["operation_id"]
    assert completed["state"] == "completed"


def test_failed_poll_after_submission_reports_running_work_not_a_failed_submit(project):
    root, service, cloud, session = project
    session.connect()
    service.pending_polls = 1_000
    service.fail_operation_reads = 1
    pending = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=5)
    assert pending["state"] == "queued"
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None
    service.pending_polls = 1
    assert ProjectSession(cloud, root).wait(timeout=1)["state"] == "completed"


def test_inline_wait_does_not_contend_for_the_project_lock(project, monkeypatch):
    _, service, _, session = project
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", lambda _seconds: None)
    session.connect()
    service.pending_polls = 2
    account_reads = sum(r.url.path.endswith("/account") for r in service.requests)
    result = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=5)
    assert result["state"] == "completed"
    # Only the submission itself takes the lock and re-reads the account owner.
    assert sum(r.url.path.endswith("/account") for r in service.requests) == account_reads + 1


@pytest.mark.parametrize("stall", ["read", "connect"])
def test_inline_wait_returns_within_its_budget_when_polling_stalls(tmp_path, monkeypatch, stall):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = Service()
    clock = [1000.0]
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "leanwarp_cloud.client.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    polls: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/operations/" in request.url.path:
            # A stalled dependency: nothing arrives until this phase's timeout.
            polls.append(request.extensions["timeout"][stall])
            clock[0] += request.extensions["timeout"][stall]
            error = httpx.ReadTimeout if stall == "read" else httpx.ConnectTimeout
            raise error("stalled", request=request)
        return service.handle(request)

    # The production retry policy, not the fixture's zero-retry client.
    with LeanWarpCloud(
        "secret", base_url="https://api.example", transport=httpx.MockTransport(handle)
    ) as cloud:
        session = ProjectSession(cloud, tmp_path)
        session.connect()
        service.pending_polls = 1_000
        started = clock[0]
        pending = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=20)
    assert clock[0] - started <= 20
    assert polls and all(timeout < 20 for timeout in polls)
    assert pending["state"] == "queued"
    assert (
        pending["operation_id"]
        == json.loads((tmp_path / ".leanwarp/session.json").read_text())["operation_id"]
    )


def test_zero_wait_submits_without_polling(project):
    _, service, _, session = project
    session.connect()
    service.pending_polls = 1_000
    reads_before = len(service.requests)
    pending = session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=0)
    assert pending["state"] == "queued"
    operation_reads = [
        r
        for r in service.requests[reads_before:]
        if r.method == "GET" and "/operations/" in r.url.path
    ]
    assert operation_reads == []


@pytest.mark.parametrize("wait_seconds", [-1, 40.5, True, float("nan")])
def test_invalid_inline_wait_is_rejected_before_any_submission(project, wait_seconds):
    root, service, _, session = project
    session.connect()
    with pytest.raises(ValueError, match="inline wait"):
        session.submit_and_wait("check", {"file": "Main.lean"}, wait_seconds=wait_seconds)
    assert not any(r.url.path.endswith("/operations") for r in service.requests)
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None


def test_wrong_account_cannot_recover_a_pending_create(project):
    root, service, _, session = project
    service.lose = "/workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect()
    before = (root / ".leanwarp/session.json").read_bytes()

    def other_account(request):
        assert request.url.path.endswith("/account")
        return httpx.Response(200, json={"owner_id": "owner-b"})

    with (
        LeanWarpCloud(
            "other-key",
            base_url="https://api.example",
            transport=httpx.MockTransport(other_account),
        ) as other,
        pytest.raises(SessionError, match="another account"),
    ):
        ProjectSession(other, root).recover()
    assert (root / ".leanwarp/session.json").read_bytes() == before


@pytest.mark.parametrize(
    "field,value", [("revision", None), ("files", None), ("environment", []), ("owner_id", None)]
)
def test_invalid_journal_fields_never_reach_network(project, field, value):
    root, service, _, session = project
    session.connect()
    path = root / ".leanwarp/session.json"
    state = json.loads(path.read_text())
    state[field] = value
    path.write_text(json.dumps(state))
    count = len(service.requests)
    with pytest.raises(SessionError, match="damaged"):
        session.recover()
    assert len(service.requests) == count


@pytest.mark.parametrize(
    "receipt",
    [
        {"workspace_id": "w", "revision": None},
        {"workspace_id": "w", "revision": 2**31},
        {"workspace_id": "x" * 257, "revision": 0},
    ],
)
def test_invalid_success_receipt_retains_recoverable_creation(project, monkeypatch, receipt):
    root, _, cloud, session = project
    original = cloud._request

    def malformed(method, route, **kwargs):
        return (
            receipt
            if method == "POST" and route == "workspaces"
            else original(method, route, **kwargs)
        )

    monkeypatch.setattr(cloud, "_request", malformed)
    with pytest.raises(SessionError, match="response was incomplete"):
        session.connect()
    state = json.loads((root / ".leanwarp" / "session.json").read_text())
    assert state["pending"] is not None
    assert "workspace_id" not in state
    monkeypatch.setattr(cloud, "_request", original)
    assert ProjectSession(cloud, root).recover()["workspace_id"] == "w"


def test_unhashable_journal_method_fails_closed_with_actionable_error(project):
    root, _, cloud, session = project
    session.connect()
    path = root / ".leanwarp" / "session.json"
    state = json.loads(path.read_text())
    state["pending"] = {"method": [], "arguments": {}, "idempotency_key": "a" * 32, "hashes": None}
    path.write_text(json.dumps(state))
    with pytest.raises(SessionError, match="damaged"):
        ProjectSession(cloud, root).recover()
    assert json.loads(path.read_text()) == state


def test_disconnect_requires_confirmed_stop_and_allows_new_dependencies(project, monkeypatch):
    _, _, cloud, session = project
    session.connect()
    with pytest.raises(SessionError, match="stop"):
        session.disconnect()
    monkeypatch.setattr(cloud, "workspace", lambda _: {"workspace_id": "w", "state": "stopped"})
    monkeypatch.setattr(cloud, "stop", lambda _: {"workspace_id": "w", "state": "stopped"})
    assert session.disconnect()["workspace_id"] == "w"
    assert session.connect()["workspace_id"] == "w"


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_disconnect_preserves_nonterminal_operation_on_stopped_workspace(
    project, monkeypatch, state
):
    _, service, cloud, session = project
    session.connect()
    session.submit("check", {"file": "Main.lean"})
    service.operation["state"] = state
    monkeypatch.setattr(cloud, "workspace", lambda _: {"workspace_id": "w", "state": "stopped"})
    before = session.path.read_bytes()
    with pytest.raises(SessionError, match="still running"):
        session.disconnect()
    assert session.path.read_bytes() == before


def test_disconnect_retains_connection_when_server_has_other_device_work(project, monkeypatch):
    _, _, cloud, session = project
    session.connect()
    monkeypatch.setattr(cloud, "workspace", lambda _: {"workspace_id": "w", "state": "stopped"})

    def busy(_):
        raise LeanWarpCloudError(409, "workspace_busy", "operation is active")

    monkeypatch.setattr(cloud, "stop", busy)
    before = session.path.read_bytes()
    with pytest.raises(LeanWarpCloudError, match="active"):
        session.disconnect()
    assert session.path.read_bytes() == before


@pytest.mark.parametrize(
    "kind,payload,error",
    [
        # A CLI command name is not an operation kind; nothing is journaled or sent.
        ("verify", {"file": "Main.lean"}, UsageError),
        ("check", {"file": "Main.lean", "oversized": "x" * (8 * 1024 * 1024)}, SessionError),
    ],
)
def test_invalid_submission_preserves_readable_journal_without_sending(
    project, kind, payload, error
):
    root, service, cloud, session = project
    session.connect()
    session.sync()
    journal = session.path.read_bytes()
    service.lose = "/operations"
    with pytest.raises(error):
        session.submit(kind, payload)
    assert session.path.read_bytes() == journal
    assert not any(request.url.path.endswith("/operations") for request in service.requests)
    recovered = ProjectSession(cloud, root)
    recovered.status()
    service.lose = ""
    assert recovered.submit("check", {"file": "Main.lean"})["state"] == "completed"


@pytest.mark.parametrize("field", ["resource_profile", "max_resource_profile"])
@pytest.mark.parametrize("value", ["", "x" * 257])
def test_invalid_connect_profile_does_not_poison_first_connection(project, field, value):
    root, service, cloud, session = project
    with pytest.raises(SessionError):
        session.connect(**{field: value})
    assert not session.path.exists()
    assert service.workspace_count == 0
    assert ProjectSession(cloud, root).connect()["workspace_id"] == "w"


@pytest.mark.parametrize("cap", [None, 0, 5_000_000])
def test_recovery_preserves_nullable_and_legacy_creation_payloads(project, cap):
    root, service, cloud, session = project
    arguments = {
        "bundle_id": "b",
        "resource_profile": "standard",
        "max_resource_profile": "standard",
        "max_spend_microusd": cap,
    }
    key = "b" * 32
    service.lose = "workspaces"
    with pytest.raises(httpx.ReadTimeout):
        cloud._request("POST", "workspaces", body=arguments, idempotency_key=key)
    session.directory.mkdir()
    session.path.write_text(
        json.dumps(
            {
                "schema": 1,
                "base_url": cloud.base_url,
                "owner_id": "owner-a",
                "files": {},
                "environment": [
                    "leanprover/lean4:v4.26.0",
                    hashlib.sha256(b'{"version":"1.1.0","packages":[]}').hexdigest(),
                ],
                "pending": {
                    "method": "create_workspace",
                    "arguments": arguments,
                    "idempotency_key": key,
                    "hashes": None,
                },
            }
        )
    )
    assert ProjectSession(cloud, root).recover()["workspace_id"] == "w"
    assert service.workspace_count == 1
    posts = [r for r in service.requests if r.method == "POST"]
    assert len(posts) == 2 and posts[0].content == posts[1].content
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]


def test_new_connection_explicitly_selects_prepaid_policy(project):
    _, service, _, session = project
    session.connect()
    body = json.loads(next(r.content for r in service.requests if r.method == "POST"))
    assert body["max_spend_microusd"] is None
    assert "resource_profile" not in body
    assert "max_resource_profile" not in body


@pytest.mark.parametrize("cap", [0, 2_000_001, 10**12])
def test_capped_connection_recovers_exact_creation_intent(project, cap):
    root, service, cloud, session = project
    service.lose = "/workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect(max_spend_microusd=cap)
    journal = json.loads(session.path.read_text())
    assert journal["pending"]["arguments"]["max_spend_microusd"] == cap
    resumed = ProjectSession(cloud, root)
    resumed.recover()
    assert resumed.connect(max_spend_microusd=cap)["max_spend_microusd"] == cap
    assert resumed.connect()["max_spend_microusd"] == cap
    assert service.workspace_count == 1
    posts = [r for r in service.requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]


@pytest.mark.parametrize("saved, requested", [(None, 0), (2_000_000, 0), (2_000_000, 3_000_000)])
def test_reconnect_rejects_conflicting_cap_without_mutation(project, saved, requested):
    _, service, _, session = project
    session.connect(max_spend_microusd=saved)
    journal = session.path.read_bytes()
    with pytest.raises(SessionError, match="different spending cap"):
        session.connect(max_spend_microusd=requested)
    assert session.path.read_bytes() == journal
    assert service.workspace_count == 1
    assert service.max_spend_microusd == saved


@pytest.mark.parametrize("cap", [-1, 10**12 + 1, True, 1.5, "2000000"])
def test_invalid_cap_starts_no_request_and_creates_no_journal(project, cap):
    _, service, _, session = project
    with pytest.raises(ValueError, match="max_spend_microusd"):
        session.connect(max_spend_microusd=cap)
    assert service.requests == []
    assert not session.directory.exists()


def test_recovery_keeps_server_default_selection_omitted(project):
    root, service, _, session = project
    service.lose = "workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect()
    assert ProjectSession(session.cloud, root).recover()["workspace_id"] == "w"
    posts = [r for r in service.requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]
    assert "resource_profile" not in json.loads(posts[0].content)


def test_connection_keeps_explicit_profile_selection(project):
    _, service, _, session = project
    session.connect(resource_profile="standard", max_resource_profile="large")
    body = json.loads(next(r.content for r in service.requests if r.method == "POST"))
    assert body["resource_profile"] == "standard"
    assert body["max_resource_profile"] == "large"


def test_connected_project_survives_root_rename_and_formatting(project):
    root, _, _, session = project
    before = session.connect()
    (root / "lake-manifest.json").write_text(
        '{ "name": "my_research", "packages": [], "version": "1.1.0" }\n'
    )
    after = session.connect()
    assert after["workspace_id"] == before["workspace_id"]
    session.sync()


class PruningService(Service):
    """A service that has removed finished history, as LeanWarp's window does."""

    def __init__(self) -> None:
        super().__init__()
        self.removed: set[str] = set()
        self.expired_keys: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.split("/operations/")[-1].removesuffix("/cancel")
        if "/operations/" in request.url.path and operation in self.removed:
            self.requests.append(request)
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})
        if request.headers.get("Idempotency-Key") in self.expired_keys:
            self.requests.append(request)
            return httpx.Response(
                409, json={"error": {"code": "operation_expired", "message": "expired"}}
            )
        return super().handle(request)


@pytest.fixture
def pruned(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = PruningService()
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(service.handle),
    ) as cloud:
        yield tmp_path, service, ProjectSession(cloud, tmp_path)


def test_a_removed_last_operation_does_not_strand_the_project(pruned):
    root, service, session = pruned
    session.connect()
    first = session.submit("check", {"file": "Main.lean"})
    service.removed.add(first["operation_id"])

    assert session.status()["operation_expired"] is True
    assert "operation" not in session.status()
    with pytest.raises(SessionError, match="no longer stores"):
        session.wait(timeout=1)
    with pytest.raises(SessionError, match="no longer stores"):
        session.cancel()
    # A new operation is submitted normally and becomes the project's last one.
    second = session.submit("check", {"file": "Main.lean"})
    assert second["operation_id"] != first["operation_id"]
    assert session.status()["operation"]["operation_id"] == second["operation_id"]
    # Disconnecting gets past the removed operation to its own workspace check.
    service.removed.add(second["operation_id"])
    with pytest.raises(SessionError, match="leanwarp stop"):
        session.disconnect()
    journal = json.loads((root / ".leanwarp/session.json").read_text())
    assert journal["operation_id"] == second["operation_id"]


def test_other_operation_read_failures_still_stop_submission(pruned):
    _, service, session = pruned
    session.connect()
    session.submit("check", {"file": "Main.lean"})
    service.fail_operation_reads = 1
    count = len(service.receipts)
    with pytest.raises(httpx.ConnectError):
        session.submit("check", {"file": "Main.lean"})
    assert len(service.receipts) == count


def test_recovering_an_expired_submission_never_runs_it_again(pruned):
    root, service, session = pruned
    session.connect()
    session.sync()
    service.lose = "/operations"
    with pytest.raises(httpx.ReadTimeout):
        session.submit("check", {"file": "Main.lean"})
    pending = json.loads((root / ".leanwarp/session.json").read_text())["pending"]
    service.expired_keys.add(pending["idempotency_key"])
    count = len(service.receipts)
    with pytest.raises(LeanWarpCloudError) as error:
        session.recover()
    assert error.value.code == "operation_expired"
    assert len(service.receipts) == count
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None


class SharedWorkspace(Service):
    """A workspace another client also writes to, with real revision conflicts."""

    def __init__(self) -> None:
        super().__init__()
        self.denied_submissions = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/files"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "workspace_id": "w",
                    "revision": self.revision,
                    "files": {
                        name: hashlib.sha256(text.encode()).hexdigest()
                        for name, text in self.sources.items()
                    },
                },
            )
        if (
            request.method == "PUT"
            and path.endswith("/files")
            and json.loads(request.content)["expected_revision"] != self.revision
        ):
            self.requests.append(request)
            return httpx.Response(409, json={"error": {"code": "revision_conflict"}})
        if path.endswith("/stop"):
            self.requests.append(request)
            return httpx.Response(200, json={"workspace_id": "w", "state": "stopped"})
        if path.endswith("/operations") and self.denied_submissions:
            self.denied_submissions -= 1
            self.requests.append(request)
            return httpx.Response(
                403, json={"error": {"code": "leanwarp_key_scope_denied", "message": "denied"}}
            )
        return super().handle(request)


@pytest.fixture
def shared(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = SharedWorkspace()
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(service.handle),
    ) as cloud:
        yield tmp_path, service, ProjectSession(cloud, tmp_path)


def test_stop_does_not_wait_for_an_unresolved_submission(shared):
    root, service, session = shared
    session.connect()
    session.sync()
    # A key without execute permission is refused, and the SDK keeps the request
    # for recovery because it cannot know the refusal was final.
    service.denied_submissions = 1
    with pytest.raises(LeanWarpCloudError):
        session.submit("check", {"file": "Main.lean"})
    pending = json.loads((root / ".leanwarp/session.json").read_text())["pending"]
    assert pending is not None
    stopped = session.stop()
    assert stopped["recovery_required"] is True
    assert service.requests[-1].url.path.endswith("/w/stop")
    # The unresolved request stays recorded for recovery.
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] == pending
    with pytest.raises(SessionError, match="recover"):
        session.submit("check", {"file": "Main.lean"})


def test_stop_without_a_known_workspace_still_needs_recovery(shared):
    _, service, session = shared
    service.lose = "/workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect()
    with pytest.raises(SessionError, match="recover"):
        session.stop()


def test_reconcile_adopts_another_clients_revision_explicitly(shared):
    root, service, session = shared
    session.connect()
    session.sync()
    # Another client changes the workspace.
    service.sources["Other.lean"] = "def other := 1"
    service.sources["Main.lean"] = "theorem candidate : True := trivial"
    service.revision += 1
    (root / "Helper.lean").write_text("def helper := 2")
    with pytest.raises(LeanWarpCloudError) as conflict:
        session.sync()
    assert conflict.value.code == "revision_conflict"
    # Recovery cannot help: the refused sync has no accepted effect to replay.
    assert session.recover()["recovery_required"] is False
    with pytest.raises(LeanWarpCloudError):
        session.sync()

    report = session.reconcile()
    assert report == {
        "workspace_id": "w",
        "revision": service.revision,
        "previous_revision": service.revision - 1,
        "upload": ["Helper.lean", "Main.lean"],
        "delete": ["Other.lean"],
    }
    synced = session.sync()
    assert synced["revision"] == service.revision
    assert set(service.sources) == {"Main.lean", "Helper.lean"}
    assert "by trivial" in service.sources["Main.lean"]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("Invalid.lean", b"\xff", "not UTF-8"),
        ("Invalid.lean", b"a" * (MAX_UPLOAD_BYTES + 1), "larger than"),
        ("invalid-name.lean", b"def value := 1", "module path"),
    ],
    ids=["utf8", "size", "path"],
)
def test_failed_reconcile_preserves_revision_fence(shared, name, content, message):
    root, service, session = shared
    session.connect()
    session.sync()
    service.sources["Other.lean"] = "def other := 1"
    service.revision += 1
    (root / "Helper.lean").write_text("def helper := 2")
    before = session.path.read_bytes()
    invalid = root / name
    invalid.write_bytes(content)

    with pytest.raises(ProjectError, match=message):
        session.reconcile()
    assert session.path.read_bytes() == before
    invalid.unlink()
    # Fixing the local problem must not authorize overwriting another client's
    # changes without a successful reconciliation and its preview.
    with pytest.raises(LeanWarpCloudError) as conflict:
        session.sync()
    assert conflict.value.code == "revision_conflict"
    assert service.sources["Other.lean"] == "def other := 1"
    assert session.reconcile()["delete"] == ["Other.lean"]


class AccountWorkspaces(SharedWorkspace):
    """An account with a second workspace and stored operation history."""

    def __init__(self) -> None:
        super().__init__()
        self.history = [
            {"operation_id": f"op-{n}", "workspace_id": "w", "state": "completed"} for n in range(5)
        ]
        self.deleted: list[str] = []
        self.delete_error: tuple[int, str] | None = None
        self.usage_status: int | None = None
        self.usage_disconnect = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/usage") and self.usage_disconnect:
            self.requests.append(request)
            raise httpx.ReadTimeout("usage response lost", request=request)
        if path.endswith("/usage") and self.usage_status is not None:
            self.requests.append(request)
            return httpx.Response(self.usage_status, json={"error": {"code": "unavailable"}})
        if request.method == "DELETE":
            self.requests.append(request)
            workspace = path.rsplit("/", 1)[-1]
            error = self.delete_error or ((404, "not_found") if workspace in self.deleted else None)
            if error:
                status, code = error
                return httpx.Response(status, json={"error": {"code": code}})
            self.deleted.append(workspace)
            if self.lose == "/delete":
                self.lose = ""
                raise httpx.ReadTimeout("deletion response lost", request=request)
            return httpx.Response(204)
        if request.method == "GET" and path.endswith("/v1/leanwarp/workspaces"):
            self.requests.append(request)
            listed = [{"workspace_id": "w", "bundle_id": "b"}, {"workspace_id": "x"}]
            return httpx.Response(200, json={"workspaces": listed})
        if request.method == "GET" and path.endswith("/operations"):
            self.requests.append(request)
            start = int(request.url.params.get("cursor", "0"))
            limit = int(request.url.params["limit"])
            page = self.history[start : start + 2][:limit]
            more = start + len(page) < len(self.history)
            return httpx.Response(
                200,
                json={
                    "operations": page,
                    "next_cursor": str(start + len(page)) if more else None,
                },
            )
        if request.method == "GET" and path.endswith("/workspaces/w"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "workspace_id": "w",
                    "bundle_id": "b",
                    "revision": self.revision,
                    "max_spend_microusd": self.max_spend_microusd,
                },
            )
        return super().handle(request)


@pytest.fixture
def account(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = AccountWorkspaces()
    # The workspace already holds this project's Main.lean, from another machine.
    service.sources = {
        "Main.lean": "theorem candidate : True := by trivial\n",
        "Old.lean": "def old := 0",
    }
    service.revision = 3
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(service.handle),
    ) as cloud:
        yield tmp_path, service, ProjectSession(cloud, tmp_path)


def test_connecting_to_an_existing_workspace_uploads_only_the_differences(account):
    root, service, session = account
    (root / "Helper.lean").write_text("def helper := 2")
    connected = session.connect(workspace_id="w")
    assert connected["connected_existing"] is True
    assert connected["upload"] == ["Helper.lean"] and connected["delete"] == ["Old.lean"]
    assert service.workspace_count == 0
    # Connecting again to the same workspace changes nothing.
    assert session.connect(workspace_id="w")["workspace_id"] == "w"
    synced = session.sync()
    assert synced["revision"] == 4
    upload = next(r for r in reversed(service.requests) if r.method == "PUT")
    assert upload.url.path.endswith("/workspaces/w/files")
    assert json.loads(upload.content) == {
        "expected_revision": 3,
        "files": {"Helper.lean": "def helper := 2"},
        "delete_paths": ["Old.lean"],
    }
    with pytest.raises(SessionError, match="connected to workspace w"):
        session.connect(workspace_id="x")


def test_an_existing_workspace_must_match_the_project_or_be_named(account):
    root, _, session = account
    with pytest.raises(UsageError, match="keeps its own environment"):
        session.connect(workspace_id="w", resource_profile="large")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
    with pytest.raises(SessionError, match="--environment b"):
        session.connect(workspace_id="w")
    assert not (root / ".leanwarp/session.json").exists()
    with pytest.raises(SessionError, match="not other"):
        session.connect(workspace_id="w", environment="other")
    assert session.connect(workspace_id="w", environment="b")["connected_existing"] is True


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("Invalid.lean", b"\xff", "not UTF-8"),
        ("Invalid.lean", b"a" * (MAX_UPLOAD_BYTES + 1), "larger than"),
        ("invalid-name.lean", b"def value := 1", "module path"),
    ],
    ids=["utf8", "size", "path"],
)
def test_failed_attach_does_not_save_an_unpreviewed_baseline(account, name, content, message):
    root, _, session = account
    invalid = root / name
    invalid.write_bytes(content)
    with pytest.raises(ProjectError, match=message):
        session.connect(workspace_id="w")
    assert not session.path.exists()
    invalid.unlink()
    assert session.connect(workspace_id="w")["delete"] == ["Old.lean"]


def test_workspaces_mark_this_project_and_history_reads_every_page(account):
    _, service, session = account
    listed = session.workspaces()["workspaces"]
    assert [item["this_project"] for item in listed] == [False, False]
    with pytest.raises(SessionError, match="not connected"):
        list(session.history())
    session.connect(workspace_id="w")
    assert [item["this_project"] for item in session.workspaces()["workspaces"]] == [True, False]
    assert [item["operation_id"] for item in session.history()] == [f"op-{n}" for n in range(5)]
    first = session.history_page()
    assert first["next_cursor"] == "2" and len(first["operations"]) == 2
    service.history[3] = {**service.history[3], "workspace_id": "x"}
    with pytest.raises(SessionError, match="history was incomplete"):
        list(session.history())


def test_deleting_needs_the_workspace_named_and_disconnects_only_its_project(account):
    root, service, session = account
    service.lose = "/workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect()
    # An unresolved create could be the workspace being deleted.
    with pytest.raises(SessionError, match="recover"):
        session.delete("w")
    assert service.deleted == []
    (root / ".leanwarp/session.json").unlink()
    session.connect(workspace_id="w")
    assert session.delete("x") == {"status": "deleted", "workspace_id": "x", "disconnected": False}
    assert json.loads((root / ".leanwarp/session.json").read_text())["workspace_id"] == "w"
    assert session.delete("w") == {"status": "deleted", "workspace_id": "w", "disconnected": True}
    assert service.deleted == ["x", "w"]
    journal = json.loads((root / ".leanwarp/session.json").read_text())
    assert "workspace_id" not in journal and journal["files"] == {}
    assert journal["pending"] is None


@pytest.mark.parametrize("deleted_by", ["lost_response", "dashboard"])
def test_explicit_delete_recovers_an_already_deleted_connection(account, deleted_by):
    root, service, session = account
    session.connect(workspace_id="w")
    # A dashboard deletion may race a local request with an uncertain outcome.
    service.denied_submissions = 1
    with pytest.raises(LeanWarpCloudError):
        session.submit("check", {"file": "Main.lean"})
    before = session.path.read_bytes()
    assert json.loads(before)["pending"] is not None
    if deleted_by == "lost_response":
        service.lose = "/delete"
        with pytest.raises(httpx.ReadTimeout):
            session.delete("w")
        assert session.path.read_bytes() == before
    else:
        service.deleted.append("w")

    resumed = ProjectSession(session.cloud, root)
    assert resumed.delete("w") == {"status": "deleted", "workspace_id": "w", "disconnected": True}
    journal = json.loads(session.path.read_text())
    assert "workspace_id" not in journal and journal["pending"] is None
    assert journal["files"] == {} and journal["owner_id"] == "owner-a"
    assert resumed.connect()["workspace_id"] == "w"
    assert service.workspace_count == 1


@pytest.mark.parametrize(
    ("workspace", "error"),
    [
        ("x", (404, "not_found")),
        ("w", (404, "unavailable")),
        ("w", (403, "leanwarp_key_scope_denied")),
        ("w", (503, "unavailable")),
    ],
)
def test_delete_keeps_connection_on_unconfirmed_absence(account, workspace, error):
    _, service, session = account
    session.connect(workspace_id="w")
    before = session.path.read_bytes()
    service.delete_error = error
    with pytest.raises(LeanWarpCloudError) as rejected:
        session.delete(workspace)
    assert (rejected.value.status_code, rejected.value.code) == error
    assert session.path.read_bytes() == before


def test_deleted_connection_still_requires_its_original_account(account):
    root, service, session = account
    session.connect(workspace_id="w")
    service.deleted.append("w")
    before = session.path.read_bytes()

    def other_account(request):
        if request.url.path.endswith("/account"):
            return httpx.Response(200, json={"owner_id": "owner-b"})
        return service.handle(request)

    with (
        LeanWarpCloud(
            "other-secret",
            base_url="https://api.example",
            retries=0,
            transport=httpx.MockTransport(other_account),
        ) as cloud,
        pytest.raises(SessionError, match="another account"),
    ):
        ProjectSession(cloud, root).delete("w")
    assert session.path.read_bytes() == before
    assert not any(request.method == "DELETE" for request in service.requests)


@pytest.mark.parametrize("status", [404, 503])
def test_status_and_recovery_do_not_depend_on_history_usage(account, status):
    _, service, session = account
    service.usage_status = status
    session.connect(workspace_id="w")
    report = session.status()
    assert report["workspace"]["workspace_id"] == "w" and "history_usage" not in report
    assert session.recover()["recovery_required"] is False


def test_status_and_recovery_survive_usage_transport_failure(account):
    _, service, session = account
    session.connect(workspace_id="w")
    service.usage_disconnect = True
    assert "history_usage" not in session.status()
    assert session.recover()["recovery_required"] is False


def _suggested(source: str, replacement: str = "omega") -> Any:
    start = source.encode().index(b"sorry")
    end = start + len("sorry")
    edited = (source.encode()[:start] + replacement.encode() + source.encode()[end:]).decode()

    def result(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "closed",
            "file": body["payload"]["file"],
            "tactic": replacement,
            "verification": "strict",
            "patch": {
                "start_utf8_offset": start,
                "end_utf8_offset": end,
                "replacement": replacement,
            },
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "patched_source_sha256": hashlib.sha256(edited.encode()).hexdigest(),
        }

    return result, edited


SORRY = "theorem t (a b : Nat) : a + b = b + a := by\n  sorry\n"


def test_apply_writes_a_suggestion_and_uploads_it_as_the_next_revision(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    service.result_for, edited = _suggested(SORRY)
    session.connect()
    suggested = session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    assert OperationOutcome(suggested).successful
    revision = service.revision
    applied = session.apply()
    assert applied["status"] == "applied" and applied["file"] == "Main.lean"
    assert (root / "Main.lean").read_text() == edited
    assert service.sources["Main.lean"] == edited
    assert applied["revision"] == service.revision == revision + 1


def test_apply_refuses_a_file_edited_after_the_suggestion(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    service.result_for, _ = _suggested(SORRY)
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    (root / "Main.lean").write_text(SORRY + "-- edited\n")
    with pytest.raises(SessionError, match="changed locally"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY + "-- edited\n"


def test_apply_refuses_a_result_computed_before_a_later_upload(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    service.result_for, _ = _suggested(SORRY)
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    (root / "Other.lean").write_text("theorem other : True := trivial\n")
    session.sync()
    with pytest.raises(SessionError, match="uploaded after"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY


def test_apply_refuses_results_whose_hashes_do_not_bind_this_file(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    result, _ = _suggested(SORRY)
    service.result_for = lambda body: {**result(body), "patched_source_sha256": "0" * 64}
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    with pytest.raises(SessionError, match="checksum"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY


@pytest.mark.parametrize("kind", ["check", "holes", "check_files"])
def test_apply_only_writes_editing_results(project, kind):
    _, _, _, session = project
    session.connect()
    payload = {"files": ["Main.lean"]} if kind == "check_files" else {"file": "Main.lean"}
    session.submit_and_wait(kind, payload, wait_seconds=0)
    with pytest.raises(SessionError, match="only a suggest or sorry2lemma"):
        session.apply()


def test_apply_writes_a_sorry2lemma_transformation(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    transformed = "theorem obligation (a b : Nat) : a + b = b + a := by\n  sorry\n" + SORRY
    service.result_for = lambda body: {
        "status": "extracted",
        "file": body["payload"]["file"],
        "source_changed": True,
        "source_sha256": hashlib.sha256(SORRY.encode()).hexdigest(),
        "transformed_source": transformed,
        "transformed_source_sha256": hashlib.sha256(transformed.encode()).hexdigest(),
    }
    session.connect()
    session.submit_and_wait("sorry2lemma", {"file": "Main.lean"}, wait_seconds=0)
    assert session.apply()["status"] == "applied"
    assert (root / "Main.lean").read_text() == transformed


def _suggestion_for_apply(project: Any) -> tuple[Path, Any, ProjectSession, str]:
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    service.result_for, edited = _suggested(SORRY)
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    return root, service, session, edited


def test_apply_refuses_a_result_the_client_does_not_count_as_successful(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    result, _ = _suggested(SORRY)
    # Closed, with hashes that bind, but no recheck verdict this client knows.
    service.result_for = lambda body: {**result(body), "verification": "unchecked"}
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    with pytest.raises(SessionError, match="not a verified edit"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY


def test_apply_after_a_failed_upload_uploads_the_edit_it_already_wrote(project):
    root, service, session, edited = _suggestion_for_apply(project)
    revision = service.revision
    service.reject = True  # Another device advanced the workspace first.
    with pytest.raises(LeanWarpCloudError):
        session.apply()
    assert (root / "Main.lean").read_text() == edited
    applied = session.apply()
    assert applied["status"] == "applied" and service.sources["Main.lean"] == edited
    assert applied["revision"] == service.revision == revision + 1


def test_apply_writes_nothing_when_the_environment_changed(project):
    root, _, session, _ = _suggestion_for_apply(project)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
    with pytest.raises(SessionError, match="toolchain"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY


def test_apply_never_overwrites_an_editor_save_made_while_it_runs(project, monkeypatch):
    root, _, session, _ = _suggestion_for_apply(project)
    real = tempfile.mkstemp

    def saved_meanwhile(*args: Any, **kwargs: Any) -> Any:
        (root / "Main.lean").write_text(SORRY + "-- saved by the editor\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(session_module.tempfile, "mkstemp", saved_meanwhile)
    with pytest.raises(SessionError, match="changed while"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY + "-- saved by the editor\n"
    assert not [path for path in root.iterdir() if path.name.startswith(".Main.lean.")]


def test_apply_refuses_read_only_and_hard_linked_files(project):
    root, _, session, _ = _suggestion_for_apply(project)
    target = root / "Main.lean"
    target.chmod(0o444)
    try:
        with pytest.raises(SessionError, match="read-only"):
            session.apply()
    finally:
        target.chmod(0o644)
    (root.parent / "linked.lean").unlink(missing_ok=True)
    os.link(target, root.parent / "linked.lean")
    with pytest.raises(SessionError, match="hard links"):
        session.apply()
    assert target.read_text() == SORRY


def test_a_journal_from_a_newer_client_stays_recoverable(project):
    _, service, _, session = project
    session.connect()
    session.sync()
    state = json.loads(session.path.read_text())
    # A newer client journaled a kind this one does not know, then lost the reply.
    state["pending"] = {
        "method": "submit",
        "arguments": {
            "workspace_id": "w",
            "kind": "future_tool",
            "expected_revision": service.revision,
            "payload": {"file": "Main.lean"},
        },
        "idempotency_key": "a" * 32,
        "hashes": None,
    }
    session.path.write_text(json.dumps(state))
    assert session.status()["recovery_required"] is True
    recovered = session.recover()
    assert recovered["kind"] == "future_tool"


def test_apply_edits_only_the_file_this_project_asked_about(project):
    root, service, _, session = project
    (root / "Main.lean").write_text(SORRY)
    (root / "Other.lean").write_text(SORRY)
    result, _ = _suggested(SORRY)
    # The server's result names another uploaded file with self-consistent hashes.
    service.result_for = lambda body: {**result(body), "file": "Other.lean"}
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Main.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    with pytest.raises(SessionError, match="did not ask"):
        session.apply()
    assert (root / "Other.lean").read_text() == SORRY


@pytest.mark.parametrize("record", [None, "[]", "not json", '{"operation_id": "other"}'])
def test_apply_refuses_a_result_whose_submission_was_not_recorded(project, record):
    root, _, session, _ = _suggestion_for_apply(project)
    # None: submitted by a client that predates the record. Otherwise unusable.
    if record is None:
        session.submitted.unlink()
    else:
        session.submitted.write_text(record)
    with pytest.raises(SessionError, match="did not ask"):
        session.apply()
    assert (root / "Main.lean").read_text() == SORRY


@pytest.mark.parametrize("interrupted_write", ["binding", "journal"])
def test_submission_binding_recovers_without_submitting_paid_work_again(
    project, monkeypatch, interrupted_write
):
    root, service, cloud, session = project
    (root / "Main.lean").write_text(SORRY)
    service.result_for, edited = _suggested(SORRY)
    session.connect()
    session.sync()
    write = session._write

    def interrupt(path, raw):
        if interrupted_write == "binding" and path == session.submitted:
            raise OSError("interrupted binding write")
        if path == session.path and json.loads(raw)["pending"] is None:
            raise OSError("interrupted journal commit")
        write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(session, "_write", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            session.submit("suggest", {"file": "Main.lean", "line": 2, "column": 3})
    pending = json.loads(session.path.read_text())["pending"]
    assert pending["method"] == "submit"
    accepted = service.operation["operation_id"]
    receipt_count = len(service.receipts)

    # A fresh process must finish the binding using the original accepted request.
    resumed = ProjectSession(cloud, root)
    assert resumed.recover()["operation_id"] == accepted
    assert len(service.receipts) == receipt_count
    submissions = [
        request
        for request in service.requests
        if request.method == "POST" and request.url.path.endswith("/operations")
    ]
    assert len(submissions) == 2
    assert submissions[0].content == submissions[1].content
    assert {request.headers["Idempotency-Key"] for request in submissions} == {
        pending["idempotency_key"]
    }
    assert json.loads(session.path.read_text())["pending"] is None
    assert resumed.apply()["status"] == "applied"
    assert (root / "Main.lean").read_text() == edited


def test_apply_preserves_editor_save_after_the_last_source_comparison(project, monkeypatch):
    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    saved = SORRY + "-- editor saved after comparison\n"
    real_read = Path.read_bytes
    reads = 0

    def save_after_read(path: Path) -> bytes:
        nonlocal reads
        value = real_read(path)
        if path == source:
            reads += 1
            # Inject the save after _replace_file obtains its comparison bytes.
            if reads == 1:
                source.write_text(saved)
        return value

    monkeypatch.setattr(Path, "read_bytes", save_after_read)
    revision = service.revision
    with pytest.raises(SessionError, match="changed while"):
        session.apply()
    assert source.read_text() == saved
    backup = next((root / ".leanwarp/apply-backups").glob("*/original"))
    assert backup.read_text() == saved
    assert service.revision == revision and service.sources["Main.lean"] == SORRY
    for command in (session.apply, session.sync):
        with pytest.raises(SessionError, match="needs recovery"):
            command()
    assert session.recover()["recovery_required"] is True
    assert session.status()["apply_recovery"].endswith("apply-recovery.txt")
    # Explicit recovery leaves the editor's source as a new, unlinked inode.
    source.unlink()
    source.write_text(saved)
    (root / ".leanwarp/apply-recovery.txt").unlink()
    session.sync()
    assert service.sources["Main.lean"] == saved


def test_apply_does_not_overwrite_editor_atomic_save_during_publication(project, monkeypatch):
    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    saved = SORRY + "-- atomic editor save\n"
    real_link = os.link

    def save_before_publication(src, dst, *args, **kwargs):
        if Path(dst) == source and Path(src).name.startswith("proposed-"):
            source.write_text(saved)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(session_module.os, "link", save_before_publication)
    with pytest.raises(SessionError, match="changed while"):
        session.apply()
    assert source.read_text() == saved
    assert service.sources["Main.lean"] == SORRY
    assert next((root / ".leanwarp/apply-backups").glob("*/original")).read_text() == SORRY


def test_apply_preserves_late_writes_through_an_editors_open_descriptor(project):
    root, service, session, edited = _suggestion_for_apply(project)
    source = root / "Main.lean"
    with source.open("r+") as editor:
        result = session.apply()
        editor.seek(0)
        editor.write(SORRY + "-- saved through old descriptor\n")
        editor.truncate()
        editor.flush()
    backups = Path(result["backup_directory"])
    assert (
        next(backups.glob("*/original")).read_text().endswith("-- saved through old descriptor\n")
    )
    assert str(source) in next(backups.glob("*/RECOVERY.txt")).read_text()
    assert source.read_text() == service.sources["Main.lean"] == edited
    assert not (root / ".leanwarp/apply-recovery.txt").exists()


def test_interrupted_apply_requires_explicit_recovery_before_hash_retry(project, monkeypatch):
    root, service, session, edited = _suggestion_for_apply(project)
    source = root / "Main.lean"
    real_link = os.link

    def interrupted_after_publication(src, dst, *args, **kwargs):
        result = real_link(src, dst, *args, **kwargs)
        if Path(dst) == source and Path(src).name.startswith("proposed-"):
            raise OSError("interrupted after publication")
        return result

    monkeypatch.setattr(session_module.os, "link", interrupted_after_publication)
    with pytest.raises(OSError, match="interrupted"):
        session.apply()
    assert source.read_text() == edited
    assert service.sources["Main.lean"] == SORRY
    with pytest.raises(SessionError, match="needs recovery"):
        session.apply()
    assert next((root / ".leanwarp/apply-backups").glob("*/original")).read_text() == SORRY


def test_apply_cross_device_failure_leaves_source_and_retry_intact(project, monkeypatch):
    import errno

    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    real_rename = os.rename

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(session_module.os, "rename", cross_device)
    with pytest.raises(OSError, match="cross-device"):
        session.apply()
    assert source.read_text() == SORRY and service.sources["Main.lean"] == SORRY
    assert not (root / ".leanwarp/apply-recovery.txt").exists()
    monkeypatch.setattr(session_module.os, "rename", real_rename)
    assert session.apply()["status"] == "applied"


def test_apply_preserves_permissions_and_handles_nested_sources(project):
    root, service, _, session = project
    source = root / "Nested/Example.lean"
    source.parent.mkdir()
    source.write_text(SORRY)
    source.chmod(0o640)
    service.result_for, edited = _suggested(SORRY)
    session.connect()
    session.submit_and_wait(
        "suggest", {"file": "Nested/Example.lean", "line": 2, "column": 3}, wait_seconds=0
    )
    result = session.apply()
    assert source.read_text() == edited
    assert source.stat().st_mode & 0o777 == 0o640 and source.stat().st_nlink == 1
    assert next(Path(result["backup_directory"]).glob("*/original")).read_text() == SORRY


def test_apply_preserves_atomic_editor_save_between_comparison_and_rename(project, monkeypatch):
    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    saved = SORRY + "-- atomically saved before rename\n"
    real_rename = os.rename

    def save_before_displacement(src, dst):
        if Path(src) == source:
            editor_file = root / "editor-save"
            editor_file.write_text(saved)
            os.replace(editor_file, source)
        return real_rename(src, dst)

    monkeypatch.setattr(session_module.os, "rename", save_before_displacement)
    with pytest.raises(SessionError, match="changed while"):
        session.apply()
    assert source.read_text() == saved
    assert service.sources["Main.lean"] == SORRY
    assert next((root / ".leanwarp/apply-backups").glob("*/original")).read_text() == saved


def test_apply_detects_old_descriptor_save_while_publishing(project, monkeypatch):
    root, service, session, edited = _suggestion_for_apply(project)
    source = root / "Main.lean"
    real_link = os.link
    saved = SORRY + "-- editor saved during publish\n"
    with source.open("r+") as editor:

        def save_after_link(src, dst, *args, **kwargs):
            result = real_link(src, dst, *args, **kwargs)
            if Path(dst) == source and Path(src).name.startswith("proposed-"):
                editor.seek(0)
                editor.write(saved)
                editor.truncate()
                editor.flush()
            return result

        monkeypatch.setattr(session_module.os, "link", save_after_link)
        with pytest.raises(SessionError, match="changed while"):
            session.apply()
    assert source.read_text() == edited
    assert next((root / ".leanwarp/apply-backups").glob("*/original")).read_text() == saved
    assert service.sources["Main.lean"] == SORRY
    with pytest.raises(SessionError, match="needs recovery"):
        session.sync()


def test_apply_never_follows_a_symlink_installed_after_comparison(project, monkeypatch):
    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    target = root / "editor-target"
    target.write_text("do not change this\n")
    real_rename = os.rename

    def replace_with_symlink(src, dst):
        if Path(src) == source:
            source.unlink()
            source.symlink_to(target)
        return real_rename(src, dst)

    monkeypatch.setattr(session_module.os, "rename", replace_with_symlink)
    with pytest.raises(SessionError, match="changed while"):
        session.apply()
    assert source.is_symlink()
    assert target.read_text() == "do not change this\n"
    assert service.sources["Main.lean"] == SORRY


def test_apply_interrupted_after_rename_preserves_recovery_before_python_records_it(
    project, monkeypatch
):
    root, service, session, _ = _suggestion_for_apply(project)
    source = root / "Main.lean"
    real_rename = os.rename

    def interrupt_after_rename(src, dst):
        real_rename(src, dst)
        if Path(src) == source:
            raise KeyboardInterrupt("interrupted after rename")

    monkeypatch.setattr(session_module.os, "rename", interrupt_after_rename)
    revision = service.revision
    with pytest.raises(KeyboardInterrupt, match="interrupted after rename"):
        session.apply()
    original = next((root / ".leanwarp/apply-backups").glob("*/original"))
    assert original.read_text() == SORRY
    assert (original.parent / "RECOVERY.txt").is_file()
    assert source.read_text() == SORRY
    assert (root / ".leanwarp/apply-recovery.txt").is_file()
    assert session.status()["recovery_required"] is True
    assert session.recover()["apply_recovery"].endswith("apply-recovery.txt")
    for command in (session.apply, session.sync):
        with pytest.raises(SessionError, match="needs recovery"):
            command()
    assert service.revision == revision and service.sources["Main.lean"] == SORRY

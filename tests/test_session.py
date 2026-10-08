from __future__ import annotations

import hashlib
import json
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
                    "result": {"status": "rejected"},
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
    "kind,payload",
    [
        ("verify", {"file": "Main.lean"}),
        ("check", {"file": "Main.lean", "oversized": "x" * (8 * 1024 * 1024)}),
    ],
)
def test_invalid_submission_preserves_readable_journal_without_sending(project, kind, payload):
    root, service, cloud, session = project
    session.connect()
    session.sync()
    journal = session.path.read_bytes()
    service.lose = "/operations"
    with pytest.raises(SessionError):
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

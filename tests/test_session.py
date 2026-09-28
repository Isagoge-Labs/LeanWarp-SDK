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

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/account"):
            return httpx.Response(200, json={"owner_id": "owner-a"})
        if path.endswith("/versions"):
            return httpx.Response(
                200,
                json={
                    "versions": [
                        {
                            "bundle_id": "b",
                            "lean_toolchain": "leanprover/lean4:v4.26.0",
                            "lake_manifest_sha256": hashlib.sha256(b"{}").hexdigest(),
                        }
                    ]
                },
            )
        if request.method == "GET":
            value = (
                self.operation
                if "/operations/" in path
                else {"workspace_id": "w", "revision": self.revision}
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
            result = self.operation
        self.receipts[key] = request.content, result
        if self.lose and path.endswith(self.lose):
            self.lose = ""
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=result)


@pytest.fixture
def project(tmp_path: Path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text("{}")
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = Service()
    with LeanWarpCloud(
        "https://api.example", "secret", retries=0, transport=httpx.MockTransport(service.handle)
    ) as cloud:
        yield tmp_path, service, cloud, ProjectSession(cloud, tmp_path)


@pytest.mark.parametrize("kind", ["workspaces", "files", "operations"])
def test_lost_response_recovers_exact_intent_after_restart(project, kind):
    root, service, cloud, session = project
    if kind != "workspaces":
        session.connect(max_spend_microusd=2_000_000)
    if kind == "operations":
        session.sync()
    service.lose = "/" + kind
    with pytest.raises(httpx.ReadTimeout):
        if kind == "workspaces":
            session.connect(max_spend_microusd=2_000_000)
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
    session.connect(max_spend_microusd=2_000_000)
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
    session.connect(max_spend_microusd=2_000_000)
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
    session.connect(max_spend_microusd=2_000_000)
    service.reject = True
    with pytest.raises(LeanWarpCloudError):
        session.sync()
    session.sync()
    assert service.revision == 1


def test_lock_prevents_two_local_clients_from_mutating(project):
    root, service, cloud, session = project
    session.connect(max_spend_microusd=2_000_000)
    with session._locked(), pytest.raises(SessionError, match="another"):
        ProjectSession(cloud, root).sync()
    assert service.revision == 0


def test_changed_environment_never_silently_upgrades(project):
    root, _, _, session = project
    session.connect(max_spend_microusd=2_000_000)
    (root / "lake-manifest.json").write_text('{"changed":true}')
    with pytest.raises(SessionError, match="dependencies changed"):
        session.sync()


def test_malformed_journal_is_not_overwritten(project):
    root, _, _, session = project
    session.connect(max_spend_microusd=2_000_000)
    journal = root / ".leanwarp/session.json"
    journal.write_text("{broken")
    with pytest.raises(SessionError, match="invalid"):
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
    session.connect(max_spend_microusd=2_000_000)
    with pytest.raises(ValueError):
        session.submit("check", {"file": "Main.lean"}, timeout_seconds=0)
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is None
    session.submit("check", {"file": "Main.lean"})


def test_wrong_account_cannot_recover_a_pending_create(project):
    root, service, _, session = project
    service.lose = "/workspaces"
    with pytest.raises(httpx.ReadTimeout):
        session.connect(max_spend_microusd=2_000_000)
    before = (root / ".leanwarp/session.json").read_bytes()

    def other_account(request):
        assert request.url.path.endswith("/account")
        return httpx.Response(200, json={"owner_id": "owner-b"})

    with (
        LeanWarpCloud(
            "https://api.example", "other-key", transport=httpx.MockTransport(other_account)
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
    session.connect(max_spend_microusd=2_000_000)
    path = root / ".leanwarp/session.json"
    state = json.loads(path.read_text())
    state[field] = value
    path.write_text(json.dumps(state))
    count = len(service.requests)
    with pytest.raises(SessionError, match="invalid"):
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
    original = cloud.create_workspace
    monkeypatch.setattr(cloud, "create_workspace", lambda **_: receipt)
    with pytest.raises(SessionError, match="invalid API receipt"):
        session.connect(max_spend_microusd=2_000_000)
    state = json.loads((root / ".leanwarp" / "session.json").read_text())
    assert state["pending"] is not None
    assert "workspace_id" not in state
    monkeypatch.setattr(cloud, "create_workspace", original)
    assert ProjectSession(cloud, root).recover()["workspace_id"] == "w"


def test_unhashable_journal_method_fails_closed_with_actionable_error(project):
    root, _, cloud, session = project
    session.connect(max_spend_microusd=2_000_000)
    path = root / ".leanwarp" / "session.json"
    state = json.loads(path.read_text())
    state["pending"] = {"method": [], "arguments": {}, "idempotency_key": "a" * 32, "hashes": None}
    path.write_text(json.dumps(state))
    with pytest.raises(SessionError, match="preserve"):
        ProjectSession(cloud, root).recover()
    assert json.loads(path.read_text()) == state


def test_disconnect_requires_confirmed_stop_and_allows_new_dependencies(project, monkeypatch):
    _, _, cloud, session = project
    session.connect(max_spend_microusd=2_000_000)
    with pytest.raises(SessionError, match="stop"):
        session.disconnect()
    monkeypatch.setattr(cloud, "workspace", lambda _: {"workspace_id": "w", "state": "stopped"})
    monkeypatch.setattr(cloud, "stop", lambda _: {"workspace_id": "w", "state": "stopped"})
    assert session.disconnect()["workspace_id"] == "w"
    assert session.connect(max_spend_microusd=3_000_000)["workspace_id"] == "w"


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_disconnect_preserves_nonterminal_operation_on_stopped_workspace(
    project, monkeypatch, state
):
    _, service, cloud, session = project
    session.connect(max_spend_microusd=2_000_000)
    session.submit("check", {"file": "Main.lean"})
    service.operation["state"] = state
    monkeypatch.setattr(cloud, "workspace", lambda _: {"workspace_id": "w", "state": "stopped"})
    before = session.path.read_bytes()
    with pytest.raises(SessionError, match="active"):
        session.disconnect()
    assert session.path.read_bytes() == before


def test_disconnect_retains_connection_when_server_has_other_device_work(project, monkeypatch):
    _, _, cloud, session = project
    session.connect(max_spend_microusd=2_000_000)
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
    session.connect(max_spend_microusd=2_000_000)
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
@pytest.mark.parametrize("value", ["", "x" * 257, None])
def test_invalid_connect_profile_does_not_poison_first_connection(project, field, value):
    root, service, cloud, session = project
    with pytest.raises(SessionError):
        session.connect(max_spend_microusd=2_000_000, **{field: value})
    assert not session.path.exists()
    assert service.workspace_count == 0
    assert ProjectSession(cloud, root).connect(max_spend_microusd=2_000_000)["workspace_id"] == "w"

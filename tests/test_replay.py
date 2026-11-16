"""A transient failure never strands a project; explicit operation ids stay bound to it."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import httpx
import pytest
from leanwarp_cloud import (
    LeanWarpCloud,
    LeanWarpCloudError,
    LeanWarpTransportError,
    ProjectSession,
    SessionError,
)
from leanwarp_cloud import session as session_module

from .test_session import Service


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
        yield tmp_path, service, ProjectSession(cloud, tmp_path)


def journal(root: Path) -> dict:
    return json.loads((root / ".leanwarp/session.json").read_text())


def posts(service: Service, ending: str) -> list[httpx.Request]:
    return [r for r in service.requests if r.method != "GET" and r.url.path.endswith(ending)]


@pytest.mark.parametrize(
    ("lost", "first", "ending"),
    [
        ("/workspaces", "connect", "/workspaces"),
        ("/files", "sync", "/files"),
        ("/operations", "check", "/operations"),
        ("/run", "check", "/run"),
    ],
)
def test_the_next_command_resends_the_saved_request_with_its_key(project, lost, first, ending):
    root, service, session = project
    if lost != "/workspaces":
        session.connect()
    if lost == "/operations":
        session.sync()
    service.lose = lost
    with pytest.raises(LeanWarpTransportError):
        if first == "connect":
            session.connect()
        elif first == "sync":
            session.sync()
        else:
            session.submit("check", {"file": "Main.lean"})
    assert journal(root)["pending"] is not None
    accepted = len(service.receipts)
    if first == "check":
        # Waiting is a command too, and it is the natural next one.
        session.wait(timeout=5)
    else:
        getattr(session, first)()
    assert journal(root)["pending"] is None
    assert len(service.receipts) == accepted
    (one, two) = posts(service, ending)[:2]
    assert one.headers["Idempotency-Key"] == two.headers["Idempotency-Key"]
    assert one.content == two.content


def test_repeating_the_same_command_after_a_lost_response_pays_once(project):
    root, service, session = project
    session.connect()
    session.sync()
    service.lose = "/operations"
    with pytest.raises(LeanWarpTransportError):
        session.submit("check", {"file": "Main.lean"})
    first = service.operation["operation_id"]
    again = session.submit("check", {"file": "Main.lean"})
    assert again["operation_id"] == first
    assert len(posts(service, "/operations")) == 2
    assert journal(root)["operation_id"] == first


def test_a_different_command_after_a_resend_runs_once_the_first_has_finished(project):
    _, service, session = project
    session.connect()
    session.sync()
    service.lose = "/operations"
    with pytest.raises(LeanWarpTransportError):
        session.submit("check", {"file": "Main.lean"})
    first = service.operation["operation_id"]
    other = session.submit("holes", {"file": "Main.lean"})
    assert other["kind"] == "holes" and other["operation_id"] != first
    assert len(posts(service, "/operations")) == 3


def test_a_different_command_is_refused_while_the_resent_work_still_runs(project):
    root, service, session = project
    session.connect()
    session.sync()
    service.pending_polls = 1_000
    service.lose = "/operations"
    with pytest.raises(LeanWarpTransportError):
        session.submit("check", {"file": "Main.lean"})
    with pytest.raises(SessionError, match="still running"):
        session.submit("holes", {"file": "Main.lean"})
    assert journal(root)["pending"] is None
    assert len(posts(service, "/operations")) == 2


def test_changed_sources_are_never_taken_for_the_resent_work(project):
    root, service, session = project
    session.connect()
    service.lose = "/run"
    with pytest.raises(LeanWarpTransportError):
        session.submit("check", {"file": "Main.lean"})
    (root / "Main.lean").write_text("theorem candidate : True := trivial\n")
    session.submit("check", {"file": "Main.lean"})
    assert "trivial" in service.sources["Main.lean"]
    assert "by trivial" not in service.sources["Main.lean"]
    assert len(posts(service, "/run")) == 3


def outage(status: int, code: str):
    return httpx.Response(status, json={"error": {"code": code, "message": "no"}})


@pytest.mark.parametrize(
    ("status", "code"),
    [(503, "unavailable"), (401, "authentication_required"), (429, "rate_limited")],
)
def test_a_transient_failure_keeps_the_request_and_the_next_command_resends_it(
    tmp_path, status, code
):
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = Service()
    down = {"on": False}

    def handle(request: httpx.Request) -> httpx.Response:
        if down["on"] and request.method != "GET":
            service.requests.append(request)
            return outage(status, code)
        return service.handle(request)

    with LeanWarpCloud(
        "secret", base_url="https://api.example", retries=0, transport=httpx.MockTransport(handle)
    ) as cloud:
        session = ProjectSession(cloud, tmp_path)
        session.connect()
        down["on"] = True
        with pytest.raises(LeanWarpCloudError) as first:
            session.sync()
        assert first.value.code == code
        assert journal(tmp_path)["pending"] is not None
        # Still down: the same error, not an instruction to run another command.
        with pytest.raises(LeanWarpCloudError) as second:
            session.sync()
        assert second.value.code == code
        down["on"] = False
        assert session.sync()["revision"] == 1
        assert journal(tmp_path)["pending"] is None
        keys = {r.headers["Idempotency-Key"] for r in posts(service, "/files")}
        assert len(keys) == 1


def test_a_definite_refusal_of_the_resend_ends_it_and_reports_it(project):
    root, service, session = project
    session.connect()
    service.lose = "/files"
    with pytest.raises(LeanWarpTransportError):
        session.sync()
    # The recorded response is gone too, so the service judges the request afresh.
    service.receipts.clear()
    service.reject = True
    with pytest.raises(LeanWarpCloudError) as raised:
        session.sync()
    assert raised.value.code == "revision_conflict"
    assert journal(root)["pending"] is None


def test_stop_is_never_blocked_and_does_not_start_the_saved_work(project):
    root, service, session = project
    session.connect()
    session.sync()
    service.lose = "/operations"
    with pytest.raises(LeanWarpTransportError):
        session.submit("check", {"file": "Main.lean"})
    before = len(posts(service, "/operations"))
    original = service.handle

    def stopping(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stop"):
            service.requests.append(request)
            return httpx.Response(200, json={"workspace_id": "w", "state": "stopped"})
        return original(request)

    session.cloud._client._transport = httpx.MockTransport(stopping)
    stopped = session.stop()
    assert stopped["state"] == "stopped" and stopped["recovery_required"] is True
    assert len(posts(service, "/operations")) == before
    assert journal(root)["pending"] is not None


# The project lock


def hold_lock(root: Path, seconds: float) -> threading.Timer:
    (root / ".leanwarp").mkdir(mode=0o700, exist_ok=True)
    fd = os.open(root / ".leanwarp" / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    timer = threading.Timer(seconds, os.close, args=(fd,))
    timer.start()
    return timer


def test_a_command_waits_briefly_for_the_project_lock(project):
    root, _, session = project
    session.connect()
    timer = hold_lock(root, 0.2)
    assert session.sync()["revision"] == 1
    timer.join()


def test_a_lock_held_too_long_is_a_retryable_busy_error(project, monkeypatch):
    root, _, session = project
    session.connect()
    monkeypatch.setattr(session_module, "_LOCK_WAIT_SECONDS", 0.1)
    timer = hold_lock(root, 0.5)
    with pytest.raises(SessionError) as raised:
        session.sync()
    assert raised.value.code == "project_busy" and raised.value.retryable is True
    timer.join()


# Naming an operation


def run_check(session: ProjectSession) -> str:
    return session.submit("check", {"file": "Main.lean"})["operation_id"]


def test_wait_and_status_can_name_an_earlier_operation(project):
    _, service, session = project
    session.connect()
    earlier = run_check(session)
    latest = session.submit("holes", {"file": "Main.lean"})["operation_id"]
    assert latest != earlier
    service.operation = {**service.operation, "operation_id": earlier, "kind": "check"}
    assert session.wait(timeout=5, operation_id=earlier)["operation_id"] == earlier
    assert session.status(operation_id=earlier)["operation"]["operation_id"] == earlier
    # Naming none still means this project's latest.
    service.operation = {**service.operation, "operation_id": latest, "kind": "holes"}
    assert session.wait(timeout=5)["operation_id"] == latest
    assert session.status()["operation"]["operation_id"] == latest


def test_an_operation_from_another_workspace_is_refused(project):
    _, service, session = project
    session.connect()
    run_check(session)
    service.operation = {**service.operation, "operation_id": "foreign", "workspace_id": "other"}
    with pytest.raises(SessionError, match="another workspace"):
        session.wait(timeout=5, operation_id="foreign")
    with pytest.raises(SessionError, match="another workspace"):
        session.status(operation_id="foreign")


def test_cancelling_another_workspaces_operation_sends_nothing(project):
    _, service, session = project
    session.connect()
    run_check(session)
    service.operation = {**service.operation, "operation_id": "foreign", "workspace_id": "other"}
    with pytest.raises(SessionError, match="another workspace"):
        session.cancel(operation_id="foreign")
    assert not [r for r in service.requests if r.url.path.endswith("/cancel")]


def test_cancel_can_name_an_operation(project):
    _, service, session = project
    session.connect()
    earlier = run_check(session)
    original = service.handle
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            asked.append(request.url.path)
            return httpx.Response(
                200, json={"operation_id": earlier, "workspace_id": "w", "state": "cancelled"}
            )
        return original(request)

    session.cloud._client._transport = httpx.MockTransport(handle)
    assert session.cancel(operation_id=earlier)["state"] == "cancelled"
    assert asked == [f"/v1/leanwarp/operations/{earlier}/cancel"]


def test_an_expired_named_operation_is_reported_not_raised_by_status(project):
    _, service, session = project
    session.connect()
    original = service.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if "/operations/" in request.url.path:
            return outage(404, "not_found")
        return original(request)

    session.cloud._client._transport = httpx.MockTransport(handle)
    assert session.status(operation_id="gone")["operation_expired"] is True

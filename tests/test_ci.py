"""CI is one command: --block, check --all, and connect --ephemeral with stop --delete."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, ProjectSession, SessionError, UsageError, cli
from leanwarp_cloud import session as session_module

from .test_session import Service

TEST_KEY = f"lw_test_{'a' * 32}.{'s' * 43}"


class CIService(Service):
    """The fake service, with deletion, stopping and cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.fail_batches: set[int] = set()
        self.batches = 0
        self.result_for = self._results

    def _results(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["kind"] != "check_files":
            return {"status": "ok", "diagnostics": []}
        self.batches += 1
        files = body["payload"]["files"]
        failing = {name for name in files if "Bad" in name}
        return {
            "status": "failed" if failing else "ok",
            "passed": len(files) - len(failing),
            "failed": len(failing),
            "timed_out": 0,
            "infrastructure_failures": 0,
            "not_checked": 0,
            "results": [
                {"file": name, "status": "error" if name in failing else "ok"} for name in files
            ],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "DELETE":
            self.requests.append(request)
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if path.endswith("/stop"):
            self.requests.append(request)
            return httpx.Response(200, json={"workspace_id": "w", "state": "stopped"})
        if path.endswith("/cancel"):
            self.requests.append(request)
            self.cancelled.append(path.split("/")[-2])
            self.pending_polls = 0
            self.operation = {**self.operation, "state": "cancelled"}
            return httpx.Response(200, json=self.operation)
        return super().handle(request)


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
    (tmp_path / "Main.lean").write_text("theorem candidate : True := by trivial\n")
    service = CIService()

    def client(*, interface):
        return LeanWarpCloud(
            TEST_KEY,
            base_url="https://api.example",
            retries=0,
            transport=httpx.MockTransport(service.handle),
            interface=interface,
        )

    monkeypatch.setattr(cli, "load_client", client)
    return tmp_path, service


def run(root: Path, capsys, *arguments: str) -> tuple[int, dict[str, Any]]:
    code = cli.main(["--project", str(root), *arguments])
    return code, json.loads(capsys.readouterr().out.splitlines()[-1])


# --block


def test_block_waits_past_the_inline_limit_until_the_operation_ends(project, capsys):
    root, service = project
    assert run(root, capsys, "connect")[0] == 0
    service.pending_polls = 3
    code, result = run(root, capsys, "check", "Main.lean", "--block")
    assert result["state"] == "completed" and code == 0


def test_block_gives_up_at_the_execution_timeout_and_reports_it_running(
    project, capsys, monkeypatch
):
    root, service = project
    monkeypatch.setattr(session_module, "_BLOCK_GRACE_SECONDS", 0.5)
    run(root, capsys, "connect")
    service.pending_polls = 1_000_000
    code, result = run(root, capsys, "check", "Main.lean", "--block", "--execution-timeout", "1")
    assert code == 3 and result["running"] is True and result["operation_id"]
    body = json.loads(next(r.content for r in reversed(service.requests) if r.method == "POST"))
    assert body["timeout_seconds"] == 1


def test_wait_block_waits_for_the_latest_operation(project, capsys):
    root, service = project
    run(root, capsys, "connect")
    service.pending_polls = 1_000_000
    assert run(root, capsys, "check", "Main.lean", "--wait", "0")[0] == 3
    service.pending_polls = 3
    code, result = run(root, capsys, "wait", "--block")
    assert code == 0 and result["state"] == "completed"


@pytest.mark.parametrize(
    "arguments",
    [
        ("check", "Main.lean", "--block", "--wait", "5"),
        ("wait", "--block", "--timeout", "5"),
        ("wait", "--execution-timeout", "5"),
        ("check",),
        ("check", "Main.lean", "--all"),
        ("check", "--all", "--wait", "5"),
    ],
)
def test_contradictory_waits_are_refused_before_anything_is_sent(project, capsys, arguments):
    root, service = project
    with pytest.raises(SystemExit) as exited:
        cli.main(["--project", str(root), *arguments])
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"
    assert service.requests == []


# check --all


def many_files(root: Path, count: int, bad: tuple[int, ...] = ()) -> None:
    (root / "Main.lean").unlink()
    for number in range(count):
        name = f"Bad{number:03}" if number in bad else f"F{number:03}"
        (root / f"{name}.lean").write_text(f"theorem t{number} : True := trivial\n")


def test_check_all_splits_the_project_into_batches_and_combines_them(project, capsys):
    root, service = project
    many_files(root, 130)
    run(root, capsys, "connect")
    code, report = run(root, capsys, "check", "--all")
    assert code == 0 and report["success"] is True
    batches = [
        json.loads(r.content)["payload"]["files"]
        for r in service.requests
        if r.method == "POST" and r.url.path.endswith(("/run", "/operations"))
    ]
    assert [len(batch) for batch in batches] == [64, 64, 2]
    assert sorted(name for batch in batches for name in batch) == sorted(
        f"F{n:03}.lean" for n in range(130)
    )
    assert (report["files"], report["passed"], report["failed"]) == (130, 130, 0)
    assert len(report["results"]) == 130 and len(report["operations"]) == 3
    assert all("result" not in operation for operation in report["operations"])


def test_check_all_fails_when_any_file_fails(project, capsys):
    root, _ = project
    many_files(root, 70, bad=(66,))
    run(root, capsys, "connect")
    code, report = run(root, capsys, "check", "--all", "--draft")
    assert code == 1 and report["success"] is False and report["strict"] is False
    assert report["failed"] == 1 and report["passed"] == 69


def test_check_all_counts_a_failed_operation_as_not_checked(project, capsys):
    root, service = project
    many_files(root, 70)
    run(root, capsys, "connect")
    plain = service._results

    def first_fails(body: dict[str, Any]) -> dict[str, Any]:
        result = plain(body)
        return {"status": "error"} if service.batches == 1 else result

    service.result_for = first_fails
    code, report = run(root, capsys, "check", "--all")
    # Nothing was refused, but 64 files were left without a verdict.
    assert code == 4 and report["not_checked"] == 64 and report["passed"] == 6
    assert (report["status"], report["success"], report["decided"]) == (
        "incomplete",
        False,
        False,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_counts",
        "wrong_status",
        "wrong_file",
        "duplicate_file",
        "missing_file",
        "invalid_entry",
        "invalid_status",
    ],
)
def test_check_all_does_not_accept_inconsistent_batch_results(project, capsys, fault):
    root, service = project
    many_files(root, 70)
    run(root, capsys, "connect")
    plain = service._results

    def inconsistent(body):
        result = plain(body)
        if service.batches != 1:
            return result
        if fault == "wrong_counts":
            result["results"][0]["status"] = "error"
        elif fault == "wrong_status":
            result["status"] = "incomplete"
        elif fault == "wrong_file":
            result["results"][0]["file"] = "Other.lean"
        elif fault == "duplicate_file":
            result["results"][0]["file"] = result["results"][1]["file"]
        elif fault == "missing_file":
            del result["results"][0]["file"]
        elif fault == "invalid_entry":
            result["results"][0] = None
        elif fault == "invalid_status":
            result["results"][0]["status"] = ["ok"]
        return result

    service.result_for = inconsistent
    code, report = run(root, capsys, "check", "--all")
    assert code == 4
    assert (report["status"], report["success"], report["decided"]) == ("incomplete", False, False)
    assert report["passed"] == 6 and report["not_checked"] == 64
    assert {entry["file"] for entry in report["results"]} == {
        f"F{n:03}.lean" for n in range(64, 70)
    }


def test_check_all_preserves_valid_partial_results_in_any_order(project, capsys):
    root, service = project
    many_files(root, 70)
    run(root, capsys, "connect")
    plain = service._results

    def partial(body):
        result = plain(body)
        if service.batches == 1:
            result.update(status="incomplete", passed=63, timed_out=1)
            result["results"][0]["status"] = "timeout"
        result["results"].reverse()
        return result

    service.result_for = partial
    code, report = run(root, capsys, "check", "--all")
    assert code == 4 and report["success"] is False and report["decided"] is False
    assert (report["passed"], report["timed_out"], report["not_checked"]) == (69, 1, 0)
    assert len(report["results"]) == 70


def test_check_all_reports_an_operation_that_outlived_its_wait(project, capsys, monkeypatch):
    root, service = project
    monkeypatch.setattr(session_module, "_BLOCK_GRACE_SECONDS", 0.5)
    many_files(root, 70)
    run(root, capsys, "connect")
    service.pending_polls = 1_000_000
    code, report = run(root, capsys, "check", "--all", "--execution-timeout", "1")
    assert code == 3 and report["success"] is None and report["running"]
    assert report["not_checked"] == 70 and service.batches == 1


def test_check_all_needs_files(tmp_path):
    session = ProjectSession(LeanWarpCloud(TEST_KEY, base_url="https://api.example"), tmp_path)
    with pytest.raises(UsageError, match=r"no \.lean files"):
        session.check_all()


# connect --ephemeral / stop --delete


def test_an_ephemeral_workspace_leaves_nothing_behind(project, capsys):
    root, service = project
    assert run(root, capsys, "connect", "--ephemeral")[0] == 0
    assert run(root, capsys, "check", "Main.lean")[0] == 0
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 0 and result == {
        "status": "deleted",
        "workspace_id": "w",
        "deleted": True,
        "disconnected": True,
        "recovery_required": False,
        "errors": [],
    }
    assert service.deleted == ["w"]
    journal = json.loads((root / ".leanwarp/session.json").read_text())
    assert "workspace_id" not in journal and journal["pending"] is None
    assert not (root / ".leanwarp/ephemeral.json").exists()


def test_stop_delete_refuses_a_workspace_that_is_not_ephemeral(project, capsys):
    root, service = project
    run(root, capsys, "connect")
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 2 and "connect --ephemeral" in result["message"]
    assert service.deleted == []


def test_stop_delete_cancels_a_running_operation_first(project, capsys):
    root, service = project
    run(root, capsys, "connect", "--ephemeral")
    service.pending_polls = 1_000_000
    submitted = run(root, capsys, "check", "Main.lean", "--wait", "0")[1]
    assert run(root, capsys, "stop", "--delete")[0] == 0
    assert service.cancelled == [submitted["operation_id"]] and service.deleted == ["w"]


def test_stop_delete_resolves_a_lost_creation_before_deleting(project, capsys):
    root, service = project
    service.lose = "/workspaces"
    assert run(root, capsys, "connect", "--ephemeral")[0] == 2
    assert run(root, capsys, "stop", "--delete")[0] == 0
    assert service.workspace_count == 1 and service.deleted == ["w"]


def test_ephemeral_is_only_for_a_workspace_this_project_creates(project, capsys):
    root, _ = project
    run(root, capsys, "connect")
    code, result = run(root, capsys, "connect", "--ephemeral")
    assert code == 2 and "lasting workspace" in result["message"]
    session = ProjectSession(cli.load_client(interface="python"), root)
    with pytest.raises(UsageError):
        session.connect(workspace_id="w", ephemeral=True)
    with pytest.raises(SessionError):
        session.stop(delete=True)


def test_the_mark_deletes_only_the_workspace_it_was_created_for(project, capsys):
    root, service = project
    run(root, capsys, "connect", "--ephemeral")
    assert json.loads((root / ".leanwarp/ephemeral.json").read_text()) == {"workspace_id": "w"}
    # The project now names another workspace, however that happened.
    journal_path = root / ".leanwarp/session.json"
    journal = json.loads(journal_path.read_text())
    journal_path.write_text(json.dumps({**journal, "workspace_id": "shared"}))
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 2 and "connect --ephemeral" in result["message"]
    assert service.deleted == []


def marks(root: Path) -> list[Path]:
    """Every ephemeral mark this project's folder holds, whatever its name."""
    return sorted((root / ".leanwarp").glob("ephemeral*"))


def test_a_mark_that_names_no_workspace_authorises_nothing(project, capsys):
    root, service = project
    run(root, capsys, "connect")
    # An empty mark, such as an earlier release wrote, under any name it may have.
    for name in ("ephemeral", "ephemeral.json"):
        (root / ".leanwarp" / name).write_text("")
    assert run(root, capsys, "stop", "--delete")[0] == 2
    assert service.deleted == []


def serving_shared(service: CIService, monkeypatch) -> None:
    """Let the fake also serve a second workspace, `shared`, with the same files."""
    handle = service.handle

    def handled(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/workspaces/shared"):
            return httpx.Response(
                200, json={"workspace_id": "shared", "state": "stopped", "bundle_id": "b"}
            )
        if request.method == "GET" and request.url.path.endswith("/workspaces/shared/files"):
            listed = handle(request).json()
            return httpx.Response(200, json={**listed, "workspace_id": "shared"})
        if request.method == "GET" and request.url.path.endswith("/workspaces/w"):
            return httpx.Response(200, json={"workspace_id": "w", "state": "stopped"})
        return handle(request)

    monkeypatch.setattr(service, "handle", handled)


def test_a_mark_left_from_an_earlier_workspace_authorises_nothing(project, capsys, monkeypatch):
    root, service = project
    serving_shared(service, monkeypatch)
    run(root, capsys, "connect", "--ephemeral")
    left = {path.name: path.read_bytes() for path in marks(root)}
    assert left
    assert run(root, capsys, "disconnect")[0] == 0
    assert marks(root) == []
    attached = run(root, capsys, "connect", "--workspace", "shared")
    assert attached[0] == 0, attached
    # The earlier workspace's mark comes back, as a restored or copied folder would bring it.
    for name, content in left.items():
        (root / ".leanwarp" / name).write_bytes(content)
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 2 and "connect --ephemeral" in result["message"]
    assert service.deleted == []


def outage(service: CIService, ending: str):
    handle = service.handle

    def failing(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and request.url.path.endswith(ending):
            service.requests.append(request)
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "no"}})
        return handle(request)

    return failing


def test_a_resend_that_fails_still_stops_the_worker_and_keeps_the_request(
    project, capsys, monkeypatch
):
    root, service = project
    run(root, capsys, "connect", "--ephemeral")
    service.lose = "/run"
    assert run(root, capsys, "check", "Main.lean")[0] == 2
    monkeypatch.setattr(service, "handle", outage(service, "/run"))
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 2 and result["status"] == "stopped" and result["deleted"] is False
    assert result["recovery_required"] is True
    assert [error["step"] for error in result["errors"]] == ["resend"]
    assert any(r.url.path.endswith("/w/stop") for r in service.requests)
    assert service.deleted == []
    assert json.loads((root / ".leanwarp/session.json").read_text())["pending"] is not None


def test_a_refused_deletion_stops_the_worker_again(project, capsys, monkeypatch):
    root, service = project
    run(root, capsys, "connect", "--ephemeral")
    handle = service.handle

    def busy(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(409, json={"error": {"code": "workspace_busy", "message": "no"}})
        return handle(request)

    monkeypatch.setattr(service, "handle", busy)
    code, result = run(root, capsys, "stop", "--delete")
    assert code == 2 and result["status"] == "stopped"
    assert [error["step"] for error in result["errors"]] == ["delete"]
    stops = [r for r in service.requests if r.url.path.endswith("/stop")]
    assert len(stops) == 2


def test_a_resend_accepted_with_its_response_lost_is_stopped_after_it(project, capsys, monkeypatch):
    root, service = project
    run(root, capsys, "connect", "--ephemeral")
    # The submission never reaches the service, so it stays saved, unsent.
    handle = service.handle
    monkeypatch.setattr(service, "handle", outage(service, "/run"))
    assert run(root, capsys, "check", "Main.lean")[0] == 2
    monkeypatch.setattr(service, "handle", handle)
    # Tearing down resends it: the service accepts it and the response is lost.
    service.lose = "/run"
    code, result = run(root, capsys, "stop", "--delete")
    accepted = [i for i, r in enumerate(service.requests) if r.url.path.endswith("/run")]
    stops = [i for i, r in enumerate(service.requests) if r.url.path.endswith("/stop")]
    assert service.operation["kind"] == "check" and len(accepted) == 2
    # A stop came after the resend that started the work, and it is what is reported.
    assert stops[-1] > accepted[-1] and len(stops) == 2
    assert code == 2 and result["status"] == "stopped" and result["recovery_required"] is True
    assert service.deleted == []


@pytest.mark.parametrize(
    ("entries", "status", "code"),
    [
        (["ok", "ok"], "ok", 0),
        # A refusal is the aggregate's verdict, whatever else was undecided.
        (["error", "timeout"], "failed", 1),
        (["ok", "timeout"], "incomplete", 4),
        (["ok", "infrastructure_failure"], "incomplete", 4),
    ],
)
def test_check_all_counts_every_status_and_exits_on_the_decision(
    project, capsys, entries, status, code
):
    root, service = project
    many_files(root, len(entries))
    run(root, capsys, "connect")
    counted = {
        "ok": "passed",
        "error": "failed",
        "rejected": "failed",
        "timeout": "timed_out",
        "infrastructure_failure": "infrastructure_failures",
        "not_checked": "not_checked",
    }

    def batch(body: dict[str, Any]) -> dict[str, Any]:
        if body["kind"] != "check_files":
            return {"status": "ok", "diagnostics": []}
        files = body["payload"]["files"]
        counts = dict.fromkeys(counted.values(), 0)
        for entry in entries:
            counts[counted[entry]] += 1
        return {
            "status": status,
            **counts,
            "results": [
                {"file": name, "status": entry} for name, entry in zip(files, entries, strict=True)
            ],
        }

    service.result_for = batch
    exit_code, report = run(root, capsys, "check", "--all")
    assert exit_code == code and report["status"] == status
    totals = [report[name] for name in sorted(set(counted.values()))]
    assert sum(totals) == report["files"] == len(entries)
    assert report["timed_out"] == entries.count("timeout")
    assert report["infrastructure_failures"] == entries.count("infrastructure_failure")

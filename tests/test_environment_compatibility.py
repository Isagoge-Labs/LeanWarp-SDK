from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import contextmanager, nullcontext

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, ProjectSession
from leanwarp_cloud.project import (
    ProjectError,
    ProjectIdentity,
    choose_environment,
    project_identity,
)

from .test_project import CATALOG, MANIFEST, TOOLCHAIN, _build
from .test_session import Service


@pytest.mark.parametrize("command", ["recover", "cancel"])
@pytest.mark.parametrize(
    "state,status,code,success",
    [
        ("completed", "ok", 0, True),
        ("completed", "rejected", 1, False),
        ("failed", None, 1, False),
        ("cancelled", None, 1, False),
        ("queued", None, 3, None),
    ],
)
def test_cli_recovery_and_cancellation_preserve_operation_outcomes(
    monkeypatch, capsys, command, state, status, code, success
):
    from leanwarp_cloud import cli

    operation = {"operation_id": "o", "revision": 1, "state": state, "kind": "check"}
    if status:
        operation["result"] = {
            "operation_id": "o",
            "revision": 1,
            "generation": 1,
            "result": {"status": status},
        }

    class Session:
        def recover(self):
            return operation

        def cancel(self):
            return operation

    monkeypatch.setattr(cli, "load_client", lambda: nullcontext(None))
    monkeypatch.setattr(cli, "ProjectSession", lambda *_: Session())
    assert cli.main([command]) == code
    assert json.loads(capsys.readouterr().out) == {"success": success, **operation}


def test_cli_recover_keeps_non_operation_receipts(monkeypatch, capsys):
    from leanwarp_cloud import cli

    class Session:
        def recover(self):
            return {"revision": 2, "workspace_id": "w"}

    monkeypatch.setattr(cli, "load_client", lambda: nullcontext(None))
    monkeypatch.setattr(cli, "ProjectSession", lambda *_: Session())
    assert cli.main(["recover"]) == 0
    assert json.loads(capsys.readouterr().out) == {"revision": 2, "workspace_id": "w"}


def test_cli_preserves_error_codes_and_recovery_fields(monkeypatch, capsys):
    from leanwarp_cloud import cli
    from leanwarp_cloud.client import OperationTimeout
    from leanwarp_cloud.session import SessionError

    def failure(error):
        def load():
            raise error

        return load

    monkeypatch.setattr(cli, "load_client", failure(OperationTimeout("o")))
    assert cli.main(["wait"]) == 3
    timeout = json.loads(capsys.readouterr().out)
    assert timeout["error"] == "poll_timeout"
    assert timeout["operation_id"] == "o" and timeout["running"] is True
    monkeypatch.setattr(cli, "load_client", failure(httpx.ConnectError("private-sentinel")))
    assert cli.main(["account"]) == 2
    lost = json.loads(capsys.readouterr().out)
    assert lost["error"] == "transport_error"
    assert lost["action"] == "recover any pending request; do not resubmit it"
    assert "private-sentinel" not in str(lost)
    monkeypatch.setattr(cli, "load_client", failure(SessionError("run connect first")))
    assert cli.main(["account"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "local_error"


def test_cli_preserves_auth_status_tokens(tmp_path, monkeypatch, capsys):
    from leanwarp_cloud import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: "private-test-key")
    saved = []
    monkeypatch.setattr(cli, "save_credentials", saved.append)
    assert cli.main(["auth", "login"]) == 0
    assert saved == ["private-test-key"]
    assert json.loads(capsys.readouterr().out) == {"status": "authenticated"}
    assert cli.main(["auth", "logout"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "local_credentials_removed",
        "key_revoked": False,
    }


@contextmanager
def api():
    service = Service()
    bound = {}

    def handle(request):
        if request.url.path.endswith("/versions"):
            service.requests.append(request)
            return httpx.Response(200, json={"versions": CATALOG})
        if request.method == "GET" and request.url.path.endswith("/workspaces/w"):
            service.requests.append(request)
            return httpx.Response(
                200, json={"workspace_id": "w", "revision": service.revision, **bound}
            )
        if request.method == "POST" and request.url.path.endswith("/workspaces"):
            selected = json.loads(request.content)["bundle_id"]
            bound.update(next(build for build in CATALOG if build["bundle_id"] == selected))
        return service.handle(request)

    with LeanWarpCloud(
        "secret", base_url="https://api.example", retries=0, transport=httpx.MockTransport(handle)
    ) as cloud:
        yield service, cloud


def pinned(root):
    (root / "lean-toolchain").write_text(TOOLCHAIN)
    (root / "lake-manifest.json").write_bytes(MANIFEST)
    (root / "Main.lean").write_text("example : True := by trivial")


@pytest.mark.parametrize("entry", ["client", "session"])
def test_public_bundle_keyword_keeps_strict_matching(tmp_path, entry):
    pinned(tmp_path)
    with api() as (service, cloud):
        create = (
            cloud.create_workspace_for_project
            if entry == "client"
            else ProjectSession(cloud, tmp_path).connect
        )
        args = (tmp_path,) if entry == "client" else ()
        with pytest.raises(ProjectError, match="No LeanWarp environment matches"):
            create(*args, bundle_id=CATALOG[1]["bundle_id"])
        assert service.workspace_count == 0
        assert create(*args, bundle_id=CATALOG[0]["bundle_id"])["workspace_id"] == "w"


@pytest.mark.parametrize("entry", ["client", "session"])
def test_conflicting_selectors_fail_before_requests_or_local_state(tmp_path, entry):
    with api() as (service, cloud):
        create = (
            cloud.create_workspace_for_project
            if entry == "client"
            else ProjectSession(cloud, tmp_path).connect
        )
        args = (tmp_path,) if entry == "client" else ()
        with pytest.raises(ProjectError, match="either environment"):
            create(*args, environment="", bundle_id=CATALOG[0]["bundle_id"])
        assert service.requests == []
        assert not (tmp_path / ".leanwarp").exists()


def test_explicit_environment_override_remains_available(tmp_path):
    pinned(tmp_path)
    with api() as (_, cloud):
        session = ProjectSession(cloud, tmp_path)
        result = session.connect(environment="lean-4.34-mathlib")
        assert result["environment_choice"]["matches_project"] is False
        assert session.doctor()["environment"]["matches_project"] is False
        assert session.doctor()["compatible"] is True


def test_doctor_reports_existing_choice_without_writes_or_reselection(tmp_path):
    (tmp_path / "Main.lean").write_text("example : True := by trivial")
    with api() as (service, cloud):
        session = ProjectSession(cloud, tmp_path)
        session.connect(environment="lean-4.26-mathlib")
        before = session.path.read_bytes()
        service.requests.clear()
        report = session.doctor()
        assert report["environment"]["environment"] == "lean-4.26-mathlib"
        assert report["environment"]["match"] == "connected"
        assert report["compatible"] is True
        assert all(request.method == "GET" for request in service.requests)
        assert session.path.read_bytes() == before
        requested = session.doctor(environment="lean-4.34-mathlib")
        assert requested["compatible"] is False
        assert requested["environment"]["environment"] == "lean-4.26-mathlib"


def test_doctor_requires_reconnect_when_metadata_changes(tmp_path):
    pinned(tmp_path)
    with api() as (_, cloud):
        session = ProjectSession(cloud, tmp_path)
        session.connect(environment="lean-4.34-mathlib")
        (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.34.1")
        report = session.doctor()
        assert report["compatible"] is False
        assert "changed" in report["message"]


def test_doctor_requires_recovery_for_uncertain_creation(tmp_path):
    pinned(tmp_path)
    with api() as (service, cloud):
        service.lose = "/workspaces"
        session = ProjectSession(cloud, tmp_path)
        with pytest.raises(httpx.ReadTimeout):
            session.connect()
        (tmp_path / "lake-manifest.json").write_text("damaged after the request")
        before = session.path.read_bytes()
        report = session.doctor()
        assert report["recovery_required"] and not report["compatible"]
        assert session.path.read_bytes() == before
        assert service.workspace_count == 1


def test_plain_project_can_sync_deletion_of_its_last_file(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial")
    with api() as (service, cloud):
        session = ProjectSession(cloud, tmp_path)
        session.connect()
        session.sync()
        source.unlink()
        session.sync()
        assert service.sources == {}
        assert service.revision == 2
        assert session.doctor()["connected"] is True


def test_newest_stable_release_is_independent_of_build_order():
    identity = ProjectIdentity(None, None, None)
    for catalog in itertools.permutations(CATALOG):
        assert choose_environment(identity, catalog).build == CATALOG[1]
    versions = [
        _build("v" + version, "leanprover/lean4:v" + version)
        for version in ("4.9.0", "4.10.0", "4.34.1", "4.34.10")
    ]
    assert choose_environment(identity, list(reversed(versions))).build == versions[-1]


def test_ambiguous_or_unrankable_environments_require_explicit_choice():
    identity = ProjectIdentity(None, None, None)
    other = {
        **CATALOG[1],
        "environment_id": "other-dependencies",
        "bundle_id": "other-dependencies-" + "a" * 20,
    }
    with pytest.raises(ProjectError, match="unambiguous default"):
        choose_environment(identity, [CATALOG[1], other])
    nightly = [{**build, "lean_toolchain": "leanprover/lean4:nightly"} for build in CATALOG]
    with pytest.raises(ProjectError, match="unambiguous default"):
        choose_environment(identity, nightly)


def test_nested_plain_sources_are_recognised_without_following_links(tmp_path):
    directory = tmp_path / "Algebra" / "Ring"
    directory.mkdir(parents=True)
    (directory / "Basic.lean").write_text("example : True := by trivial")
    assert project_identity(tmp_path) == ProjectIdentity(None, None, None)


def test_cli_preserves_catalog_shape_sync_and_strict_bundle(tmp_path, monkeypatch, capsys):
    from leanwarp_cloud.cli import main

    pinned(tmp_path)
    with api() as (service, cloud):
        monkeypatch.setattr("leanwarp_cloud.cli.load_client", lambda: cloud)
        assert main(["versions"]) == 0
        assert "versions" in json.loads(capsys.readouterr().out)
        # main closes the client after each command; the fixture creates one per command.
    with api() as (service, cloud):
        monkeypatch.setattr("leanwarp_cloud.cli.load_client", lambda: cloud)
        assert (
            main(["--project", str(tmp_path), "connect", "--bundle", CATALOG[1]["bundle_id"]]) == 2
        )
        assert service.workspace_count == 0
        assert "No LeanWarp environment matches" in capsys.readouterr().out


def test_mcp_retains_original_versions_tool_and_envelope(tmp_path):
    from leanwarp_cloud.mcp_server import create_server

    with api() as (_, cloud):
        server = create_server(cloud, str(tmp_path))

        async def call():
            tools = {tool.name for tool in await server.list_tools()}
            assert {"versions", "environments", "doctor"} <= tools
            result = await server.call_tool("versions", {})
            blocks = result[0] if isinstance(result, tuple) else result
            assert json.loads(blocks[0].text)["versions"] == CATALOG

        asyncio.run(call())

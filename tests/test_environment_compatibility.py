from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpTransportError, ProjectSession
from leanwarp_cloud import environment as environment_module
from leanwarp_cloud.environment import project_answers, project_inputs
from leanwarp_cloud.errors import ConnectTimeout, EnvironmentBuildError, LeanWarpCloudError
from leanwarp_cloud.project import ProjectError, newest_toolchain

from .test_session import TOOLCHAIN, Service


@pytest.mark.parametrize("command", ["recover", "cancel"])
@pytest.mark.parametrize(
    "state,status,code,success,decided",
    [
        ("completed", "ok", 0, True, True),
        ("completed", "rejected", 1, False, True),
        # No verdict: the work ended without deciding the proof.
        ("completed", "timeout", 4, False, False),
        ("completed", "infrastructure_failure", 4, False, False),
        ("failed", None, 4, False, False),
        ("cancelled", None, 4, False, False),
        ("queued", None, 3, None, None),
    ],
)
def test_cli_recovery_and_cancellation_preserve_operation_outcomes(
    monkeypatch, capsys, command, state, status, code, success, decided
):
    from leanwarp_cloud import cli

    operation = {"operation_id": "o", "revision": 1, "state": state, "kind": "check"}
    if status:
        operation["result"] = {
            "operation_id": "o",
            "revision": 1,
            "generation": 1,
            "result": {
                "status": status,
                "diagnostics": [],
                "failure": {"kind": "axioms", "retryable": False, "message": "m"},
            },
        }

    class Session:
        def recover(self):
            return operation

        def cancel(self):
            return operation

    monkeypatch.setattr(cli, "load_client", lambda *, interface: nullcontext(None))
    monkeypatch.setattr(cli, "ProjectSession", lambda *_: Session())
    assert cli.main([command]) == code
    assert json.loads(capsys.readouterr().out) == {
        "success": success,
        "decided": decided,
        **operation,
    }


def test_cli_recover_keeps_non_operation_receipts(monkeypatch, capsys):
    from leanwarp_cloud import cli

    class Session:
        def recover(self):
            return {"revision": 2, "workspace_id": "w"}

    monkeypatch.setattr(cli, "load_client", lambda *, interface: nullcontext(None))
    monkeypatch.setattr(cli, "ProjectSession", lambda *_: Session())
    assert cli.main(["recover"]) == 0
    assert json.loads(capsys.readouterr().out) == {"revision": 2, "workspace_id": "w"}


def test_cli_preserves_error_codes_and_recovery_fields(monkeypatch, capsys):
    from leanwarp_cloud import cli
    from leanwarp_cloud.client import OperationTimeout
    from leanwarp_cloud.session import SessionError

    def failure(error):
        def load(*, interface):
            raise error

        return load

    monkeypatch.setattr(cli, "load_client", failure(OperationTimeout("o")))
    assert cli.main(["wait"]) == 3
    timeout = json.loads(capsys.readouterr().out)
    assert timeout["error"] == "poll_timeout"
    assert timeout["operation_id"] == "o" and timeout["running"] is True
    monkeypatch.setattr(cli, "load_client", failure(LeanWarpTransportError("private-sentinel")))
    assert cli.main(["stop"]) == 2
    lost = json.loads(capsys.readouterr().out)
    assert lost["error"] == "transport_error" and lost["retryable"] is True
    # `recover` resends a saved create, sync or submit; it has nothing to say about a stop.
    assert "recover" not in lost["message"] and "run the command again" in lost["message"]
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
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        retries=0,
        transport=httpx.MockTransport(service.handle),
    ) as cloud:
        yield service, cloud


MANIFEST_PACKAGES = [
    {
        "type": "git",
        "name": "mathlib",
        "url": "https://github.com/leanprover-community/mathlib4",
        "rev": "a" * 40,
        "scope": "leanprover-community",
        "inputRev": "master",
        "inherited": False,
    },
    {"type": "path", "name": "sibling", "dir": "../sibling", "inherited": False},
    {"type": "path", "name": "nested", "dir": "vendor/nested", "inherited": False},
]


def lake_project(root):
    """A Lake project with a sibling and a nested path dependency, never built here."""
    project = root / "project"
    (project / "vendor" / "nested" / ".lake" / "build").mkdir(parents=True)
    (project / "vendor" / "nested" / "Nested.lean").write_text("def nested := 1")
    (project / "vendor" / "nested" / ".lake" / "build" / "Nested.olean").write_bytes(b"x")
    (root / "sibling").mkdir()
    (root / "sibling" / "Sibling.lean").write_text("def sibling := 2")
    (project / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (project / "lakefile.toml").write_text('name = "project"\n')
    (project / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES})
    )
    (project / "Main.lean").write_text("example : True := by trivial")
    return project


def test_lake_inputs_are_read_without_lake_and_every_input_changes_the_request(tmp_path):
    project = lake_project(tmp_path)
    first = project_inputs(project / "vendor")
    request = first.request
    assert request["lakefile"]["name"] == "lakefile.toml"
    assert [tree["dir"] for tree in request["path_trees"]] == ["../sibling", "vendor/nested"]
    # A path dependency's whole source tree is named by content; build output is not.
    nested = json.loads(first.blobs[request["path_trees"][1]["sha256"]])
    assert [entry["path"] for entry in nested["files"]] == ["Nested.lean"]
    digests = {first.digest}
    # Changing only a locked pin, or only its effective override, changes the request.
    pinned = [{**MANIFEST_PACKAGES[0], "rev": "b" * 40}, *MANIFEST_PACKAGES[1:]]
    (project / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": pinned})
    )
    digests.add(project_inputs(project).digest)
    (project / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES})
    )
    (project / ".lake").mkdir()
    (project / ".lake" / "package-overrides.json").write_text(
        json.dumps({"version": "1.1.0", "packages": [{**MANIFEST_PACKAGES[0], "rev": "c" * 40}]})
    )
    digests.add(project_inputs(project).digest)
    (tmp_path / "sibling" / "Sibling.lean").write_text("def sibling := 3")
    digests.add(project_inputs(project).digest)
    assert len(digests) == 4
    # A file whose bytes are another tree's listing still waits for that listing's
    # files: every blob is uploaded after each digest it names.
    nested_listing = project_inputs(project).blobs[
        project_inputs(project).request["path_trees"][1]["sha256"]
    ]
    (tmp_path / "sibling" / "Listing.json").write_bytes(nested_listing)
    inputs = project_inputs(project)
    order = list(inputs.blobs)
    for tree in inputs.request["path_trees"]:
        for item in json.loads(inputs.blobs[tree["sha256"]])["files"]:
            assert order.index(item["sha256"]) < order.index(tree["sha256"])
    # A filesystem git dependency: Lake's checkout of it, at the locked commit (here
    # through a branch in the packed refs), is uploaded whole, never its repository.
    (project / ".lake" / "package-overrides.json").unlink()
    rev = "e" * 40
    checkout = project / ".lake" / "packages" / "mine"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (checkout / ".git" / "packed-refs").write_text(f"# pack-refs\n{rev} refs/heads/main\n")
    (checkout / ".lake").mkdir()
    (checkout / ".lake" / "Mine.olean").write_bytes(b"built")
    (checkout / "Mine.lean").write_text("def mine := 1")
    local = {"type": "git", "name": "mine", "url": "/home/me/mine", "rev": rev, "scope": ""}
    (project / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": [*MANIFEST_PACKAGES, local]})
    )
    inputs = project_inputs(project)
    (tree,) = [item for item in inputs.request["path_trees"] if item["dir"].startswith(".lake")]
    assert tree["dir"] == ".lake/packages/mine"
    listed = json.loads(inputs.blobs[tree["sha256"]])["files"]
    assert [item["path"] for item in listed] == ["Mine.lean"]
    # A submodule's own `.git` file is repository metadata too, never uploaded.
    (checkout / "sub").mkdir()
    (checkout / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
    (checkout / "sub" / "Sub.lean").write_text("def sub := 1")
    inputs = project_inputs(project)
    (tree,) = [item for item in inputs.request["path_trees"] if item["dir"].startswith(".lake")]
    listed = json.loads(inputs.blobs[tree["sha256"]])["files"]
    assert [item["path"] for item in listed] == ["Mine.lean", "sub/Sub.lean"]
    # Lake writes an escaped name in the lock, and keeps the checkout unescaped.
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {"version": "1.1.0", "packages": [*MANIFEST_PACKAGES, {**local, "name": "«mine»"}]}
        )
    )
    assert ".lake/packages/mine" in {
        item["dir"] for item in project_inputs(project).request["path_trees"]
    }
    # A lock entry's name is one path component: it never leads a read elsewhere.
    for name in ("../../elsewhere", "a/b", ".."):
        (project / "lake-manifest.json").write_text(
            json.dumps(
                {"version": "1.1.0", "packages": [*MANIFEST_PACKAGES, {**local, "name": name}]}
            )
        )
        with pytest.raises(ProjectError, match="name"):
            project_inputs(project)
    # A copied path dependency is Lake's copy, which Lake keeps using whatever
    # becomes of the original: that copy is what is uploaded.
    copied = {"type": "path", "name": "cpy", "dir": "../gone", "copy": True, "scope": ""}
    (project / ".lake" / "packages" / "cpy").mkdir()
    (project / ".lake" / "packages" / "cpy" / "Cpy.lean").write_text("def cpy := 1")
    (project / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": [*MANIFEST_PACKAGES, local, copied]})
    )
    inputs = project_inputs(project)
    trees = {item["dir"]: item["sha256"] for item in inputs.request["path_trees"]}
    assert "../gone" not in trees
    assert [
        item["path"] for item in json.loads(inputs.blobs[trees[".lake/packages/cpy"]])["files"]
    ] == ["Cpy.lean"]
    # Checked out at another commit than the lock pins: Lake must update it first.
    (checkout / ".git" / "HEAD").write_text("f" * 40 + "\n")
    with pytest.raises(ProjectError, match="another commit"):
        project_inputs(project)


def test_lake_inputs_refuse_what_lake_would_not_build_from(tmp_path, monkeypatch):
    project = lake_project(tmp_path)
    (project / "vendor" / "nested" / "Link.lean").symlink_to(project / "Main.lean")
    with pytest.raises(ProjectError, match="is a link"):
        project_inputs(project)
    (project / "vendor" / "nested" / "Link.lean").unlink()
    # A credential in a package URL never leaves the machine, from either document
    # sent as read, a path dependency's own manifest, nor any lakefile; the refusal
    # never repeats it.
    manifest = project / "lake-manifest.json"
    overrides = project / ".lake" / "package-overrides.json"
    overrides.parent.mkdir()
    own = tmp_path / "sibling" / "lake-manifest.json"
    secret = "s3cr3t-placeholder"
    for url in (
        f"https://user:{secret}@github.com/o/r",
        f"https://{secret}@github.com/o/r",
        f"https://github.com/o/r?token={secret}",
        f"ssh://git:{secret}@github.com/o/r",
        f"git:{secret}@github.com:o/r",
    ):
        for document, label in (
            (manifest, "lake-manifest.json"),
            (overrides, ".lake/package-overrides.json"),
            (own, "../sibling/lake-manifest.json"),
        ):
            packages = [{**MANIFEST_PACKAGES[0], "url": url}, *MANIFEST_PACKAGES[1:]]
            document.write_text(json.dumps({"version": "1.1.0", "packages": packages}))
            with pytest.raises(ProjectError, match="mathlib a URL with a credential") as error:
                project_inputs(project)
            assert str(error.value).startswith(f"{label} gives") and secret not in str(error.value)
            document.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    # A key repeated in either document: the request sends the value JSON would drop.
    for document in (manifest, overrides):
        document.write_text(
            '{"version": "1.1.0", "packages": [{"type": "git", "name": "m", '
            f'"url": "https://user:{secret}@github.com/o/r", "url": "https://github.com/o/r"'
            ', "rev": "' + "a" * 40 + '"}]}'
        )
        with pytest.raises(ProjectError, match="not valid JSON") as error:
            project_inputs(project)
        assert document.name in str(error.value)
        document.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    for url in ("ssh://git@github.com/o/r", "git@github.com:o/r", "https://github.com/o/r#a?b"):
        packages = [{**MANIFEST_PACKAGES[0], "url": url}, *MANIFEST_PACKAGES[1:]]
        manifest.write_text(json.dumps({"version": "1.1.0", "packages": packages}))
        assert project_inputs(project).request["manifest"] == manifest.read_text()
    overrides.unlink()
    lakefile = project / "lakefile.toml"
    dependency = tmp_path / "sibling" / "lakefile.toml"
    # A dependency inside another, whose own rules leave its Lake files out: the
    # enclosing tree uploads them, so they are checked there.
    inner = tmp_path / "sibling" / "inner"
    custom = inner / "config" / "Build.lean"
    custom.parent.mkdir(parents=True)
    # And the lakefile one names in a nested tree, which only that tree uploads.
    (inner / ".gitignore").write_text("/lake-manifest.json\n")
    (tmp_path / "sibling" / ".gitignore").write_text("/inner/config/\n")
    named = {**MANIFEST_PACKAGES[1], "configFile": "../sibling/inner/config/Build.lean"}
    inner_entry = {"type": "path", "name": "inner", "dir": "../sibling/inner"}
    packages = [MANIFEST_PACKAGES[0], named, MANIFEST_PACKAGES[2], inner_entry]
    manifest.write_text(json.dumps({"version": "1.1.0", "packages": packages}))
    for url in (
        f"https://user:{secret}@github.com/o/r",
        f"http://{secret}@github.com/o/r",
        f"https://user:it's-{secret}@github.com/o/r",
        # A token in the query, with every manifest clean.
        f"https://github.com/o/r?token={secret}",
    ):
        # The project's lakefile, and a path dependency's, under the name its lock
        # entry gives too, which its tree uploads; and its manifest, if not one.
        for written, label in (
            (lakefile, "lakefile.toml"),
            (dependency, "../sibling/lakefile.toml"),
            (custom, "../sibling/inner/config/Build.lean"),
            (own, "../sibling/lake-manifest.json"),
            (inner / "lake-manifest.json", "../sibling/inner/lake-manifest.json"),
        ):
            written.write_text(f'name = "project"\n[[require]]\nname = "x"\ngit = "{url}"\n')
            with pytest.raises(ProjectError, match=re.escape(f"{label} has a URL")) as error:
                project_inputs(project)
            assert "on line 4" in str(error.value) and secret not in str(error.value)
            written.write_text('name = "project"\n')
    # Nested too deep to parse, it is still scanned as text.
    own.write_text("[" * 100_000 + f'"https://user:{secret}@github.com/o/r"')
    with pytest.raises(ProjectError, match=re.escape("../sibling/lake-manifest.json has a URL")):
        project_inputs(project)
    # Named as the lakefile too, the manifest still gets its per-package check.
    packages[1] = {**named, "configFile": "lake-manifest.json"}
    manifest.write_text(json.dumps({"version": "1.1.0", "packages": packages}))
    query = [{**MANIFEST_PACKAGES[0], "url": f"https://user:{secret}@github.com/o/r"}]
    own.write_text(json.dumps({"version": "1.1.0", "packages": query}))
    with pytest.raises(ProjectError, match=re.escape("../sibling/lake-manifest.json gives")):
        project_inputs(project)
    # An absolute configFile is the file it names, however its root is spelled.
    own.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    packages[1] = {**named, "configFile": f"/{custom}"}
    manifest.write_text(json.dumps({"version": "1.1.0", "packages": packages}))
    custom.write_text(f'git = "https://user:{secret}@github.com/o/r"\n')
    with pytest.raises(ProjectError, match=re.escape(f"{custom} has a URL")):
        project_inputs(project)
    shutil.rmtree(inner)
    for written in (dependency, own, tmp_path / "sibling" / ".gitignore"):
        written.unlink()
    manifest.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    # Nothing outside the declared trees is read: not an absolute path, not a link.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.lean").write_text("secret")
    (project / "vendor" / "linked").symlink_to(outside, target_is_directory=True)
    for directory, message in (
        (str(outside), "must be a relative directory"),
        ("vendor/linked", "through a link"),
    ):
        path = [{"type": "path", "name": "x", "dir": directory, "inherited": False}]
        manifest.write_text(json.dumps({"version": "1.1.0", "packages": path}))
        with pytest.raises(ProjectError, match=message):
            project_inputs(project)
    # A tree that changes while it is read is refused, not half captured.
    manifest.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    real = environment_module._walk
    sibling = tmp_path / "sibling"

    def same_size_edit():
        # Same size and time, new bytes: a change only the inode change time shows.
        path = sibling / "Sibling.lean"
        times = os.stat(path)
        path.write_text("def sibling := 9")
        os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))

    def replaced_directory():
        # The declared directory is swapped for an identical copy between the walks.
        (sibling / "Sibling.lean").rename(tmp_path / "Moved.lean")
        sibling.rename(tmp_path / "old-sibling")
        sibling.mkdir()
        (tmp_path / "Moved.lean").rename(sibling / "Sibling.lean")

    def pin_edit():
        # The lock changes while a dependency is captured: nothing of the old one is sent.
        manifest.write_text(manifest.read_text().replace("a" * 40, "f" * 40))

    def unsearchable_lake():
        # The overrides' directory can no longer be searched: a project error.
        (project / ".lake").chmod(0)

    (project / ".lake").mkdir(exist_ok=True)
    for change, during in (
        (unsearchable_lake, "capture"),
        (pin_edit, "capture"),
        (same_size_edit, "capture"),
        (replaced_directory, "capture"),
        # Swapped once the second walk holds its own descriptor: the final reopen
        # finds another directory at the declared path.
        (replaced_directory, "verification"),
    ):

        def racing(
            descriptor, prefix, dependency, found, captured, *rest, change=change, during=during
        ):
            real(descriptor, prefix, dependency, found, captured, *rest)
            walk = "capture" if captured is not None else "verification"
            if walk == during and dependency == "../sibling" and not prefix:
                # Kernels without fine-grained timestamps stamp at their clock tick:
                # let one pass, so an edit never shares the time of what it changes.
                time.sleep(0.05)
                change()

        monkeypatch.setattr(environment_module, "_walk", racing)
        try:
            with pytest.raises(ProjectError, match="changed"):
                project_inputs(project)
        finally:
            (project / ".lake").chmod(0o755)
    # An ignore file that leaves itself out still holds both walks to one set of
    # rules: rewritten between them with the same bytes, it shows.
    rules = sibling / ".gitignore"
    rules.write_text(".gitignore\n")

    def rules_rewritten(descriptor, prefix, dependency, found, captured, *rest):
        real(descriptor, prefix, dependency, found, captured, *rest)
        if captured is not None and dependency == "../sibling" and not prefix:
            time.sleep(0.05)
            rules.write_text(".gitignore\n")

    monkeypatch.setattr(environment_module, "_walk", rules_rewritten)
    with pytest.raises(ProjectError, match="changed"):
        project_inputs(project)
    rules.unlink()
    # Where file times are coarse (one tick for a whole connect), a same-size edit of
    # a configuration file leaves its stamp unchanged; its content still differs.
    real_identity = environment_module._identity
    monkeypatch.setattr(
        environment_module,
        "_identity",
        lambda status: real_identity(status)._replace(modified_ns=0, changed_ns=0),
    )

    def same_size_pin_edit(descriptor, prefix, dependency, found, captured, *rest):
        real(descriptor, prefix, dependency, found, captured, *rest)
        if captured is not None and dependency == "../sibling" and not prefix:
            manifest.write_text(manifest.read_text().replace("a" * 40, "b" * 40))

    manifest.write_text(json.dumps({"version": "1.1.0", "packages": MANIFEST_PACKAGES}))
    assert "a" * 40 in manifest.read_text()
    monkeypatch.setattr(environment_module, "_walk", same_size_pin_edit)
    with pytest.raises(ProjectError, match="changed"):
        project_inputs(project)
    monkeypatch.undo()
    # No component of the path is followed through a link, ancestors included.
    (tmp_path / "linked-project").symlink_to(project, target_is_directory=True)
    with pytest.raises(ProjectError, match="through a link"):
        environment_module._open_path((tmp_path / "linked-project").parts[1:], "the project")
    (project / "lake-manifest.json").unlink()
    with pytest.raises(ProjectError, match="lake update"):
        project_inputs(project)
    # Plain files outside any Lake project run on the newest supported toolchain.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Main.lean").write_text("example : True := by trivial")
    assert project_inputs(plain, default_toolchain=lambda: TOOLCHAIN).request == {
        "lean_toolchain": TOOLCHAIN
    }
    with pytest.raises(ProjectError, match="lean-toolchain"):
        project_inputs(plain)
    assert (
        newest_toolchain(
            ["leanprover/lean4:v4.9.0", "leanprover/lean4:v4.34.10", "leanprover/lean4:nightly"]
        )
        == "leanprover/lean4:v4.34.10"
    )


def test_cli_rejoins_a_long_build_after_its_first_wait_expires(tmp_path, monkeypatch, capsys):
    from leanwarp_cloud import cli

    project = lake_project(tmp_path)
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    with api() as (service, cloud):
        monkeypatch.setattr(cli, "load_client", lambda **_: nullcontext(cloud))
        ready = service.environment
        service.environment = {**ready, "state": "building", "phase": "compiling"}
        # The first call elects to leave after one second.
        monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
        argv = ["--project", str(project), "connect"]
        assert cli.main([*argv, "--timeout", "1"]) == 3
        outcome = json.loads(capsys.readouterr().out)
        assert outcome["error"] == "connect_timeout" and "connect again" in outcome["message"]
        assert "operation_id" not in outcome
        assert now[0] == 1001

        # The same project rejoins. The shared build finishes after 102 minutes.
        def finish(_seconds):
            now[0] += 102 * 60
            service.environment = ready

        monkeypatch.setattr(time, "sleep", finish)
        assert cli.main(argv) == 0
        assert (
            json.loads(capsys.readouterr().out)["environment"]["environment_id"]
            == ready["environment_id"]
        )
        assert all(body == service.environment_requests[0] for body in service.environment_requests)
        assert not any(r.url.path.endswith(("/retry", "/cancel")) for r in service.requests)


def test_connect_uploads_only_missing_inputs_and_waits_for_a_build(tmp_path):
    project = lake_project(tmp_path)
    # Its build may read the project's parent: the owner names it.
    (project / ".leanwarpread").write_text("# read by the lakefile\n..\n")
    with api() as (service, cloud):
        building = {**service.environment, "state": "building", "phase": "compiling"}
        polls = iter([building, building, service.environment])
        ready = service.environment
        service.environment = building

        def advance(request):
            nonlocal ready
            if request.method == "GET" and "/environments/" in request.url.path:
                service.environment = next(polls)
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(advance)
        seen: list[Mapping[str, Any]] = []
        result = cloud.connect_environment(project, poll_seconds=0, progress=seen.append)
        assert result["state"] == "ready" and result["environment_id"] == ready["environment_id"]
        # The waiting caller is told each change in the build once, not each poll.
        assert [item["phase"] for item in seen] == ["compiling"]
        # Different reads asked in the same phase are a change too; an observer that
        # fails changes nothing about the connect.
        asked = [{**building, "input_requests": [path]} for path in ("../a.json", "../b.json")]
        polls = iter([*asked, ready])
        service.environment = building
        reports: list[tuple[str, ...]] = []

        def observer(environment):
            reports.append(environment["input_requests"])
            # Read-only all the way down: nothing it does reaches the exchange.
            with pytest.raises((TypeError, AttributeError)):
                environment["dependencies"].append({"name": "x"})
            raise RuntimeError("the observer failed")

        def answering(request):
            if request.url.path.endswith("/inputs"):
                reply = next(polls)
                if reply is ready:
                    service.environment = ready
                return httpx.Response(202, json=reply)
            if request.method == "GET" and "/environments/" in request.url.path:
                service.environment = next(polls)
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(answering)
        assert cloud.connect_environment(project, poll_seconds=0, progress=observer)["state"] == (
            "ready"
        )
        assert ("../a.json",) in reports and ("../b.json",) in reports
        sent = service.environment_requests[0]
        assert set(service.blobs) == {sent["lakefile"]["sha256"], *project_inputs(project).blobs}
        # Each listing is stored after the files it names, and the lakefile last.
        order = list(service.blobs)
        assert order[-1] == sent["lakefile"]["sha256"]
        for tree in sent["path_trees"]:
            named = json.loads(service.blobs[tree["sha256"]])["files"]
            assert all(order.index(item["sha256"]) < order.index(tree["sha256"]) for item in named)
        service.environment = {
            **ready,
            "state": "failed",
            "error": {"code": "dependency_build_failed", "package": "sibling"},
        }
        with pytest.raises(EnvironmentBuildError, match="package sibling"):
            cloud.connect_environment(project, poll_seconds=0)
        service.environment["phase"] = "retiring"
        with pytest.raises(EnvironmentBuildError, match="package sibling"):
            cloud.connect_environment(project, poll_seconds=0, retry=True)
        service.environment["phase"] = None
        # The service's own explanation, when it sends one, says what to do.
        service.environment["error"] = {
            **service.environment["error"],
            "message": "A dependency failed to build with this toolchain.",
        }
        with pytest.raises(EnvironmentBuildError, match=r"sibling\)\. A dependency failed"):
            cloud.connect_environment(project, poll_seconds=0)
        # The build reads files outside the project: the project's own copies answer,
        # without following links or naming credential locations.
        config = project.parent / "config.json"
        config.write_bytes(b'{"flag": 1}')
        answers, blobs = project_answers(project, ["../config.json", "../missing", ".."])
        assert answers["../config.json"] == {
            "kind": "file",
            "sha256": hashlib.sha256(b'{"flag": 1}').hexdigest(),
            "size": 11,
        }
        assert answers["../missing"] == {"kind": "absent"}
        assert ["config.json", "file"] in answers[".."]["entries"]
        assert list(blobs.values()) == [b'{"flag": 1}']
        (project.parent / "linked.json").symlink_to(config)
        for refused in (
            "../linked.json",
            "../.ssh/id",
            ".lake/x",
            "../project/.lake/x",
            "../" * 9 + "x",
        ):
            with pytest.raises(ProjectError):
                project_answers(project, [refused])
        # Outside the project, only path dependencies and what .leanwarpread names.
        (project / ".leanwarpread").unlink()
        with pytest.raises(ProjectError, match=r"\.\./config\.json.*\.leanwarpread"):
            project_answers(project, ["../config.json"])
        (tmp_path / "sibling" / "extra.json").write_text("{}")
        assert (
            project_answers(project, ["../sibling/extra.json"])[0]["../sibling/extra.json"]["kind"]
            == "file"
        )
        (tmp_path / "sibling" / "extra.json").unlink()
        # Inside a declared directory is no further up, and its parent is not in it.
        (project / ".leanwarpread").write_text("../sibling\n")
        for outside in ("..", "../../x"):
            with pytest.raises(ProjectError, match=r"\.leanwarpread"):
                project_answers(project, [outside])
        # A declared directory's parent must already be reachable: one of the
        # project's own parents.
        (project / ".leanwarpread").write_text("../sibling/tools\n")
        with pytest.raises(ProjectError, match=r"\.\./sibling\b"):
            project_answers(project, ["../sibling/extra.json"])
        for named in ("/etc", "../.ssh", "../" * 9):
            (project / ".leanwarpread").write_text(named + "\n")
            with pytest.raises(ProjectError, match=r"\.leanwarpread"):
                project_answers(project, ["../sibling/extra.json"])
        (project / ".leanwarpread").write_text("..\n")
        answered: list[dict] = []
        submitted: list[dict] = []
        asking = {**building, "input_requests": ["../config.json"], "input_token": "c" * 64}
        service.environment = asking

        def exchange(request):
            if request.url.path.endswith("/inputs"):
                answered.append(json.loads(request.content))
                if len(answered) == 1:
                    # The build moved on since it was read: read it again.
                    return httpx.Response(
                        409, json={"error": {"code": "stale_attempt", "message": "moved on"}}
                    )
                # A build shared with another waiter: its answer's reply is ready.
                service.environment = ready
                return httpx.Response(202, json=ready)
            if request.method == "POST" and request.url.path.endswith("/environments"):
                submitted.append(json.loads(request.content))
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(exchange)
        assert cloud.connect_environment(project, poll_seconds=0)["state"] == "ready"
        # Each answer echoes the token its reads were asked under.
        assert answered == 2 * [
            {
                "attempt_id": building["attempt_id"],
                "request_token": "c" * 64,
                "answers": {"../config.json": answers["../config.json"]},
            }
        ]
        # Whoever's answers it read, a set built while this waited is replayed.
        assert len(submitted) == 2
        # An answer the build was given that changes before it is ready refuses the
        # connect, before a replay could start another build from the edit.
        answered.clear()
        service.environment = asking

        def edited_live(request):
            response = exchange(request)
            if request.url.path.endswith("/inputs"):
                config.write_bytes(str(len(answered)).encode())
            return response

        cloud._client._transport = httpx.MockTransport(edited_live)
        with pytest.raises(ProjectError, match="commit"):
            cloud.connect_environment(project, poll_seconds=0)
        config.write_bytes(b'{"flag": 1}')
        # Nor does a replay that starts another build, given the edited copy: every
        # build this connect answered must have read the same files.
        replayed: list[dict] = []
        second = {**asking, "attempt_id": "b2", "input_token": "d" * 64}

        def rebuilt(request):
            if request.url.path.endswith("/inputs"):
                body = json.loads(request.content)
                replayed.append(body)
                service.environment = ready
                return httpx.Response(202, json=ready)
            if (
                request.method == "POST"
                and request.url.path.endswith("/environments")
                and len(replayed) == 1
            ):
                config.write_bytes(b'{"flag": 5}')
                service.environment = second
                return httpx.Response(202, json=second)
            return service.handle(request)

        service.environment = asking
        cloud._client._transport = httpx.MockTransport(rebuilt)
        with pytest.raises(ProjectError, match="commit"):
            cloud.connect_environment(project, poll_seconds=0)
        config.write_bytes(b'{"flag": 1}')
        # An answer the service refused as stale was read by no build: a later
        # epoch's ask is answered with what the file holds then.
        refused: list[dict] = []

        def stale_then_edited(request):
            if request.url.path.endswith("/inputs"):
                refused.append(json.loads(request.content))
                if len(refused) == 1:
                    config.write_bytes(b'{"flag": 7}')
                    return httpx.Response(
                        409, json={"error": {"code": "stale_attempt", "message": "moved on"}}
                    )
                service.environment = ready
                return httpx.Response(202, json=ready)
            return service.handle(request)

        service.environment = asking
        cloud._client._transport = httpx.MockTransport(stale_then_edited)
        assert cloud.connect_environment(project, poll_seconds=0)["state"] == "ready"
        assert refused[1]["answers"]["../config.json"]["sha256"] == (
            hashlib.sha256(b'{"flag": 7}').hexdigest()
        )
        config.write_bytes(b'{"flag": 1}')
        service.environment = ready
        # A committed helper a build read is answered from its commit again in the
        # replay, whatever its working copy holds since.
        ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
        for argv in (["init", "-q"], ["add", "config.json"], ["commit", "-qm", "a"]):
            subprocess.run(["git", "-C", str(tmp_path), *ident, *argv], check=True)  # noqa: S603, S607
        replays: list[dict] = []

        def committed_replay(request):
            if request.url.path.endswith("/inputs"):
                service.environment = ready
                return httpx.Response(202, json=ready)
            if request.method == "POST" and request.url.path.endswith("/environments"):
                replays.append(json.loads(request.content))
                if len(replays) == 2:
                    config.write_bytes(b'{"flag": 8}')
                    return httpx.Response(
                        409,
                        json={
                            "error": {
                                "code": "inputs_required",
                                "message": "a",
                                "paths": ["../config.json"],
                            }
                        },
                    )
            return service.handle(request)

        service.environment = asking
        cloud._client._transport = httpx.MockTransport(committed_replay)
        # Keep the connected project journal separate from the later fresh-connect cases.
        connected_project = shutil.copytree(project, tmp_path / "connected-project")
        connected = ProjectSession(cloud, connected_project)
        connected.connect()
        assert connected.doctor()["compatible"]
        assert connected.connect()["workspace_id"]
        assert replays[-1]["inputs"]["../config.json"] == answers["../config.json"]
        shutil.rmtree(tmp_path / ".git")
        config.write_bytes(b'{"flag": 1}')
        service.environment = ready
        # A resend refused as stale does not undo an earlier send whose reply was lost.
        sends: list[dict] = []

        def lost_then_stale(request):
            if request.url.path.endswith("/inputs"):
                sends.append(json.loads(request.content))
                if len(sends) == 1:
                    raise httpx.ReadTimeout("reply lost", request=request)
                if len(sends) == 2:
                    config.write_bytes(b'{"flag": 9}')
                    return httpx.Response(
                        409, json={"error": {"code": "stale_attempt", "message": "moved on"}}
                    )
                service.environment = ready
                return httpx.Response(202, json=ready)
            return service.handle(request)

        resending = LeanWarpCloud(
            "secret", base_url="https://api.example", retries=1,
            transport=httpx.MockTransport(lost_then_stale),
        )  # fmt: skip
        service.environment = asking
        with resending, pytest.raises(ProjectError, match="commit"):
            resending.connect_environment(project, poll_seconds=0, timeout=30)
        config.write_bytes(b'{"flag": 1}')
        service.environment = ready
        # A failed build and its retry within one connect read the same files too.
        failed = {**asking, "state": "failed", "input_requests": [],
                  "error": {"code": "dependency_build_failed"}}  # fmt: skip
        retried = {**asking, "attempt_id": "r2", "input_token": "e" * 64}

        def retrying(request):
            if request.url.path.endswith("/inputs"):
                if json.loads(request.content)["attempt_id"] == "r2":
                    service.environment = ready
                    return httpx.Response(202, json=ready)
                service.environment = failed
                return httpx.Response(202, json=failed)
            if request.url.path.endswith("/retry"):
                config.write_bytes(b'{"flag": 6}')
                service.environment = retried
                return httpx.Response(202, json=retried)
            return service.handle(request)

        service.environment = asking
        cloud._client._transport = httpx.MockTransport(retrying)
        with pytest.raises(ProjectError, match="commit"):
            ProjectSession(cloud, project).connect(retry=True, timeout=30)
        config.write_bytes(b'{"flag": 1}')
        service.environment = ready
        # A ready set whose build read those files is reused only with the project's answers.
        submissions: list[dict] = []

        def replay(request):
            if request.method == "POST" and request.url.path.endswith("/environments"):
                submissions.append(json.loads(request.content))
                if len(submissions) == 1:
                    return httpx.Response(
                        409,
                        json={
                            "error": {
                                "code": "inputs_required",
                                "message": "answer",
                                "paths": ["../config.json"],
                            }
                        },
                    )
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(replay)
        assert cloud.connect_environment(project, poll_seconds=0)["state"] == "ready"
        assert submissions[1]["inputs"] == {"../config.json": answers["../config.json"]}

        # An answer changed after it was read, before attaching, refuses the connect;
        # a committed one is read from its commit, which only a new commit changes.
        def git(*argv: str) -> None:
            identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "-C", str(tmp_path), *identity, *argv], check=True)  # noqa: S603, S607

        def late(edit: Callable[[], None]) -> Callable[[httpx.Request], httpx.Response]:
            def handle(request: httpx.Request) -> httpx.Response:
                response = replay(request)
                if len(submissions) == 2 and request.url.path.endswith("/environments"):
                    edit()
                return response

            return handle

        def plain() -> None:
            git("init", "-q")
            git("add", "config.json")
            git("commit", "-qm", "a")

        def linked() -> None:
            # A linked worktree or submodule: its .git is a file naming the repository.
            git("init", "-q", f"--separate-git-dir={tmp_path / 'repository'}")
            git("add", "config.json")
            git("commit", "-qm", "a")

        def symlink() -> None:
            # The commit holds a link whose target's text is the file's bytes.
            config.unlink()
            config.symlink_to('{"flag": 1}')
            plain()
            config.unlink()

        edited = lambda: config.write_bytes(b'{"flag": 3}')  # noqa: E731
        for setup, edit, refused in (
            (None, edited, True),
            # Written back with the very same bytes: the file still changed after it
            # was read, so the answers cannot be shown to have held together.
            (None, lambda: config.write_bytes(config.read_bytes()), True),
            (plain, edited, False),
            (linked, edited, False),
            (symlink, edited, True),
            (plain, lambda: (config.write_bytes(b"{}"), git("commit", "-qam", "b")), True),
        ):
            config.write_bytes(b'{"flag": 1}')
            if setup is not None:
                setup()
                config.write_bytes(b'{"flag": 1}')
            submissions.clear()
            cloud._client._transport = httpx.MockTransport(late(edit))
            if refused:
                with pytest.raises(ProjectError, match="commit"):
                    cloud.connect_environment(project, poll_seconds=0)
            else:
                assert cloud.connect_environment(project, poll_seconds=0)["state"] == "ready"
            if setup is plain and refused is False:
                # A name git would read as a pathspec is matched literally.
                (tmp_path / ":config.json").write_bytes(b'{"flag": 1}')
                magic: dict = {}
                project_answers(project, ["../:config.json"], committed=magic)
                assert magic == {}
                (tmp_path / ":config.json").unlink()
            for made in (tmp_path / ".git", tmp_path / "repository"):
                if made.is_dir() and not made.is_symlink():
                    shutil.rmtree(made)
                else:
                    made.unlink(missing_ok=True)
        config.write_bytes(b'{"flag": 1}')
        # An answer an edit invalidated between two batches starts the exchange over:
        # the set is attached only with answers that held together.
        submissions.clear()

        def refuse(paths: list[str]) -> httpx.Response:
            return httpx.Response(
                409, json={"error": {"code": "inputs_required", "message": "a", "paths": paths}}
            )

        def edited(request):
            if request.method == "POST" and request.url.path.endswith("/environments"):
                submissions.append(json.loads(request.content))
                given = submissions[-1].get("inputs", {})
                if len(submissions) == 2:
                    config.write_bytes(b'{"flag": 2}')
                if "../config.json" not in given:
                    return refuse(["../config.json"])
                if "../other.json" not in given:
                    return refuse(["../other.json"])
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(edited)
        assert cloud.connect_environment(project, poll_seconds=0)["state"] == "ready"
        assert [sorted(item.get("inputs", {})) for item in submissions] == [
            [],
            ["../config.json"],
            [],
            ["../config.json"],
            ["../config.json", "../other.json"],
        ]
        assert submissions[-1]["inputs"]["../config.json"]["sha256"] == (
            hashlib.sha256(b'{"flag": 2}').hexdigest()
        )
        # One budget for the whole connect: files that keep changing are reported
        # after a few restarts, and replay honours the connect's deadline.
        submissions.clear()

        def changing(request):
            if request.method == "POST" and request.url.path.endswith("/environments"):
                submissions.append(json.loads(request.content))
                # Every batch asks something new while the config keeps changing.
                config.write_bytes(str(len(submissions)).encode())
                return refuse(["../config.json", f"../p{len(submissions)}.json"])
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(changing)
        with pytest.raises(LeanWarpCloudError, match="kept changing"):
            cloud.connect_environment(project, poll_seconds=0)
        cloud._client._transport = httpx.MockTransport(replay)
        submissions.clear()
        with pytest.raises(ConnectTimeout):
            cloud.connect_environment(project, timeout=0, poll_seconds=0)
        assert len(submissions) == 1
        # Nor does an upload outlast it: only the first request is exempt.
        uploads: list[str] = []

        def uploading(request):
            if request.method == "POST" and request.url.path.endswith("/environments"):
                return httpx.Response(
                    409, json={"error": {"code": "upload_required", "message": "upload"}}
                )
            uploads.append(request.url.path)
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(uploading)
        with pytest.raises(ConnectTimeout):
            cloud.connect_environment(project, timeout=0, poll_seconds=0)
        assert uploads == []


def test_doctor_reports_the_connection_without_writes(tmp_path):
    (tmp_path / "Main.lean").write_text("example : True := by trivial")
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN)
    with api() as (service, cloud):
        session = ProjectSession(cloud, tmp_path)
        session.connect()
        before = session.path.read_bytes()
        service.requests.clear()
        report = session.doctor()
        assert report["compatible"] is True
        assert report["environment"]["environment_id"] == "e"
        assert all(request.method == "GET" for request in service.requests)
        assert session.path.read_bytes() == before
        (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.34.1")
        assert "changed" in session.doctor()["message"]


def test_doctor_requires_recovery_for_uncertain_creation(tmp_path):
    (tmp_path / "Main.lean").write_text("example : True := by trivial")
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN)
    with api() as (service, cloud):
        service.lose = "/workspaces"
        session = ProjectSession(cloud, tmp_path)
        with pytest.raises(LeanWarpTransportError):
            session.connect()
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
        assert service.environment_requests == [{"lean_toolchain": TOOLCHAIN}]
        session.sync()
        source.unlink()
        session.sync()
        assert service.sources == {}
        assert service.revision == 2
        assert session.doctor()["connected"] is True


def test_mcp_offers_toolchains_environments_and_connect_without_selection(tmp_path):
    from leanwarp_cloud.mcp_server import create_server

    with api() as (_, cloud):
        server = create_server(cloud, str(tmp_path))

        async def call():
            tools = {tool.name: tool for tool in await server.list_tools()}
            assert {"versions", "environments", "doctor", "connect"} <= set(tools)
            assert "environment" not in json.dumps(tools["connect"].inputSchema["properties"])
            result = await server.call_tool("versions", {})
            blocks = result[0] if isinstance(result, tuple) else result
            assert json.loads(blocks[0].text)["toolchains"][0]["lean_toolchain"] == TOOLCHAIN

        asyncio.run(call())


def test_a_connect_makes_no_request_past_its_deadline(tmp_path, monkeypatch):
    """Once the first request is made, every later one, a resend, an upload's blob
    or a poll after its sleep included, waits on the connect's own deadline. Time
    passes only when the client sleeps or a step below spends it."""
    import leanwarp_cloud.client as client_module

    project = lake_project(tmp_path)
    now = [1000.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        client_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    with api() as (service, cloud):
        cloud._retries = None  # resend as the default client would
        sent: list[tuple[str, str]] = []

        def busy(paths: tuple[str, ...]):
            def handle(request):
                sent.append((request.method, request.url.path))
                if request.url.path.endswith(paths):
                    assert request.extensions["leanwarp_deadline"] == now[0] + 1
                    assert request.extensions["timeout"]["read"] <= 1
                    # Spends the connect's budget, then asks to be retried.
                    now[0] += 5
                    return httpx.Response(
                        503,
                        headers={"Retry-After": "2"},
                        json={"error": {"code": "service_unavailable", "message": "busy"}},
                    )
                return service.handle(request)

            return handle

        # A resend of the first submission waits on the deadline: one attempt only.
        cloud._client._transport = httpx.MockTransport(busy(("/environments",)))
        with pytest.raises(ConnectTimeout):
            cloud.connect_environment(project, timeout=1, poll_seconds=0)
        assert [method for method, _ in sent] == ["POST"]

        # An upload receipt that spends the budget is followed by no blob and no resend.
        sent.clear()

        def upload_first(request):
            if request.method == "POST" and request.url.path.endswith("/environments"):
                sent.append(("POST", "environments"))
                return httpx.Response(
                    409, json={"error": {"code": "upload_required", "message": "upload"}}
                )
            return busy(("/environment-uploads",))(request)

        cloud._client._transport = httpx.MockTransport(upload_first)
        with pytest.raises((LeanWarpCloudError, ConnectTimeout)):
            cloud.connect_environment(project, timeout=1, poll_seconds=0)
        assert [method for method, _ in sent] == ["POST", "POST"]
        assert sent[-1][1].endswith("/environment-uploads")

        # A poll whose sleep crosses the deadline makes no read.
        sent.clear()
        service.environment = {**service.environment, "state": "building", "phase": "compiling"}

        def building(request):
            sent.append((request.method, request.url.path))
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(building)
        began = now[0]
        with pytest.raises(ConnectTimeout):
            cloud.connect_environment(project, timeout=1, poll_seconds=5)
        # It reached the poll (submitted, and found the build running), then read nothing.
        assert sent[-1][0] == "POST" and sent[-1][1].endswith("/environments")
        assert not any(method == "GET" for method, _ in sent)
        # Its sleep ran to the deadline, no further.
        assert now[0] - began == 1


def test_snapshot_and_helper_reads_share_credential_exclusions(tmp_path, capsys):
    from leanwarp_cloud import cli

    project = lake_project(tmp_path)
    package = tmp_path / "sibling"
    (package / ".aws").mkdir()
    (package / ".kube").mkdir()
    sentinel = b"non-secret test sentinel"
    for name in (
        ".aws/credentials",
        ".netrc",
        ".env.local",
        ".npmrc",
        ".kube/config",
        "id_ed25519",
        "Server.PEM",
    ):
        (package / name).write_bytes(sentinel)
        with pytest.raises(ProjectError, match="never sends"):
            project_answers(project, ["../sibling/" + name])
    # The tree is what a clone of it holds: its own ignore files apply, and a file
    # in an ignored directory stays out even if a later rule names it.
    (package / ".gitignore").write_text("secrets/\n!secrets/Keep.lean\n*.log\n")
    (package / ".leanwarpignore").write_text("Draft.lean\n")
    (package / "secrets").mkdir()
    for name in ("secrets/Keep.lean", "build.log", "Draft.lean"):
        (package / name).write_bytes(sentinel)
    (package / "id_ed25519.pub").write_text("public")
    inputs = project_inputs(project)
    assert sentinel not in inputs.blobs.values()
    sibling, nested = inputs.request["path_trees"]
    listed = [item["path"] for item in json.loads(inputs.blobs[sibling["sha256"]])["files"]]
    assert listed == [".gitignore", ".leanwarpignore", "Sibling.lean", "id_ed25519.pub"]
    # What the tree leaves out, the build cannot ask for later: refused, never absent.
    (package / "link.log").symlink_to(package / "Sibling.lean")
    for name in (
        "secrets/Keep.lean",
        "secrets",
        "build.log",
        "missing.log",
        "link.log",
        "Draft.lean",
    ):
        refused = re.escape(f"read ../sibling/{name}, which the path dependency ../sibling")
        with pytest.raises(ProjectError, match=refused):
            project_answers(project, ["../sibling/" + name])
    answers = project_answers(project, ["../sibling", "../sibling/Sibling.lean"])[0]
    assert ["secrets", "directory"] in answers["../sibling"]["entries"]
    assert answers["../sibling/Sibling.lean"]["kind"] == "file"
    # A directory where an ignore file would be is none, and can itself be ignored.
    (project / "vendor" / "nested" / ".gitignore").mkdir()
    (project / "vendor" / "nested" / ".leanwarpignore").write_text(".gitignore/\n")
    inputs = project_inputs(project)
    nested = inputs.request["path_trees"][1]
    assert [item["path"] for item in json.loads(inputs.blobs[nested["sha256"]])["files"]] == [
        ".leanwarpignore",
        "Nested.lean",
    ]
    with pytest.raises(ProjectError, match="dependency vendor/nested"):
        project_answers(project, ["vendor/nested/.gitignore"])
    # `files --dependencies` previews exactly what connect sends, with no key.
    assert cli.main(["--project", str(project), "files", "--dependencies"]) == 0
    preview = json.loads(capsys.readouterr().out)["dependencies"]
    assert [tree["dir"] for tree in preview] == ["../sibling", "vendor/nested"]
    assert preview[1]["files"] == [
        {"path": ".leanwarpignore", "bytes": 12},
        {"path": "Nested.lean", "bytes": 15},
    ]
    assert [(item["path"], item["bytes"]) for item in preview[0]["files"]] == [
        (path, (package / path).stat().st_size) for path in listed
    ]
    assert cli.main(["--project", str(project), "files"]) == 0
    assert "dependencies" not in json.loads(capsys.readouterr().out)
    manifest = project / "lake-manifest.json"
    manifest.write_text(manifest.read_text().replace("../sibling", "../sibling/.aws"))
    with pytest.raises(ProjectError, match="credential location"):
        project_inputs(project)


def test_upload_receipts_split_by_both_byte_and_file_limits(monkeypatch):
    from leanwarp_cloud import client

    monkeypatch.setattr(client, "_UPLOAD_BATCH", 2)
    monkeypatch.setattr(client, "_UPLOAD_RECEIPT_BYTES", 5)
    blobs = {"a": b"123", "b": b"456", "c": b"78", "d": b""}
    assert list(client._upload_batches({name: len(data) for name, data in blobs.items()})) == [
        {"a": 3},
        {"b": 3, "c": 2},
        {"d": 0},
    ]


def test_resumed_workspace_rechecks_helpers_used_to_select_its_environment(tmp_path):
    from leanwarp_cloud.errors import SessionError

    project = lake_project(tmp_path)
    helper = project / "dependency.txt"
    helper.write_text("first")
    with api() as (service, cloud):
        ready = service.environment
        service.environment = {
            **ready,
            "state": "building",
            "input_requests": ["dependency.txt"],
            "input_token": "c" * 64,
        }

        def answer(request):
            if request.url.path.endswith("/inputs"):
                service.environment = ready
                return httpx.Response(202, json=ready)
            return service.handle(request)

        cloud._client._transport = httpx.MockTransport(answer)
        ProjectSession(cloud, project).connect()
        resumed = ProjectSession(cloud, project)
        assert resumed.connect()["workspace_id"]
        helper.write_text("second")
        with pytest.raises(SessionError, match="changed"):
            resumed.connect()
        assert resumed.doctor()["compatible"] is False

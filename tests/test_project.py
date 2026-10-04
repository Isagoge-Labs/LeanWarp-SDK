from pathlib import Path

import pytest
from leanwarp_cloud import collect_lean_sources


def test_uploads_only_project_lean_sources(tmp_path: Path) -> None:
    (tmp_path / "Proof.lean").write_text("example : True := by trivial")
    (tmp_path / ".env").write_text("API_KEY=secret")
    (tmp_path / "lakefile.lean").write_text("import Lake")
    (tmp_path / ".lake").mkdir()
    (tmp_path / ".lake" / "Dependency.lean").write_text("should not upload")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "Private.lean").write_text("secret")
    assert collect_lean_sources(tmp_path) == {"Proof.lean": "example : True := by trivial"}


def test_rejects_symlink_to_secret_outside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret").write_text("secret")
    (root / "Proof.lean").symlink_to(tmp_path / "secret")
    with pytest.raises(OSError):
        collect_lean_sources(root)


def test_rejects_symlinked_source_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        collect_lean_sources(root)


def test_limits_include_encoded_unicode_and_total_upload(tmp_path: Path) -> None:
    (tmp_path / "A.lean").write_text("∀" * 5)
    with pytest.raises(ValueError, match="limit"):
        collect_lean_sources(tmp_path, max_file_bytes=10)
    with pytest.raises(ValueError, match="limit"):
        collect_lean_sources(tmp_path, max_total_bytes=20)


def test_plain_lean_files_need_no_project_metadata(tmp_path):
    from leanwarp_cloud.project import project_environment, project_identity

    (tmp_path / "Proof.lean").write_text("example : True := trivial")
    identity = project_identity(tmp_path)
    assert identity.toolchain is None and identity.manifest_sha256 is None
    assert project_environment(tmp_path) == ("unspecified", "unlocked")


def test_lean_files_in_a_subdirectory_count_as_a_project(tmp_path):
    from leanwarp_cloud.project import project_environment

    (tmp_path / "Algebra").mkdir()
    (tmp_path / "Algebra" / "Basic.lean").write_text("example : True := trivial")
    assert project_environment(tmp_path) == ("unspecified", "unlocked")


def test_directory_without_lean_files_is_reported_as_the_wrong_directory(tmp_path):
    from leanwarp_cloud.project import ProjectError, project_identity

    (tmp_path / "notes.txt").write_text("not Lean")
    with pytest.raises(ProjectError, match="no Lean files") as error:
        project_identity(tmp_path)
    assert "--project" in str(error.value)


TOOLCHAIN = "leanprover/lean4:v4.26.0"
MANIFEST = b'{"version":"1.1.0","packages":[]}'


def _build(environment, toolchain=TOOLCHAIN, manifest=MANIFEST):
    from leanwarp_cloud.project import dependency_fingerprint

    return {
        "bundle_id": f"{environment}-{'a' * 20}",
        "environment_id": environment,
        "lean_toolchain": toolchain,
        "lake_dependencies_sha256": dependency_fingerprint(manifest),
    }


CATALOG = [
    _build("lean-4.26-mathlib"),
    _build("lean-4.34-mathlib", "leanprover/lean4:v4.34.1", b'{"version":"1.2.0","packages":[]}'),
]


def test_pinned_project_runs_only_where_its_toolchain_and_lockfile_match(tmp_path):
    from leanwarp_cloud.project import choose_environment, project_identity

    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (tmp_path / "lake-manifest.json").write_bytes(MANIFEST)
    choice = choose_environment(project_identity(tmp_path), CATALOG)
    assert choice.public() == {
        "environment": "lean-4.26-mathlib",
        "lean_toolchain": TOOLCHAIN,
        "match": "exact",
        "matches_project": True,
    }


def test_unpinned_project_runs_on_the_newest_environment(tmp_path):
    from leanwarp_cloud.project import choose_environment, project_identity

    (tmp_path / "Proof.lean").write_text("example : True := trivial")
    choice = choose_environment(project_identity(tmp_path), CATALOG)
    assert (choice.public()["environment"], choice.match) == ("lean-4.34-mathlib", "unpinned")


def test_toolchain_without_lockfile_selects_by_toolchain(tmp_path):
    from leanwarp_cloud.project import choose_environment, project_identity

    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN)
    choice = choose_environment(project_identity(tmp_path), CATALOG)
    assert (choice.public()["environment"], choice.match) == ("lean-4.26-mathlib", "exact")


def test_mismatched_project_needs_an_explicit_environment(tmp_path):
    from leanwarp_cloud.project import ProjectError, choose_environment, project_identity

    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.20.0")
    identity = project_identity(tmp_path)
    with pytest.raises(ProjectError, match="--environment NAME") as error:
        choose_environment(identity, CATALOG)
    assert "lean-4.26-mathlib, lean-4.34-mathlib" in str(error.value)
    choice = choose_environment(identity, CATALOG, "lean-4.26-mathlib")
    assert (choice.match, choice.matches_project) == ("requested", False)
    with pytest.raises(ProjectError, match="does not serve"):
        choose_environment(identity, CATALOG, "lean-3")


def test_real_manifest_ignores_root_name_format_and_dependency_order():
    import json

    from leanwarp_cloud.project import dependency_fingerprint

    original = (Path(__file__).parents[1] / "examples/lean-4.26/lake-manifest.json").read_bytes()
    manifest = json.loads(original)
    manifest["name"] = "researchers_project"
    manifest["lakeDir"] = "custom-cache"
    manifest["packages"].reverse()
    manifest["packages"][0]["inputRev"] = "other-ref-at-same-commit"
    assert dependency_fingerprint(original) == dependency_fingerprint(json.dumps(manifest).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("rev", "f" * 40),
        ("url", "https://example.test/fork"),
        ("configFile", "other.lean"),
        ("subDir", "other"),
    ],
)
def test_real_manifest_preserves_execution_relevant_dependency_identity(field, value):
    import json

    from leanwarp_cloud.project import dependency_fingerprint

    original = (Path(__file__).parents[1] / "examples/lean-4.26/lake-manifest.json").read_bytes()
    manifest = json.loads(original)
    manifest["packages"][0][field] = value
    assert dependency_fingerprint(original) != dependency_fingerprint(json.dumps(manifest).encode())


def test_lean_4_34_manifest_format_names_the_same_dependencies():
    import json

    from leanwarp_cloud.project import dependency_fingerprint

    original = (Path(__file__).parents[1] / "examples/lean-4.34/lake-manifest.json").read_bytes()
    manifest = json.loads(original)
    assert manifest["version"] == "1.2.0"
    # The 1.2.0 toolchain flag is project metadata, not a dependency.
    manifest["fixedToolchain"] = True
    assert dependency_fingerprint(original) == dependency_fingerprint(json.dumps(manifest).encode())
    manifest["version"] = "1.1.0"
    manifest.pop("fixedToolchain")
    assert dependency_fingerprint(original) == dependency_fingerprint(json.dumps(manifest).encode())


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"version":"1.1.0","packages":[],"unknown":true}',
        b'{"version":"1.1.0","packages":[],"fixedToolchain":true}',
        b'{"version":"1.1.0","packages":[],"packages":[]}',
        b'{"version":"2.0.0","packages":[]}',
        b'{"version":[],"packages":[]}',
        b'{"version":{},"packages":[]}',
        b'{"version":null,"packages":[]}',
        b'{"version":true,"packages":[]}',
        b'{"version":12,"packages":[]}',
    ],
)
def test_ambiguous_or_unsupported_manifest_fails_closed(raw):
    from leanwarp_cloud.project import ProjectError, dependency_fingerprint

    with pytest.raises(ProjectError):
        dependency_fingerprint(raw)


def test_explicit_bundle_choice_accepts_any_build_of_a_listed_environment():
    from leanwarp_cloud.project import selects

    current = {"bundle_id": "lean-4.26-mathlib-" + "b" * 20, "environment_id": "lean-4.26-mathlib"}
    assert selects(current, None)
    assert selects(current, current["bundle_id"])
    assert selects(current, "lean-4.26-mathlib")
    assert selects(current, "lean-4.26-mathlib-" + "a" * 20)
    assert not selects(current, "lean-4.34-mathlib-" + "a" * 20)
    # Older services list builds without an environment.
    assert not selects({"bundle_id": "lean426"}, "lean-4.26-mathlib")

import os
from pathlib import Path

import pytest
from leanwarp_cloud import collect_lean_sources, plan_upload
from leanwarp_cloud.project import ProjectError


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
    with pytest.raises(ProjectError, match=r"Proof\.lean cannot be read; symlinked"):
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


def test_ignore_files_exclude_an_archive_that_could_not_be_uploaded(tmp_path: Path) -> None:
    (tmp_path / "Main.lean").write_text("theorem main : True := trivial\n")
    archive = tmp_path / "archive-2024"
    archive.mkdir()
    (archive / "Old Draft.lean").write_text("-- not a module path\n")
    with pytest.raises(ProjectError, match=r"Exclude it in \.leanwarpignore"):
        collect_lean_sources(tmp_path)

    (tmp_path / ".leanwarpignore").write_text("archive-2024/\n")
    assert collect_lean_sources(tmp_path) == {"Main.lean": "theorem main : True := trivial\n"}


def test_gitignore_is_honoured_and_leanwarpignore_can_override_it(tmp_path: Path) -> None:
    (tmp_path / "Main.lean").write_text("import Generated.Data\n")
    (tmp_path / "Scratch.lean").write_text("-- scratch\n")
    generated = tmp_path / "Generated"
    generated.mkdir()
    (generated / "Data.lean").write_text("def data := 1\n")
    (tmp_path / ".gitignore").write_text("Scratch.lean\n/Generated/*.lean\n")
    # Leaving out a file that an uploaded file imports would only fail on the worker.
    with pytest.raises(ProjectError, match=r"Main\.lean imports Generated\.Data, but an ignore"):
        collect_lean_sources(tmp_path)

    (tmp_path / ".leanwarpignore").write_text("!/Generated/Data.lean\n")
    assert set(collect_lean_sources(tmp_path)) == {"Main.lean", "Generated/Data.lean"}


def test_upload_plan_lists_files_and_every_problem(tmp_path: Path) -> None:
    (tmp_path / "Main.lean").write_text("theorem main : True := trivial\n")
    (tmp_path / "Ignored.lean").write_text("-- ignored\n")
    (tmp_path / "Bad-Name.lean").write_text("-- bad\n")
    (tmp_path / "Latin1.lean").write_bytes(b"-- caf\xe9\n")
    (tmp_path / ".leanwarpignore").write_text("Ignored.lean\n")

    plan = plan_upload(tmp_path).public()

    assert plan["files"] == [{"path": "Main.lean", "bytes": 31}]
    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 31 + len("Main.lean")
    assert plan["limits"] == {"files": 256, "bytes": 1024 * 1024}
    assert plan["ignored_lean_files"] == 1
    assert plan["ignore_files"] == [".leanwarpignore"]
    assert len(plan["problems"]) == 2
    assert plan["problems"][0].startswith("Bad-Name.lean is not an uploadable Lean module path")
    assert plan["problems"][1] == "Latin1.lean is not UTF-8 text"


def test_too_many_files_points_to_the_preview_and_the_ignore_file(tmp_path: Path) -> None:
    for index in range(257):
        (tmp_path / f"File{index}.lean").write_text("-- x\n")
    with pytest.raises(ProjectError, match=r"leanwarp files.*\.leanwarpignore"):
        collect_lean_sources(tmp_path)
    assert plan_upload(tmp_path).public()["file_count"] == 257


def test_imports_from_an_ignored_directory_are_reported(tmp_path: Path) -> None:
    main = "module\n\npublic import Vendor.Util\n\ntheorem t : True := trivial\n"
    (tmp_path / "Main.lean").write_text(main)
    vendor = tmp_path / "Vendor"
    vendor.mkdir()
    (vendor / "Util.lean").write_text("def util := 1\n")
    (tmp_path / ".leanwarpignore").write_text("Vendor/\n")
    problems = plan_upload(tmp_path).problems
    assert problems == (
        "Main.lean imports Vendor.Util, but an ignore rule in .gitignore or .leanwarpignore "
        "leaves out Vendor/Util.lean. Change the rule so it is uploaded; `leanwarp files` "
        "shows the result",
    )


def test_unreadable_files_say_why(tmp_path: Path) -> None:
    locked = tmp_path / "Locked.lean"
    locked.write_text("-- private\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with permission to read any file")
        assert plan_upload(tmp_path).problems == ("Locked.lean cannot be read: Permission denied",)
    finally:
        locked.chmod(0o600)


def test_size_problems_state_the_limits_in_force(tmp_path: Path) -> None:
    (tmp_path / "Big.lean").write_text("-- " + "x" * 2048 + "\n")
    assert plan_upload(tmp_path, max_file_bytes=1024).problems == (
        "Big.lean is larger than the 1 KiB upload limit",
    )


@pytest.mark.parametrize(
    "header",
    [
        "/- Copyright /- nested -/ notice -/\nimport Aux\n",
        "import\n  Aux\n",
        "import /- comment -/ Aux -- note\n",
        "import «Aux»\n",
        "module\nmeta import all Aux\n",
    ],
)
def test_ignored_dependencies_are_found_through_lean_header_whitespace(tmp_path, header):
    (tmp_path / "Aux.lean").write_text("def aux := 1\n")
    (tmp_path / ".leanwarpignore").write_text("Aux.lean\n")
    (tmp_path / "Main.lean").write_text(header + "example : True := trivial\n")
    with pytest.raises(ProjectError, match=r"Main\.lean imports Aux"):
        collect_lean_sources(tmp_path)


def test_comment_words_are_not_imports_and_body_import_text_is_not_a_header(tmp_path):
    (tmp_path / "Aux.lean").write_text("def aux := 1\n")
    (tmp_path / ".leanwarpignore").write_text("Aux.lean\n")
    main = 'import Init -- Aux\n/- import Aux -/\ndef text := "import Aux"\n'
    (tmp_path / "Main.lean").write_text(main)
    assert collect_lean_sources(tmp_path) == {"Main.lean": main}


def test_lowercase_module_imports_are_checked(tmp_path):
    (tmp_path / "aux.lean").write_text("def aux := 1\n")
    (tmp_path / ".leanwarpignore").write_text("aux.lean\n")
    (tmp_path / "Main.lean").write_text("import aux\nexample : True := trivial\n")
    with pytest.raises(ProjectError, match=r"Main\.lean imports aux"):
        collect_lean_sources(tmp_path)


def test_preview_keeps_complete_metadata_without_retaining_oversized_source_bodies(tmp_path):
    from dataclasses import asdict

    source = "-- " + "x" * (64 * 1024)
    for index in range(300):
        (tmp_path / f"File{index}.lean").write_text(source)
    plan = plan_upload(tmp_path)
    assert plan.public()["file_count"] == 300
    assert plan.total_bytes > 18 * 1024 * 1024
    assert "exceed the upload limit" in plan.problems[0]
    assert "sources" not in asdict(plan)
    assert source not in repr(asdict(plan))
    with pytest.raises(ProjectError, match="exceed the upload limit"):
        collect_lean_sources(tmp_path)


def test_quoted_and_unicode_imports_do_not_hide_later_ignored_modules(tmp_path):
    vendor = tmp_path / "Foo"
    vendor.mkdir()
    (vendor / "Bar.lean").write_text("def bar := 1\n")
    (tmp_path / ".leanwarpignore").write_text("Foo/Bar.lean\n")
    (tmp_path / "Main.lean").write_text(
        "import «外部»\nimport Foo.«Bar»\nexample : True := trivial\n"
    )
    assert "Main.lean imports Foo.Bar" in plan_upload(tmp_path).problems[0]


def test_repeated_imports_produce_one_actionable_problem_per_file(tmp_path):
    (tmp_path / "Aux.lean").write_text("def aux := 1\n")
    (tmp_path / ".leanwarpignore").write_text("Aux.lean\n")
    (tmp_path / "Main.lean").write_text("import Aux\n" * 1000)
    assert len(plan_upload(tmp_path).problems) == 1

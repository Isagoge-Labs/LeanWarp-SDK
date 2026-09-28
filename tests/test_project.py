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

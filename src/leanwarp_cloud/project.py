"""Conservative source discovery: never upload directories or arbitrary project files."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

_EXCLUDED = {".git", ".lake", ".env", ".ssh", ".aws", ".venv", "node_modules", "vendor"}


class ProjectError(ValueError):
    """An actionable project error that contains no file contents or credentials."""


def project_environment(root: str | Path) -> tuple[str, str]:
    """Read bounded project metadata without uploading or executing build configuration."""
    project = Path(root).resolve(strict=True)
    descriptor = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        files: dict[str, bytes] = {}
        for name in ("lean-toolchain", "lake-manifest.json"):
            try:
                file_descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                )
            except FileNotFoundError as error:
                raise ProjectError(
                    f"missing {name}; run from your Lean project root or set --project. "
                    "The project must have a toolchain and a Lake dependency lockfile."
                ) from error
            with os.fdopen(file_descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("project metadata must be a regular file")
                files[name] = source.read(1024 * 1024 + 1)
                if len(files[name]) > 1024 * 1024:
                    raise ValueError("project metadata exceeds the size limit")
    finally:
        os.close(descriptor)
    return (
        files["lean-toolchain"].decode("utf-8").strip(),
        hashlib.sha256(files["lake-manifest.json"]).hexdigest(),
    )


def collect_lean_sources(
    root: str | Path, *, max_file_bytes: int = 1024 * 1024, max_total_bytes: int = 1024 * 1024
) -> dict[str, str]:
    """Read regular UTF-8 .lean files below root, excluding links and hidden paths.

    Dependency files and the toolchain are supplied by the selected server bundle.
    Symlinks are errors rather than silently dereferenced outside the project.
    """
    project = Path(root).resolve(strict=True)
    if not project.is_dir():
        raise ValueError("project root must be a directory")
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("source size limits must be positive")
    sources: dict[str, str] = {}
    total = 0
    for directory, directories, names, directory_fd in os.fwalk(project, follow_symlinks=False):
        directories[:] = sorted(
            name for name in directories if name not in _EXCLUDED and not name.startswith(".")
        )
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise ValueError("symlinked source directories are not supported")
        for name in sorted(names):
            if not name.endswith(".lean") or name.startswith(".") or name == "lakefile.lean":
                continue
            path = Path(directory) / name
            relative = path.relative_to(project).as_posix()
            module_root = relative.split("/", 1)[0].removesuffix(".lean")
            if (
                len(relative) > 240
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*\.lean", relative
                )
                is None
                or module_root in {"Init", "Lean", "Lake", "Std", "Mathlib"}
            ):
                raise ValueError(f"unsupported or reserved Lean module path: {relative}")
            # O_NOFOLLOW rejects a file replaced by a symlink during discovery.
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"source must be a regular file: {relative}")
                raw = source.read(max_file_bytes + 1)
            total += len(raw) + len(relative.encode("utf-8"))
            if len(raw) > max_file_bytes or total > max_total_bytes or len(sources) >= 256:
                raise ValueError("project exceeds the configured source upload limit")
            sources[relative] = raw.decode("utf-8")
    return sources

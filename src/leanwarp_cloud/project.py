"""Conservative source discovery: never upload directories or arbitrary project files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

_EXCLUDED = {".git", ".lake", ".env", ".ssh", ".aws", ".venv", "node_modules", "vendor"}
# Lake manifest formats in supported Lean releases. 1.2.0 (Lean 4.34) adds an
# optional `fixedToolchain` flag; neither format's extra fields name dependencies.
_V1_FIELDS = frozenset({"version", "packages", "name", "lakeDir", "packagesDir"})
_MANIFEST_FIELDS = {"1.1.0": _V1_FIELDS, "1.2.0": _V1_FIELDS | {"fixedToolchain"}}


class ProjectError(ValueError):
    """An actionable project error that contains no file contents or credentials."""


def _project_metadata(root: str | Path) -> dict[str, bytes]:
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
    return files


def dependency_fingerprint(raw: bytes) -> str:
    """Versioned Lake 1.1 dependency identity, independent of the root project.

    Retain package source, resolved revision and build-file selection. Root name,
    local cache paths, requested refs and direct/transitive provenance do not
    change the locked dependency sources. Unknown fields fail closed.
    """

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ProjectError("duplicate Lake manifest field")
            value[key] = item
        return value

    try:
        manifest = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ProjectError("invalid Lake dependency manifest") from error
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("version"), str)
        or manifest.get("version") not in _MANIFEST_FIELDS
        or set(manifest) - _MANIFEST_FIELDS[manifest["version"]]
        or not isinstance(manifest.get("packages"), list)
    ):
        raise ProjectError(
            "unsupported Lake dependency manifest; expected version "
            + " or ".join(sorted(_MANIFEST_FIELDS))
        )
    packages = []
    names: set[str] = set()
    for package in manifest["packages"]:
        if (
            not isinstance(package, dict)
            or set(package)
            - {
                "name",
                "type",
                "url",
                "rev",
                "subDir",
                "scope",
                "manifestFile",
                "configFile",
                "inputRev",
                "inherited",
            }
            or package.get("type") != "git"
            or not isinstance(package.get("name"), str)
            or not package["name"]
            or package["name"] in names
            or not isinstance(package.get("url"), str)
            or not package["url"]
            or not isinstance(package.get("rev"), str)
            or re.fullmatch(r"[0-9a-f]{40}", package["rev"]) is None
        ):
            raise ProjectError("dependencies must be uniquely named, pinned Git packages")
        names.add(package["name"])
        packages.append({k: v for k, v in package.items() if k not in {"inputRev", "inherited"}})
    canonical = json.dumps(
        {
            "schema": "leanwarp.dependencies.v1",
            "packages": sorted(packages, key=lambda p: p["name"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def project_environment(root: str | Path, *, legacy: bool = False) -> tuple[str, str]:
    files = _project_metadata(root)
    raw = files["lake-manifest.json"]
    return (
        files["lean-toolchain"].decode("utf-8").strip(),
        hashlib.sha256(raw).hexdigest() if legacy else dependency_fingerprint(raw),
    )


def selects(bundle: dict[str, Any], requested: str | None) -> bool:
    """Whether a listed build satisfies an explicit `--bundle` choice.

    The service lists each environment's current build, so a build or environment
    identity selects it; a superseded build of the same environment does too.
    """
    if requested is None:
        return True
    if requested == bundle["bundle_id"]:
        return True
    environment = bundle.get("environment_id")
    build = re.fullmatch(r"(.+)-[0-9a-f]{20}", requested)
    return environment is not None and environment in {
        requested,
        build.group(1) if build else None,
    }


def matching_bundles(root: str | Path, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files = _project_metadata(root)
    toolchain = files["lean-toolchain"].decode("utf-8").strip()
    raw = files["lake-manifest.json"]
    fingerprint = dependency_fingerprint(raw)
    raw_digest = hashlib.sha256(raw).hexdigest()
    # Existing immutable catalogs remain usable during a rolling SDK upgrade.
    return [
        bundle
        for bundle in catalog
        if bundle["lean_toolchain"] == toolchain
        and (
            bundle.get("lake_dependencies_sha256") == fingerprint
            if bundle.get("lake_dependencies_sha256") is not None
            else bundle["lake_manifest_sha256"] == raw_digest
        )
    ]


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

"""Project identity, environment choice and conservative source discovery.

Only `.lean` sources are uploaded. The served environment supplies Lean and every
dependency, so a project's toolchain and lockfile only decide where it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_EXCLUDED = {".git", ".lake", ".env", ".ssh", ".aws", ".venv", "node_modules", "vendor"}
# Lake manifest formats in supported Lean releases. 1.2.0 (Lean 4.34) adds an
# optional `fixedToolchain` flag; neither format's extra fields name dependencies.
_V1_FIELDS = frozenset({"version", "packages", "name", "lakeDir", "packagesDir"})
_MANIFEST_FIELDS = {"1.1.0": _V1_FIELDS, "1.2.0": _V1_FIELDS | {"fixedToolchain"}}


class ProjectError(ValueError):
    """An actionable project error that contains no file contents or credentials."""


_PROJECT_FILES = ("lean-toolchain", "lake-manifest.json")
_PROJECT_MARKERS = frozenset({*_PROJECT_FILES, "lakefile.toml", "lakefile.lean"})


def _has_lean_files(project: Path) -> bool:
    """Find a source below the root without following links or an unbounded tree."""
    visited = 0
    for directory, directories, files in os.walk(project, followlinks=False):
        visited += len(directories) + len(files) + 1
        if visited > 10_000:
            raise ProjectError("project discovery exceeds 10000 entries; pass a narrower --project")
        directories[:] = sorted(
            name
            for name in directories
            if name not in _EXCLUDED
            and not name.startswith(".")
            and not (Path(directory) / name).is_symlink()
        )
        if any(
            name.endswith(".lean")
            and not name.startswith(".")
            and name != "lakefile.lean"
            and (Path(directory) / name).is_file()
            and not (Path(directory) / name).is_symlink()
            for name in files
        ):
            return True
    return False


def _project_metadata(root: str | Path, *, allow_empty: bool = False) -> dict[str, bytes]:
    """Read the bounded toolchain and lockfile that exist, without executing build files.

    Both are optional: a directory of plain Lean files has neither. A directory with
    no Lean files and no project metadata is almost certainly the wrong directory.
    """
    project = Path(root).resolve(strict=True)
    descriptor = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        names = set(os.listdir(descriptor))
        if not allow_empty and not names & _PROJECT_MARKERS and not _has_lean_files(project):
            raise ProjectError(
                f"{project} has no Lean files in it or its subdirectories, and no "
                "lean-toolchain or lakefile; run from your project directory or pass --project"
            )
        files: dict[str, bytes] = {}
        for name in _PROJECT_FILES:
            try:
                file_descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                )
            except FileNotFoundError:
                continue
            with os.fdopen(file_descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ProjectError(f"{name} must be a regular file")
                files[name] = source.read(1024 * 1024 + 1)
                if len(files[name]) > 1024 * 1024:
                    raise ProjectError(f"{name} exceeds 1 MiB")
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
                raise ProjectError("lake-manifest.json repeats a field")
            value[key] = item
        return value

    try:
        manifest = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ProjectError("lake-manifest.json is not valid JSON") from error
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("version"), str)
        or manifest.get("version") not in _MANIFEST_FIELDS
        or set(manifest) - _MANIFEST_FIELDS[manifest["version"]]
        or not isinstance(manifest.get("packages"), list)
    ):
        raise ProjectError(
            "lake-manifest.json uses an unsupported format; expected version "
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
            raise ProjectError(
                "lake-manifest.json must list uniquely named Git dependencies pinned to commits"
            )
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


# Journal placeholders for metadata a project does not have.
_NO_TOOLCHAIN = "unspecified"
_NO_LOCKFILE = "unlocked"


@dataclass(frozen=True)
class ProjectIdentity:
    """What a local project pins: its Lean toolchain and locked dependencies, if any."""

    toolchain: str | None
    dependencies: str | None
    manifest_sha256: str | None

    def journal(self, *, legacy: bool = False) -> tuple[str, str]:
        locked = self.manifest_sha256 if legacy else self.dependencies
        return (self.toolchain or _NO_TOOLCHAIN, locked or _NO_LOCKFILE)

    def matches(self, build: Mapping[str, Any]) -> bool:
        """Whether a served build has exactly what this project pins."""
        if self.toolchain is not None and build["lean_toolchain"] != self.toolchain:
            return False
        if self.manifest_sha256 is None:
            return True
        # Older catalogs identify a lockfile by its bytes rather than its dependencies.
        if build.get("lake_dependencies_sha256") is not None:
            return bool(build["lake_dependencies_sha256"] == self.dependencies)
        return bool(build["lake_manifest_sha256"] == self.manifest_sha256)


def project_identity(root: str | Path, *, allow_empty: bool = False) -> ProjectIdentity:
    files = _project_metadata(root, allow_empty=allow_empty)
    raw = files.get("lake-manifest.json")
    toolchain = files.get("lean-toolchain")
    return ProjectIdentity(
        toolchain=toolchain.decode("utf-8").strip() or None if toolchain is not None else None,
        dependencies=dependency_fingerprint(raw) if raw is not None else None,
        manifest_sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
    )


def project_environment(root: str | Path, *, legacy: bool = False) -> tuple[str, str]:
    """The project identity a connection records; placeholders stand for absent files."""
    return project_identity(root).journal(legacy=legacy)


def environment_name(build: Mapping[str, Any]) -> str:
    return str(build.get("environment_id") or build.get("bundle_id") or "another environment")


def selects(bundle: Mapping[str, Any], requested: str | None) -> bool:
    """Whether a listed build satisfies an explicit environment choice.

    The service lists each environment's current build, so a build or environment
    identity selects it; a superseded build of the same environment does too.
    """
    if requested is None:
        return True
    if requested == bundle.get("bundle_id"):
        return True
    environment = bundle.get("environment_id")
    build = re.fullmatch(r"(.+)-[0-9a-f]{20}", requested)
    return environment is not None and environment in {
        requested,
        build.group(1) if build else None,
    }


Match = Literal["exact", "requested", "unpinned"]


@dataclass(frozen=True)
class EnvironmentChoice:
    """The served build a project runs on, and why it was chosen."""

    build: Mapping[str, Any]
    match: Match
    matches_project: bool

    def public(self) -> dict[str, Any]:
        return {
            "environment": environment_name(self.build),
            "lean_toolchain": self.build["lean_toolchain"],
            "match": self.match,
            "matches_project": self.matches_project,
        }


def choose_environment(
    identity: ProjectIdentity,
    catalog: Sequence[Mapping[str, Any]],
    requested: str | None = None,
    *,
    bundle_id: str | None = None,
) -> EnvironmentChoice:
    """Pick a served environment for a project.

    A project that pins a toolchain or lockfile runs where those match exactly, so
    LeanWarp checks against the same Lean and Mathlib as a local build. A project
    that pins nothing runs on the newest served stable Lean release. Anything else needs an
    explicit choice: the files then run against that environment's dependencies.
    """
    validate_selection(requested, bundle_id)
    if requested is not None:
        requested_builds = [build for build in catalog if selects(build, requested)]
        if not requested_builds:
            raise ProjectError(
                f"LeanWarp does not serve the environment {requested!r}. "
                "Run `leanwarp environments` to list the ones it does."
            )
        return EnvironmentChoice(
            requested_builds[-1], "requested", identity.matches(requested_builds[-1])
        )
    matching = [build for build in catalog if identity.matches(build) and selects(build, bundle_id)]
    if matching:
        pinned = identity.toolchain is not None or identity.manifest_sha256 is not None
        chosen = matching[-1] if pinned or bundle_id is not None else _default_environment(matching)
        return EnvironmentChoice(chosen, "exact" if pinned else "unpinned", True)
    served = ", ".join(sorted({environment_name(build) for build in catalog})) or "none"
    raise ProjectError(
        f"No LeanWarp environment matches this project "
        f"({identity.toolchain or 'its lockfile'}). Served environments: "
        f"{served}. To check these files against one anyway, run "
        "`leanwarp connect --environment NAME`; results then follow that environment's "
        "Lean and Mathlib rather than your local build."
    )


def validate_selection(environment: str | None, bundle_id: str | None) -> None:
    if environment is not None and bundle_id is not None:
        raise ProjectError("choose either environment or the legacy bundle_id selector, not both")


def _default_environment(catalog: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Default to the newest served stable Lean release, never build insertion order."""
    if len(catalog) == 1:
        return catalog[0]
    releases: list[tuple[tuple[int, int, int], Mapping[str, Any]]] = []
    for build in catalog:
        match = re.fullmatch(
            r"leanprover/lean4:v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
            build["lean_toolchain"],
        )
        if match is not None:
            major, minor, patch = match.groups()
            releases.append(((int(major), int(minor), int(patch)), build))
    if releases:
        latest = max(version for version, _ in releases)
        choices = [build for version, build in releases if version == latest]
        if len(choices) == 1:
            return choices[0]
    raise ProjectError(
        "There is no unambiguous default environment. Run `leanwarp environments`, then "
        "choose with `leanwarp connect --environment NAME`."
    )


def collect_lean_sources(
    root: str | Path, *, max_file_bytes: int = 1024 * 1024, max_total_bytes: int = 1024 * 1024
) -> dict[str, str]:
    """Read regular UTF-8 .lean files below root, excluding links and hidden paths.

    The selected environment supplies the toolchain and dependencies.
    Symlinks are errors rather than silently dereferenced outside the project.
    """
    project = Path(root).resolve(strict=True)
    if not project.is_dir():
        raise ProjectError(f"{project} is not a directory")
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
                raise ProjectError(
                    f"{(Path(directory) / name).relative_to(project)} is a symlinked directory; "
                    "LeanWarp uploads only regular files inside the project"
                )
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
                raise ProjectError(
                    f"{relative} is not an uploadable Lean module path: use ASCII letters, "
                    "digits and underscores, at most 240 characters, outside Init, Lean, Lake, "
                    "Std and Mathlib"
                )
            # O_NOFOLLOW rejects a file replaced by a symlink during discovery.
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ProjectError(f"{relative} must be a regular file")
                raw = source.read(max_file_bytes + 1)
            total += len(raw) + len(relative.encode("utf-8"))
            if len(raw) > max_file_bytes or total > max_total_bytes or len(sources) >= 256:
                raise ProjectError(
                    "the project's Lean files exceed the upload limit of 256 files and 1 MiB; "
                    "move unrelated files out of the project directory"
                )
            try:
                sources[relative] = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ProjectError(f"{relative} is not UTF-8 text") from error
    return sources

"""Read what a local `lake build` reads first, to find or build its LeanWarp environment.

The SDK never runs Lake and never clones anything. It reads the project's
`lean-toolchain`, its lakefile, `lake-manifest.json` and the optional
`.lake/package-overrides.json`, and snapshots each path dependency's source tree.
LeanWarp reads them itself, fetches pinned Git dependencies and builds the set, so
the user does nothing beyond what a local `lake build` needs.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from .errors import ProjectError
from .project import _IGNORE_FILE_BYTES, IGNORE_FILES, _has_lean_files, _ignore_matcher

LAKEFILES = ("lakefile.lean", "lakefile.toml")
TREE_SCHEMA = "leanwarp.tree.v1"
# One blob is one request body; the service refuses larger ones.
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_TREE_FILES = 20_000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
# Where Lake keeps git dependencies: the layout LeanWarp builds in.
PACKAGES_DIRECTORY = ".lake/packages"
# A path dependency's build outputs and version control are never its sources.
_SKIPPED = frozenset({".lake", ".git"})


@dataclass(frozen=True)
class ProjectInputs:
    """The environment request for a project and the blobs it names."""

    request: dict[str, Any]
    # The blobs the request names, in the order they must be uploaded.
    blobs: dict[str, bytes]

    @property
    def digest(self) -> str:
        """What a connection journals: any change to the inputs changes it."""
        return _sha256(_canonical(self.request))

    @property
    def plain(self) -> bool:
        return "lakefile" not in self.request

    def trees(self) -> list[dict[str, Any]]:
        """Each path tree the request sends: its directory, and each file's path and size."""
        return [
            {
                "dir": tree["dir"],
                "files": [
                    {"path": entry["path"], "bytes": entry["size"]}
                    for entry in json.loads(self.blobs[tree["sha256"]])["files"]
                ],
            }
            for tree in self.request.get("path_trees", ())
        ]


def dependency_files(root: str | Path) -> list[dict[str, Any]]:
    """What connect sends of each path dependency, read as connect reads it; nothing
    for a directory outside any Lake project."""
    return project_inputs(root).trees() if lake_root(root) is not None else []


def lake_root(start: str | Path) -> Path | None:
    """The nearest enclosing directory with a lakefile, as Lake itself would find it."""
    here = Path(start).resolve(strict=True)
    for directory in (here, *here.parents):
        if any((directory / name).is_file() for name in LAKEFILES):
            return directory
    return None


def project_inputs(
    root: str | Path,
    *,
    default_toolchain: Callable[[], str | None] | None = None,
    allow_empty: bool = False,
) -> ProjectInputs:
    """Read a project's inputs. A directory of plain Lean files outside any Lake
    project sends only its toolchain: its own `lean-toolchain`, or else the one
    ``default_toolchain`` gives, asked only then."""
    project = lake_root(root)
    if project is None:
        directory = Path(root).resolve(strict=True)
        if not allow_empty and not _has_lean_files(directory):
            raise ProjectError(
                f"{directory} has no Lean files in it or its subdirectories and is not in a "
                "Lake project; run from your project directory or pass --project"
            )
        toolchain = _optional(directory / "lean-toolchain")
        chosen: str | None = None
        if toolchain is not None:
            chosen = toolchain.decode("utf-8").strip()
        elif default_toolchain is not None:
            chosen = default_toolchain()
        if not chosen:
            raise ProjectError(
                "this directory is not in a Lake project and has no lean-toolchain; "
                "add one, or run from your Lake project"
            )
        return ProjectInputs(request={"lean_toolchain": chosen}, blobs={})
    # Every read below goes through descriptors opened from "/" on this one real path,
    # never resolved again and never through a link.
    parts = project.parts[1:]
    anchor = _open_path(parts, str(project))
    read: dict[tuple[str, ...], Seen] = {}
    try:
        start = _identity(os.fstat(anchor))
        toolchain = _read_at(anchor, ("lean-toolchain",), read)
        manifest = _read_at(anchor, ("lake-manifest.json",), read)
        lakefile_name, lakefile = next(
            (
                (name, data)
                for name in LAKEFILES
                if (data := _read_at(anchor, (name,), read)) is not None
            ),
            (None, None),
        )
        overrides = _read_at(anchor, (".lake", "package-overrides.json"), read)
        # Each filesystem git checkout must be at its locked commit: Lake checked it
        # out; the SDK reads its HEAD and never runs git.
        local = _local_git_dependencies(manifest, overrides) if manifest is not None else []
        for name, rev in local:
            _confirm_head(anchor, name, rev, read)
    finally:
        os.close(anchor)
    if toolchain is None or not toolchain.strip():
        raise ProjectError(f"{project} has no lean-toolchain; Lake needs one to build")
    if manifest is None:
        raise ProjectError(
            f"{project} has no lake-manifest.json; run `lake update` (or `lake build`) once"
        )
    if lakefile_name is None or lakefile is None:
        raise ProjectError(f"{project} changed while it was read; try again")
    _refuse_lakefile_secret(lakefile_name, lakefile)
    contents: dict[str, bytes] = {}
    listings: dict[str, list[str]] = {}
    request: dict[str, Any] = {
        "lean_toolchain": toolchain.decode("utf-8").strip(),
        "lakefile": {"name": lakefile_name, "sha256": _sha256(lakefile)},
        "manifest": manifest.decode("utf-8"),
    }
    if overrides is not None:
        request["package_overrides"] = overrides.decode("utf-8")
    trees = []
    declared = _path_trees(manifest, overrides)
    snapshots = [(dependency, _snapshot(parts, dependency)) for dependency, _ in declared]
    holders = [(_dependency_parts(parts, tree), captured) for tree, (*_, captured) in snapshots]
    _refuse_lake_secrets(
        [(tree, config, _dependency_parts(parts, tree)) for tree, config in declared], holders
    )
    for dependency, (listing, files, _) in snapshots:
        digest = _sha256(listing)
        contents.update(files)
        contents[digest] = listing
        listings[digest] = list(files)
        trees.append({"dir": dependency, "sha256": digest})
    # The project read is the one still at its path, every file read unchanged and
    # every file found absent still absent.
    # Both through one fresh descriptor, so they describe the same directory. These
    # files are small: their bytes are compared too, since a same-size edit within
    # one tick of a coarse file clock leaves their stamps as they were.
    anchor = _open_path(parts, str(project))
    try:
        if _identity(os.fstat(anchor)) != start or any(
            _seen_at(anchor, names) != seen for names, seen in read.items()
        ):
            raise ProjectError(f"{project} changed while it was read; try again")
    except OSError:
        raise ProjectError(f"{project} changed while it was read; try again") from None
    finally:
        os.close(anchor)
    if trees:
        request["path_trees"] = trees
    contents[_sha256(lakefile)] = lakefile
    return ProjectInputs(
        request=request,
        blobs=_upload_order(contents, listings, [tree["sha256"] for tree in trees]),
    )


def _upload_order(
    contents: dict[str, bytes], listings: dict[str, list[str]], roots: list[str]
) -> dict[str, bytes]:
    """Every blob after each digest it names, so an interrupted upload never leaves a
    stored listing whose files are missing. A digest can be a file of one tree and
    the listing of another; it then waits for its own files too."""
    ordered: dict[str, bytes] = {}

    def place(digest: str) -> None:
        if digest in ordered:
            return
        for named in listings.get(digest, ()):
            place(named)
        ordered[digest] = contents[digest]

    for digest in [*roots, *contents]:
        place(digest)
    return ordered


# Sent as they are read, so a credential in them would leave the machine.
_NO_SECRETS = (
    "LeanWarp never sends one. Remove it, and connect private GitHub repositories in "
    "the dashboard instead: https://isagoge.in/dashboard"
)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
# A `scheme://user:password@` URL, or any `http(s)://user@` one.
_LAKEFILE_SECRET = re.compile(
    r'[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#@"]*:[^\s/?#@"]*@|https?://[^\s/?#@"]+@',
    re.IGNORECASE,
)
# A URL a lakefile quotes, as Lake reads a dependency's: held to the manifest's rule.
_QUOTED_URL = re.compile(
    r'"([A-Za-z][A-Za-z0-9+.-]*://[^"\s]*)"|\'([A-Za-z][A-Za-z0-9+.-]*://[^\'\s]*)\''
)


def _secret_in_url(url: str) -> bool:
    """Whether a package URL carries a credential: a password in any URL, any user or
    query in one not over ssh, or a password in an scp-like ``user:password@host:path``.
    A path on this machine carries none."""
    if url.startswith(("/", "file:///")):
        return False
    if scheme := _SCHEME.match(url):
        rest = url[scheme.end() :]
        user, at, _ = re.split(r"[/?#]", rest, maxsplit=1)[0].rpartition("@")
        over_ssh = scheme[0].removesuffix("://").casefold() == "ssh"
        query = "?" in rest.partition("#")[0]
        return ":" in user or (not over_ssh and (bool(at) or query))
    user, at, _ = url.partition("@")
    return bool(at) and ":" in user


def _refuse_lakefile_secret(label: str, lakefile: bytes) -> None:
    """A lakefile is uploaded as it is read: one with a credential in a URL is refused."""
    text = lakefile.decode("utf-8", "replace")
    found = _LAKEFILE_SECRET.search(text) or next(
        (url for url in _QUOTED_URL.finditer(text) if _secret_in_url(url[1] or url[2])), None
    )
    if found:
        line = text.count("\n", 0, found.start()) + 1
        raise ProjectError(
            f"{label} has a URL with a credential in it, on line {line}; {_NO_SECRETS}"
        )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object with each key once: the raw document is sent, so a repeated key
    would send a value the checks never saw."""
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _manifest_packages(raw: bytes, source: str) -> list[Any]:
    """A Lake manifest's package entries."""
    try:
        document = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as error:
        raise ProjectError(f"{source} is not valid JSON; run `lake update`") from error
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, list):
        raise ProjectError(f"{source} is not a Lake manifest; run `lake update`")
    return packages


def _refuse_url_secrets(packages: list[Any], source: str) -> None:
    """A manifest is uploaded as it is read: one naming a URL with a credential in it
    is refused, by the package, never by the URL."""
    for package in packages:
        if (
            isinstance(package, dict)
            and isinstance(package.get("url"), str)
            and _secret_in_url(package["url"])
        ):
            name = package.get("name")
            raise ProjectError(
                f"{source} gives the package {name if isinstance(name, str) else '?'} a "
                f"URL with a credential in it; {_NO_SECRETS}"
            )


def _effective(manifest: bytes, overrides: bytes | None) -> list[dict[str, Any]]:
    """The effective lock: the manifest's entries, each override replacing its own.
    Either document naming a URL with a credential in it is refused."""
    packages: dict[str, Any] = {}
    documents = ((manifest, "lake-manifest.json"), (overrides, ".lake/package-overrides.json"))
    for raw, source in documents:
        if raw is None:
            continue
        entries = _manifest_packages(raw, source)
        _refuse_url_secrets(entries, source)
        for package in entries:
            if isinstance(package, dict) and isinstance(package.get("name"), str):
                packages[package["name"]] = package
    return list(packages.values())


def _local_git(package: Mapping[str, Any]) -> bool:
    """A filesystem git dependency: a repository on this machine."""
    return (
        package.get("type") == "git"
        and isinstance(package.get("url"), str)
        and package["url"].startswith(("/", "file:///"))
    )


def _path_trees(manifest: bytes, overrides: bytes | None) -> list[tuple[str, object]]:
    """Each directory a connection sends as a tree, with the lakefile its lock entry
    names (`configFile`), if any: every path dependency's, then each filesystem git
    dependency's checkout in the packages directory. A path dependency Lake copies
    (`copy`) is its copy there, which Lake keeps using whatever becomes of the
    original."""
    packages = _effective(manifest, overrides)
    paths = [
        (
            f"{PACKAGES_DIRECTORY}/{_package_name(package['name'])}"
            if package.get("copy") is True
            else os.path.normpath(package["dir"]),
            package.get("configFile"),
        )
        for package in packages
        if package.get("type") == "path" and isinstance(package.get("dir"), str)
    ]
    checkouts = [
        (f"{PACKAGES_DIRECTORY}/{_package_name(package['name'])}", package.get("configFile"))
        for package in packages
        if _local_git(package)
    ]
    return [
        *sorted(paths, key=lambda tree: tree[0]),
        *sorted(checkouts, key=lambda tree: tree[0]),
    ]


def _package_name(name: object) -> str:
    """The directory Lake keeps a lock entry's package in: its name printed without
    escapes (the lock's `«my-dep»` is `my-dep`), one safe path component, never a
    way out of the packages directory."""
    directory = None
    if isinstance(name, str):
        parts, current, escaped = [], "", False
        for character in name:
            if character == "«" and not escaped:
                escaped = True
            elif character == "»" and escaped:
                escaped = False
            elif character == "." and not escaped:
                parts.append(current)
                current = ""
            else:
                current += character
        parts.append(current)
        directory = ".".join(parts)
    if (
        directory is None
        or directory in ("", ".", "..")
        or "/" in directory
        or "\\" in directory
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in directory)
    ):
        raise ProjectError(
            f"lake-manifest.json names a package {name!r} that is not a valid name; run "
            "`lake update`"
        )
    return directory


def _local_git_dependencies(manifest: bytes, overrides: bytes | None) -> list[tuple[str, str]]:
    """Each filesystem git dependency (a repository on this machine), by name and the
    commit the lock pins: its checkout in the packages directory is uploaded."""
    return sorted(
        (_package_name(package["name"]), str(package.get("rev")))
        for package in _effective(manifest, overrides)
        if _local_git(package)
    )


def _confirm_head(anchor: int, name: str, rev: str, read: dict[tuple[str, ...], Seen]) -> None:
    """The checkout Lake made of a filesystem git dependency is at its locked commit."""
    checkout = (*PACKAGES_DIRECTORY.split("/"), name)
    label = f"the git dependency {name}"
    head = _read_at(anchor, (*checkout, ".git", "HEAD"), read)
    if head is None:
        raise ProjectError(f"{label} is not checked out; run `lake build` once")
    text = head.decode("utf-8", "replace").strip()
    if text.startswith("ref: "):
        # A branch: its commit, from its loose ref or the packed refs.
        ref = text.removeprefix("ref: ").strip()
        # Below the checkout's own repository, one plain component at a time.
        if any(part in ("", ".", "..") or "\\" in part for part in ref.split("/")):
            raise ProjectError(f"{label} has an unreadable HEAD; run `lake build` once")
        loose = _read_at(anchor, (*checkout, ".git", *ref.split("/")), read)
        if loose is not None:
            text = loose.decode("utf-8", "replace").strip()
        else:
            packed = _read_at(anchor, (*checkout, ".git", "packed-refs"), read) or b""
            text = next(
                (
                    line.split(" ", 1)[0]
                    for line in packed.decode("utf-8", "replace").splitlines()
                    if line.endswith(f" {ref}")
                ),
                "",
            )
    if text != rev:
        raise ProjectError(
            f"{label} is checked out at another commit than lake-manifest.json pins; run "
            "`lake build` (or `lake update`)"
        )


# Opened only through verified directory descriptors: never a link, at any depth.
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class Identity(NamedTuple):
    """What a captured entry was: any change to it shows here."""

    directory: bool
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int


def _identity(status: os.stat_result) -> Identity:
    directory = stat.S_ISDIR(status.st_mode)
    return Identity(
        directory=directory,
        device=status.st_dev,
        inode=status.st_ino,
        size=0 if directory else status.st_size,
        modified_ns=status.st_mtime_ns,
        changed_ns=status.st_ctime_ns,
        mode=status.st_mode,
    )


def _open_path(parts: tuple[str, ...] | list[str], label: str) -> int:
    """A directory descriptor for a real path, each component opened from the last
    without following a link, starting at "/"."""
    descriptor = os.open("/", _DIRECTORY)
    try:
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                raise ProjectError(f"{label} does not exist; run `lake update`") from None
            except OSError:
                raise ProjectError(
                    f"{label} is not a directory, or is reached through a link"
                ) from None
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _confirm(parts: tuple[str, ...] | list[str], expected: Identity, label: str) -> None:
    """The path still names the directory that was read."""
    descriptor = _open_path(parts, label)
    try:
        if _identity(os.fstat(descriptor)) != expected:
            raise ProjectError(f"{label} changed while it was read; try again")
    finally:
        os.close(descriptor)


def _dependency_parts(project: tuple[str, ...], directory: str) -> list[str]:
    """A path dependency's real path components, as Lake names it relative to the
    project: at most a few climbs out of it, lexically, then its own components."""
    normal = os.path.normpath(directory)
    parts = normal.split("/")
    climbs = 0
    while climbs < len(parts) and parts[climbs] == "..":
        climbs += 1
    if (
        os.path.isabs(directory)
        or "\\" in directory
        or ".." in parts[climbs:]
        or climbs > min(8, len(project))
    ):
        raise ProjectError(f"the path dependency {directory} must be a relative directory")
    resolved = [
        *project[: len(project) - climbs],
        *(part for part in parts[climbs:] if part != "."),
    ]
    if any(_private_name(part) for part in resolved):
        raise ProjectError("a path dependency cannot be in a credential location")
    return resolved


def _snapshot(
    project: tuple[str, ...], directory: str
) -> tuple[bytes, dict[str, bytes], dict[str, bytes]]:
    """A path dependency's source tree, by content, refusing links and changes: what a
    clone of it holds, so not what its own top-level ignore files leave out. Its
    listing, its files by digest, and the bytes captured at each path."""
    parts = _dependency_parts(project, directory)
    label = f"the path dependency {directory}"
    root = _open_path(parts, label)
    try:
        start = _identity(os.fstat(root))
        rules, ignored = _tree_rules(root, label)
        before: dict[str, Identity] = {}
        captured: dict[str, bytes] = {}
        _walk(root, "", directory, before, captured, [MAX_TREE_BYTES], ignored)
    finally:
        os.close(root)
    # One snapshot: the declared directory is still the one read, before and after
    # a second walk under the same rules finds every entry in it unchanged.
    again = _open_path(parts, label)
    try:
        after: dict[str, Identity] = {}
        if _identity(os.fstat(again)) != start or _ignore_texts(again, label) != rules:
            raise ProjectError(f"{label} changed while it was read; try again")
        _walk(again, "", directory, after, None, [MAX_TREE_BYTES], ignored)
    finally:
        os.close(again)
    if after != before:
        raise ProjectError(f"{label} changed while it was read; try again")
    _confirm(parts, start, label)
    entries: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for relative, data in sorted(captured.items()):
        digest = _sha256(data)
        files[digest] = data
        entries.append(
            {
                "path": relative,
                "sha256": digest,
                "size": len(data),
                "executable": bool(before[relative].mode & stat.S_IXUSR),
            }
        )
    listing = _canonical({"schema": TREE_SCHEMA, "files": entries})
    return listing, files, captured


def _refuse_lake_secrets(
    trees: list[tuple[str, object, list[str]]],
    holders: list[tuple[list[str], dict[str, bytes]]],
) -> None:
    """Each path dependency's own Lake files are checked as the project's are: every
    top-level manifest first, by its packages (as text if it is not one), then each
    lakefile, by its usual names and the ``config`` name its lock entry gives; each
    file once. Every copy any tree uploads is checked, found by the file's own path,
    whatever tree holds it and whatever its own ignore rules leave out."""

    def copies(file: list[str]) -> Iterator[bytes]:
        for holder, captured in holders:
            if file[: len(holder)] == holder:
                data = captured.get("/".join(file[len(holder) :]))
                if data is not None:
                    yield data

    checked = []
    for directory, _, top in trees:
        source = f"{directory}/lake-manifest.json"
        if [*top, "lake-manifest.json"] in checked:
            continue
        checked.append([*top, "lake-manifest.json"])
        for data in copies(checked[-1]):
            try:
                packages = _manifest_packages(data, source)
            except ProjectError:
                # Not a manifest Lake reads; its text is still uploaded.
                _refuse_lakefile_secret(source, data)
            else:
                _refuse_url_secrets(packages, source)
    for directory, config, top in trees:
        for name in [*LAKEFILES, *([config] if isinstance(config, str) else [])]:
            # As Lake resolves it: from the dependency, or as it is if absolute.
            file = list(Path(os.path.normpath(os.path.join("/", *top, name))).parts[1:])
            if file in checked:
                continue
            checked.append(file)
            for data in copies(file):
                _refuse_lakefile_secret(os.path.normpath(os.path.join(directory, name)), data)


def _tree_rules(
    descriptor: int, label: str
) -> tuple[dict[str, tuple[Identity, bytes]], Callable[[str], bool]]:
    """A tree's top-level ignore files as ``_ignore_texts`` reads them, and their
    matcher for paths relative to the tree (a directory's with a trailing ``/``)."""
    texts = _ignore_texts(descriptor, label)
    try:
        return texts, _ignore_matcher({name: data for name, (_, data) in texts.items()})
    except ProjectError as error:
        raise ProjectError(f"{label}: {error}") from None


def _ignore_texts(descriptor: int, label: str) -> dict[str, tuple[Identity, bytes]]:
    """A tree's top-level ignore files, read through its descriptor, each with its
    identity and bytes, refused if its identity changes while read. Reads that match
    later show each file unchanged between them, as far as its file times can show a
    write. As for git, a link or anything but a regular file there is no ignore file."""
    texts: dict[str, tuple[Identity, bytes]] = {}
    for name in IGNORE_FILES:
        try:
            handle = os.open(name, _FILE, dir_fd=descriptor)
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno == errno.ELOOP:
                continue
            raise
        try:
            before = _identity(os.fstat(handle))
            if not stat.S_ISREG(before.mode):
                continue
            with os.fdopen(handle, "rb", closefd=False) as source:
                data = source.read(_IGNORE_FILE_BYTES + 1)
            if _identity(os.fstat(handle)) != before:
                raise ProjectError(f"{label} changed while it was read; try again")
            texts[name] = (before, data)
        finally:
            os.close(handle)
    return texts


def _walk(
    descriptor: int,
    prefix: str,
    dependency: str,
    found: dict[str, Identity],
    captured: dict[str, bytes] | None,
    budget: list[int],
    ignored: Callable[[str], bool],
) -> None:
    """Every entry below an open directory but build outputs, credentials and what
    ``ignored`` matches (a matched directory is never entered), by identity, reading
    each file through this directory's descriptor when ``captured`` is given;
    ``budget`` holds the bytes the tree may still add."""
    for name in sorted(os.listdir(descriptor)):
        relative = f"{prefix}{name}"
        if name in _SKIPPED or _private_name(name):
            # Build outputs and repository metadata, a submodule's `.git` file included.
            continue
        status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if ignored(f"{relative}/" if stat.S_ISDIR(status.st_mode) else relative):
            continue
        if stat.S_ISLNK(status.st_mode):
            raise ProjectError(
                f"{relative} in a path dependency is a link; LeanWarp uploads files only"
            )
        if stat.S_ISDIR(status.st_mode):
            found[relative] = _identity(status)
            child = os.open(name, _DIRECTORY, dir_fd=descriptor)
            try:
                if _identity(os.fstat(child)) != found[relative]:
                    raise ProjectError(f"the path dependency {dependency} changed; try again")
                _walk(child, f"{relative}/", dependency, found, captured, budget, ignored)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(status.st_mode):
            raise ProjectError(f"{relative} in a path dependency is not a regular file")
        found[relative] = _identity(status)
        if captured is None:
            continue
        budget[0] -= status.st_size
        if len(captured) >= MAX_TREE_FILES or budget[0] < 0:
            raise ProjectError(
                f"the path dependency {dependency} is too large to upload; contact "
                "contact@isagoge.in"
            )
        handle = os.open(name, _FILE, dir_fd=descriptor)
        with os.fdopen(handle, "rb") as source:
            opened = _identity(os.fstat(source.fileno()))
            data = source.read(MAX_BLOB_BYTES + 1)
            read = _identity(os.fstat(source.fileno()))
        if len(data) > MAX_BLOB_BYTES:
            raise ProjectError(f"{dependency}/{relative} exceeds {MAX_BLOB_BYTES // 2**20} MiB")
        # The bytes are the recorded file's, unchanged while they were read.
        if not opened == read == found[relative] or len(data) != status.st_size:
            raise ProjectError(f"the path dependency {dependency} changed while it was read")
        captured[relative] = data


# A small file as it was read: its identity and its bytes' digest; None if absent.
Seen = tuple[Identity, str] | None


def _seen_at(anchor: int, names: tuple[str, ...]) -> Seen:
    """A small file below a directory descriptor as it is now: read again."""
    again: dict[tuple[str, ...], Seen] = {}
    _read_at(anchor, names, again)
    return again[names]


def _read_at(
    anchor: int, names: tuple[str, ...], read: dict[tuple[str, ...], Seen]
) -> bytes | None:
    """A file below a directory descriptor, every component opened without following
    a link; None if it does not exist. ``read`` records what was found, absence
    included, so the caller can confirm it later."""
    label = "/".join(names)
    descriptor = os.dup(anchor)
    try:
        for name in names[:-1]:
            try:
                child = os.open(name, _DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                read[names] = None
                return None
            os.close(descriptor)
            descriptor = child
        try:
            handle = os.open(names[-1], _FILE, dir_fd=descriptor)
        except FileNotFoundError:
            read[names] = None
            return None
    finally:
        os.close(descriptor)
    # This function alone owns the descriptor and closes it once.
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectError(f"{label} must be a regular file")
        with os.fdopen(handle, "rb", closefd=False) as source:
            data = source.read(MAX_METADATA_BYTES + 1)
        after = os.fstat(handle)
    finally:
        os.close(handle)
    if len(data) > MAX_METADATA_BYTES:
        raise ProjectError(f"{label} exceeds {MAX_METADATA_BYTES // (1024 * 1024)} MiB")
    if _identity(before) != _identity(after) or len(data) != after.st_size:
        raise ProjectError(f"{label} changed while it was read; try again")
    read[names] = (_identity(after), _sha256(data))
    return data


def _optional(path: Path) -> bytes | None:
    try:
        return _read(path)
    except FileNotFoundError:
        return None


def _read(path: Path, *, limit: int = MAX_METADATA_BYTES, label: str | None = None) -> bytes:
    """A regular file's bytes, read once; refused if it changes while read."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ProjectError(f"{label or path.name} must be a regular file")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) > limit:
        raise ProjectError(f"{label or path.name} exceeds {limit // (1024 * 1024)} MiB")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or (
        len(data) != after.st_size
    ):
        raise ProjectError(f"{label or path.name} changed while it was read; try again")
    return data


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Never answered or uploaded: where local credentials and repository internals live,
# and well-known credential files.
_PRIVATE = frozenset(
    {
        *(".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config"),
        *(".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass", ".htpasswd"),
        *("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"),
    }
)
_PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _private_name(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in _PRIVATE
        or folded == ".env"
        or folded.startswith(".env.")
        or folded.endswith(_PRIVATE_SUFFIXES)
    )


MAX_ANSWER_BYTES = 8 * 1024 * 1024
MAX_LISTING = 256


def project_answers(
    root: str | Path,
    paths: list[str],
    *,
    committed: dict[str, Commit] | None = None,
    stamps: dict[str, Stamp] | None = None,
    deadline: float = math.inf,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    """What the project's own files hold at each path its hosted build read: a file's
    digest and size, confirmed absence or a sorted listing; and the files' bytes.

    Each path is relative to the project and read through descriptors from "/"
    without following a link; one that climbs too far, names a credential location
    or the project's own Lake directory, or passes through a link is refused. One
    outside the project is answered only below a path dependency's directory or
    a directory the project's ``.leanwarpread`` names, one per line. One below a path
    dependency's directory is refused where that tree's ignore rules leave it out.

    With ``committed``, each file answered as its repository's ``HEAD`` holds it is
    recorded there with that commit: ``changed_answers`` holds it to the commit, not
    to the copy on disk."""
    project = lake_root(root)
    if project is None:
        raise ProjectError(f"{root} is not in a Lake project")
    readable, trees = _readable(project.parts[1:])
    rules: dict[str, Callable[[str], bool]] = {}
    answers: dict[str, dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    for path in paths:
        parts = _readable_parts(project, readable, path)
        refuse = functools.partial(_refuse_ignored, project.parts[1:], trees, rules, parts, path)
        refuse(None)
        answers[path] = _answer_at(parts, path, blobs, stamps, refuse)
        if committed is not None:
            committed.pop(path, None)
            if answers[path]["kind"] == "file":
                commit = _commit_of(parts, blobs[answers[path]["sha256"]], deadline=deadline)
                if commit is not None:
                    committed[path] = commit
    return answers, blobs


def _readable_parts(project: Path, readable: tuple[str, ...], path: str) -> list[str]:
    parts = _answer_parts(project.parts[1:], path)
    if (path == ".." or path.startswith("../")) and not any(
        _within(path, place) for place in readable
    ):
        raise ProjectError(
            f"the build read {path}, outside the project and its path dependencies; "
            "name its directory in .leanwarpread if the build may read it"
        )
    return parts


@dataclass(frozen=True)
class Commit:
    """The repository a file was answered from, and its commit then."""

    repository: str
    head: str


def _commit_fingerprint(file: str, repository: str, head: str) -> str:
    return "git:" + _sha256(_canonical([os.path.relpath(file, repository), head]))


def answer_fingerprints(
    root: str | Path, answers: Mapping[str, Any], commits: Mapping[str, Commit]
) -> dict[str, str]:
    """Save only identities of the observations used by the accepted build."""
    fingerprints = {path: _sha256(_canonical(answer)) for path, answer in answers.items()}
    if commits and (project := lake_root(root)) is not None:
        for path, commit in commits.items():
            if path in answers:
                file = os.path.normpath(project / path)
                fingerprints[path] = _commit_fingerprint(file, commit.repository, commit.head)
    return fingerprints


def observations_match(root: str | Path, saved: Mapping[str, str]) -> bool:
    if not saved:
        return True
    project = lake_root(root)
    if project is None:
        return False
    readable = _readable(project.parts[1:])[0]
    heads: dict[str, str | None] = {}
    for path, expected in saved.items():
        parts = _readable_parts(project, readable, path)
        if expected.startswith("git:"):
            directory = "/" + "/".join(parts[:-1])
            # Refuse redirected parents before locating the current checkout.
            os.close(_open_path(parts[:-1], path))
            top = _git(directory, "rev-parse", "--show-toplevel")
            if not top:
                return False
            if top not in heads:
                heads[top] = head_of(top)
            head = heads[top]
            if not head:
                return False
            actual = _commit_fingerprint("/" + "/".join(parts), top, head)
        else:
            # Release each file buffer after computing its identity.
            actual = _sha256(_canonical(_answer_at(parts, path, {})))
        if actual != expected:
            return False
    return True


def changed_answers(
    root: str | Path,
    answered: Mapping[str, Mapping[str, Any]],
    committed: Mapping[str, Commit],
    stamps: Mapping[str, Stamp] | None = None,
    *,
    deadline: float = math.inf,
) -> list[str]:
    """The answered paths that no longer hold: a committed one whose repository has
    moved to another commit, or any other whose copy on disk differs now or was
    written since its ``stamps`` were taken (so unchanged ones all held together
    since the last was read). Git is asked nothing past ``deadline``, a
    ``time.monotonic()`` instant."""
    heads: dict[str, str | None] = {}
    changed = []
    others = sorted(path for path in answered if path not in committed)
    now: dict[str, Stamp] = {}
    current = project_answers(root, others, stamps=now)[0] if others else {}
    for path, answer in answered.items():
        commit = committed.get(path)
        if commit is None:
            stamp = (stamps or {}).get(path)
            holds = current.get(path) == answer and (stamp is None or now.get(path) == stamp)
        else:
            if commit.repository not in heads:
                heads[commit.repository] = head_of(commit.repository, deadline=deadline)
            holds = heads[commit.repository] == commit.head
        if not holds:
            changed.append(path)
    return sorted(changed)


def head_of(repository: str, *, deadline: float = math.inf) -> str | None:
    """The commit a repository has checked out, or None when git cannot say."""
    return _git(repository, "rev-parse", "--verify", "HEAD", deadline=deadline)


# How git stores a regular file; a link is a blob too, of its target's text.
_REGULAR = frozenset({"100644", "100755"})


def _commit_of(parts: list[str], data: bytes, *, deadline: float = math.inf) -> Commit | None:
    """The repository holding the file at ``parts`` and its ``HEAD``, if that commit
    holds exactly these bytes there as a regular file; never git's filters, only the
    stored blob. Linked worktrees and submodules are found through git itself."""
    directory = "/" + "/".join(parts[:-1])
    top = _git(directory, "rev-parse", "--show-toplevel", deadline=deadline)
    head = _git(directory, "rev-parse", "--verify", "HEAD", deadline=deadline)
    form = _git(directory, "rev-parse", "--show-object-format", deadline=deadline)
    if top is None or head is None or form not in ("sha1", "sha256"):
        return None
    relative = os.path.relpath("/" + "/".join(parts), top)
    if relative.startswith("../"):
        return None
    listed = _git(top, "ls-tree", "-z", head, "--", relative, deadline=deadline)
    if not listed:
        return None
    # One entry, for exactly this path: names are matched literally, never as patterns.
    metadata, _, named = listed.split("\0")[0].partition("\t")
    entry = metadata.split(" ")
    blob = hashlib.new(form, b"blob %d\0" % len(data) + data).hexdigest()
    if (
        named != relative
        or len(entry) != 3
        or entry[0] not in _REGULAR
        or entry[1] != "blob"
        or entry[2] != blob
    ):
        return None
    return Commit(top, head)


def committed_bytes(
    root: str | Path, path: str, commit: Commit, deadline: float = math.inf
) -> bytes | None:
    """The bytes ``commit`` holds for a file answered at ``path`` (relative to the
    project ``root``), from git's object store alone; None if it cannot say."""
    # The answer's path is relative to the project, wherever the connect started.
    project = lake_root(root)
    if project is None:
        return None
    absolute = os.path.normpath(os.path.join(str(project), path))
    relative = os.path.relpath(absolute, commit.repository)
    budget = min(30.0, deadline - time.monotonic())
    if relative.startswith("../") or budget <= 0:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
            ["git", "-c", "core.fsmonitor=false", "-C", commit.repository,  # noqa: S607
             "cat-file", "blob", f"{commit.head}:{relative}"],
            capture_output=True, timeout=budget, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _git(repository: str, *argv: str, deadline: float = math.inf) -> str | None:
    """One read-only git query in ``repository``, or None when it cannot answer by
    ``deadline``."""
    budget = min(30.0, deadline - time.monotonic())
    if budget <= 0:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
            ["git", "-c", "core.fsmonitor=false", "-C", repository, *argv],  # noqa: S607
            capture_output=True,
            timeout=budget,
            env={
                **os.environ,
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _readable(
    project: tuple[str, ...],
) -> tuple[tuple[str, ...], list[tuple[str, list[str]]]]:
    """The directories outside the project a build may read: each path dependency's,
    and each its ``.leanwarpread`` names; and each tree a connection sends, by its
    directory and real path components."""
    files: dict[str, bytes] = {}
    for name in ("lake-manifest.json", ".lake/package-overrides.json", ".leanwarpread"):
        read: dict[str, bytes] = {}
        answer = _answer_at([*project, *name.split("/")], name, read)
        if answer["kind"] == "file":
            files[name] = read[answer["sha256"]]
    manifest = files.get("lake-manifest.json", b'{"packages": []}')
    overrides = files.get(".lake/package-overrides.json")
    sent = [tree for tree, _ in _path_trees(manifest, overrides)]
    places = [place for place in sent if place == ".." or place.startswith("../")]
    for line in files.get(".leanwarpread", b"").decode("utf-8", "replace").splitlines():
        place = line.strip()
        if not place or place.startswith("#"):
            continue
        try:
            _answer_parts(project, place)
        except ProjectError:
            raise ProjectError(
                f".leanwarpread names {place}, which LeanWarp never sends; name directories "
                "relative to the project, outside credential locations"
            ) from None
        parent = os.path.dirname(place)
        if place.startswith("../") and parent.split("/")[-1] != "..":
            # The hosted build reaches it through its parent, which must be one of the
            # project's own parents: LeanWarp lists nothing above a declared directory.
            raise ProjectError(
                f".leanwarpread names {place}, inside {parent}, which the build would have "
                f"to list; name {_top(place)} instead"
            )
        places.append(place)
    return tuple(places), [(tree, _dependency_parts(project, tree)) for tree in sent]


def _refuse_ignored(
    project: tuple[str, ...],
    trees: list[tuple[str, list[str]]],
    rules: dict[str, Callable[[str], bool]],
    parts: list[str],
    path: str,
    directory: bool | None,
) -> None:
    """What a tree's snapshot leaves out, its build never reads instead: a path its
    ignore rules match, or one below a directory they match (which, as for git, no
    later rule re-includes), is refused: with ``directory`` None, by the directories
    above the path; otherwise by the path itself, a directory or not.
    The deepest tree holding the path decides, and the project's own files are its
    own, even in a tree that holds the project; ``rules`` keeps each tree's matcher,
    read once."""
    holding = [(len(top), tree, top) for tree, top in trees if parts[: len(top)] == top]
    if not holding:
        return
    _, tree, top = max(holding)
    relative = parts[len(top) :]
    if not relative or (len(project) >= len(top) and tuple(parts[: len(project)]) == project):
        return
    if tree not in rules:
        label = f"the path dependency {tree}"
        root = _open_path(top, label)
        try:
            rules[tree] = _tree_rules(root, label)[1]
        finally:
            os.close(root)
    ignored = rules[tree]
    if (
        any(ignored("/".join(relative[:depth]) + "/") for depth in range(1, len(relative)))
        if directory is None
        else ignored("/".join(relative) + ("/" if directory else ""))
    ):
        raise ProjectError(
            f"the build read {path}, which the path dependency {tree} leaves out by its "
            "ignore rules; LeanWarp sends it as a clone of it holds it, so change the rule "
            "if the build needs this"
        )


def _top(place: str) -> str:
    """The directory directly below the project's parents that holds ``place``."""
    parts = place.split("/")
    climbs = parts.count("..")
    return "/".join(parts[: climbs + 1])


def _within(path: str, place: str) -> bool:
    """Whether the normalized relative ``path`` is ``place`` or below it: compared by
    components, so a further climb is never inside."""
    inner, outer = path.split("/"), place.split("/")
    return inner[: len(outer)] == outer and ".." not in inner[len(outer) :]


def _answer_parts(project: tuple[str, ...], path: str) -> list[str]:
    parts = [] if path == "." else path.split("/")
    climbs = 0
    while climbs < len(parts) and parts[climbs] == "..":
        climbs += 1
    rest = parts[climbs:]
    if (
        os.path.normpath(path) != path
        or path.startswith("/")
        or climbs > min(8, len(project))
        or any(part in ("", ".", "..") or _private_name(part) or "\\" in part for part in rest)
    ):
        raise ProjectError(f"the build read {path}, which LeanWarp never sends")
    resolved = [*project[: len(project) - climbs], *rest]
    # The project's own Lake directory, however the path names it.
    if resolved[: len(project) + 1] == [*project, ".lake"]:
        raise ProjectError(f"the build read {path}, which LeanWarp never sends")
    return resolved


Stamp = tuple[int, int, int, int, int]


def _stamp(descriptor: int) -> Stamp:
    """What any write to a file or directory changes: its identity, size and change
    times (the change time follows every write, whatever its bytes)."""
    status = os.fstat(descriptor)
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def _answer_at(
    parts: list[str],
    label: str,
    blobs: dict[str, bytes],
    stamps: dict[str, Stamp] | None = None,
    refuse: Callable[[bool], None] = lambda directory: None,
) -> dict[str, Any]:
    """The answer at ``parts``, and in ``stamps`` what it was read from: the file, the
    directory listed, or the deepest directory that shows the path absent. ``refuse``
    sees what the path is (a directory or not) before anything of it is read."""

    def stamped(descriptor: int, answer: dict[str, Any], before: Stamp) -> dict[str, Any]:
        if _stamp(descriptor) != before:
            raise ProjectError(f"{label} changed while LeanWarp read it; try again")
        if stamps is not None:
            stamps[label] = before
        return answer

    descriptor = os.open("/", _DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            except (FileNotFoundError, NotADirectoryError):
                refuse(False)
                return stamped(descriptor, {"kind": "absent"}, _stamp(descriptor))
            except OSError:
                raise ProjectError(f"the build read {label} through a link") from None
            os.close(descriptor)
            descriptor = child
        if not parts:
            refuse(True)
            before = _stamp(descriptor)
            return stamped(descriptor, _listing(descriptor, label), before)
        before = _stamp(descriptor)
        try:
            status = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            refuse(False)
            return stamped(descriptor, {"kind": "absent"}, before)
        refuse(stat.S_ISDIR(status.st_mode))
        if stat.S_ISLNK(status.st_mode):
            raise ProjectError(f"the build read {label}, which is a link")
        if stat.S_ISDIR(status.st_mode):
            child = os.open(parts[-1], _DIRECTORY, dir_fd=descriptor)
            try:
                before = _stamp(child)
                return stamped(child, _listing(child, label), before)
            finally:
                os.close(child)
        if not stat.S_ISREG(status.st_mode):
            raise ProjectError(f"the build read {label}, which is not a regular file")
        handle = os.open(parts[-1], _FILE, dir_fd=descriptor)
        try:
            before = _stamp(handle)
            with os.fdopen(handle, "rb", closefd=False) as source:
                data = source.read(MAX_ANSWER_BYTES + 1)
            if len(data) > MAX_ANSWER_BYTES:
                raise ProjectError(f"the build read {label}, which exceeds 8 MiB")
            digest = _sha256(data)
            answer = stamped(handle, {"kind": "file", "sha256": digest, "size": len(data)}, before)
        finally:
            os.close(handle)
        blobs[digest] = data
        return answer
    finally:
        os.close(descriptor)


def _listing(descriptor: int, label: str) -> dict[str, Any]:
    entries = []
    for name in sorted(os.listdir(descriptor)):
        status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        kind = (
            "directory"
            if stat.S_ISDIR(status.st_mode)
            else "file"
            if stat.S_ISREG(status.st_mode)
            else "other"
        )
        entries.append([name, kind])
    if len(entries) > MAX_LISTING:
        raise ProjectError(f"the build listed {label}, which holds more than {MAX_LISTING} entries")
    return {"kind": "directory", "entries": entries}

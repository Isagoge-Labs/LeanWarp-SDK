"""Lakefile options, the release dependency fingerprint and conservative source discovery.

Only `.lean` sources are uploaded to a workspace. The project's own environment,
built from its toolchain, lakefile and lockfile, supplies Lean and every dependency.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pathspec import GitIgnoreSpec

from . import _toml
from .errors import ProjectError, UsageError

_EXCLUDED = {".git", ".lake", ".env", ".ssh", ".aws", ".venv", "node_modules", "vendor"}
# Package fields that record how a dependency was requested rather than which
# sources were locked: they never change the dependency's identity.
_PROVENANCE_FIELDS = frozenset({"inputRev", "inherited"})


# Lean options LeanWarp applies from a lakefile. Others can skip kernel checks, lift
# execution limits or hide the warnings proof checking relies on.
LEAN_OPTION_NAMES = frozenset(
    {
        "autoImplicit",
        "relaxedAutoImplicit",
        "maxSynthPendingDepth",
        "maxHeartbeats",
        "maxRecDepth",
        "synthInstance.maxHeartbeats",
        "synthInstance.maxSize",
        "structureDiamondWarning",
    }
)
LEAN_OPTION_PREFIXES = ("pp.", "linter.", "backward.")
# One literal `LeanOption`, such as ⟨`maxSynthPendingDepth, .ofNat 3⟩. A string value
# is the placeholder `_lean_skeleton` leaves for a string literal.
_LAKEFILE_OPTION = re.compile(
    r"⟨\s*`([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*\(?\s*(?:\.of(?:Nat|Bool|String)\s+)?"
    r"(true|false|[0-9]+|\"[0-9]+\")\s*\)?\s*⟩"
)
_LAKEFILE_TARGET = re.compile(r"(?:@\[[^\]]*\]\s*)*(package|lean_lib|lean_exe)\b")
# A definition without parameters, whose name a field can refer to.
_LAKEFILE_DEFINITION = re.compile(
    r"(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable)\s+)*(?:abbrev|def)\s+"
    r"([A-Za-z_][A-Za-z0-9_']*)\s*(?::[^\n]*?)?:=(.*)",
    re.DOTALL,
)
_LAKEFILE_FIELD = re.compile(
    r"^([ \t]+)(leanOptions|moreLeanArgs|weakLeanArgs)\s*:=(.*)$", re.MULTILINE
)
# A literal list of string placeholders, the only Lean arguments read.
_LAKEFILE_STRINGS = re.compile(r'#\[\s*(?:"[0-9]+"\s*(?:,\s*"[0-9]+"\s*)*)?\]')
_LEAN_ARGUMENTS = ("moreLeanArgs", "weakLeanArgs")


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


@dataclass(frozen=True)
class ProjectLeanOptions:
    """The Lean options a lakefile sets: those LeanWarp applies, and the names it does not."""

    applied: dict[str, bool | int | str]
    ignored: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {"lean_options": self.applied, "lean_options_ignored": list(self.ignored)}


def _lakefile_text(project: Path, name: str) -> str | None:
    try:
        descriptor = os.open(project / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            return None
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _flattened(table: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for name, value in table.items():
        if isinstance(value, dict):
            yield from _flattened(value, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", value


def _option_assignments(value: Any) -> Iterator[tuple[str, Any]]:
    """The options in a ``leanOptions`` value: a table or an array, as Lake accepts."""
    if isinstance(value, dict):
        yield from _flattened(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                item = [item.get("name"), item.get("value")]
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
                yield item[0], item[1]


# A lakefile's options: the package's, each Lean target's (a library or an executable,
# which Lake builds with its own), and whether all could be read.
_LakefileOptions = tuple[list[tuple[str, Any]], list[list[tuple[str, Any]]], bool]


def _sets_options_by_argument(arguments: object) -> bool:
    """Whether Lean arguments may set an option: `-D`, alone or among clustered short options."""
    return isinstance(arguments, list) and any(
        isinstance(argument, str) and re.match(r"-[^-=]*D", argument) is not None
        for argument in arguments
    )


def _toml_options(text: str) -> _LakefileOptions:
    try:
        lakefile = _toml.loads(text)
    except _toml.TOMLDecodeError:
        return [], [], True
    libraries = [
        target
        for kind in ("lean_lib", "lean_exe")
        if isinstance(listed := lakefile.get(kind), list)
        for target in listed
        if isinstance(target, dict)
    ]
    readable = not any(
        _sets_options_by_argument(table.get(key))
        for table in (lakefile, *libraries)
        for key in _LEAN_ARGUMENTS
    )
    return (
        list(_option_assignments(lakefile.get("leanOptions"))),
        [list(_option_assignments(library.get("leanOptions"))) for library in libraries],
        readable,
    )


def _lean_skeleton(text: str) -> tuple[str, list[str]] | None:
    """Lean source with comments as blanks, each string literal replaced by `"<index>"`.

    The strings come back in order. None for source this reader cannot follow
    confidently: an unterminated comment or string, a raw string or an escape.
    """
    kept: list[str] = []
    strings: list[str] = []
    index, depth = 0, 0
    while index < len(text):
        if depth:
            if text.startswith("/-", index):
                depth, index = depth + 1, index + 2
            elif text.startswith("-/", index):
                depth, index = depth - 1, index + 2
            else:
                kept.append("\n" if text[index] == "\n" else "")
                index += 1
        elif text.startswith("'\"'", index):
            kept.append("'\"'")
            index += 3
        elif text[index] == '"':
            end = text.find('"', index + 1)
            if end < 0 or "\\" in text[index + 1 : end] or text[index - 1 : index] in {"r", "#"}:
                return None
            strings.append(text[index + 1 : end])
            kept.append(f'"{len(strings) - 1}"')
            index = end + 1
        elif text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            kept.append(" ")
        elif text.startswith("/-", index):
            # A comment still separates tokens, as it does for Lean.
            depth, index = 1, index + 2
            kept.append(" ")
        else:
            kept.append(text[index])
            index += 1
    return None if depth else ("".join(kept), strings)


def _bracket_depth(text: str) -> int:
    return sum(text.count(c) for c in "([{⟨") - sum(text.count(c) for c in ")]}⟩")


def _top_level_parts(expression: str, separator: str) -> list[str]:
    """``expression`` split at each ``separator`` outside brackets."""
    parts, depth, start, index = [], 0, 0, 0
    while index < len(expression):
        if expression[index] in "([{⟨":
            depth += 1
        elif expression[index] in ")]}⟩":
            depth -= 1
        elif depth == 0 and expression.startswith(separator, index):
            parts.append(expression[start:index])
            start = index = index + len(separator)
            continue
        index += 1
    return [*parts, expression[start:]]


def _lean_option_list(
    expression: str,
    definitions: Mapping[str, str],
    strings: Sequence[str],
    seen: frozenset[str] = frozenset(),
) -> list[tuple[str, Any]] | None:
    """The options an expression lists, or None if any part of it cannot be read."""
    found: list[tuple[str, Any]] = []
    for part in (part.strip() for part in _top_level_parts(expression, "++")):
        if part in definitions and part not in seen:
            listed = _lean_option_list(definitions[part], definitions, strings, seen | {part})
            if listed is None:
                return None
            found += listed
        elif part.startswith("#[") and part.endswith("]"):
            for item in (item.strip() for item in _top_level_parts(part[2:-1], ",")):
                match = _LAKEFILE_OPTION.fullmatch(item) if item else None
                if item and match is None:
                    return None
                if match is not None:
                    value = match[2]
                    found.append(
                        (
                            match[1],
                            strings[int(value[1:-1])] if value[0] == '"' else json.loads(value),
                        )
                    )
        else:
            return None
    return found


def _lean_options(text: str) -> _LakefileOptions:
    """The options a Lean lakefile's package and targets set, read without running it.

    Only a narrow form is read: top-level `package`, `lean_lib` and `lean_exe` declarations
    whose indented `leanOptions` fields are literal lists, `++` and in-file
    definitions without parameters, and whose Lean arguments are literal strings
    without `-D`. Anything else that could set an option, such as an unresolved
    or namespaced name, a computed argument or a declaration this reader does not
    find, makes the whole file unreadable: no option's value could then be certain.
    """
    skeleton = _lean_skeleton(text)
    if skeleton is None:
        return [], [], False
    source, strings = skeleton
    declarations: list[str] = []
    for line in source.splitlines(keepends=True):
        if line.strip() and not line[0].isspace():
            declarations.append(line)
        elif declarations:
            declarations[-1] += line
    named = [m for m in map(_LAKEFILE_DEFINITION.match, declarations) if m is not None]
    names = Counter(match[1] for match in named)
    scoped = re.search(r"\b(?:namespace|section|open|export)\b", source) is not None
    definitions = {} if scoped else {m[1]: m[2] for m in named if names[m[1]] == 1}
    package: list[tuple[str, Any]] = []
    libraries: list[list[tuple[str, Any]]] = []
    fields = targets = 0
    readable = True
    for declaration in declarations:
        target = _LAKEFILE_TARGET.match(declaration)
        if target is None:
            continue
        targets += 1
        options: list[tuple[str, Any]] = []
        lines = declaration.splitlines()
        for field in _LAKEFILE_FIELD.finditer(declaration):
            fields += 1
            indent = len(field[1])
            value, depth = [field[3]], _bracket_depth(field[3])
            for line in lines[declaration[: field.start()].count("\n") + 1 :]:
                if depth <= 0 and line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                value.append(line)
                depth += _bracket_depth(line)
            expression = " ".join(value).strip()
            if field[2] != "leanOptions":
                literal = _LAKEFILE_STRINGS.fullmatch(expression) is not None
                readable = (
                    readable
                    and literal
                    and not _sets_options_by_argument(
                        [strings[int(index)] for index in re.findall(r'"([0-9]+)"', expression)]
                    )
                )
                continue
            listed = _lean_option_list(expression, definitions, strings)
            readable = readable and listed is not None
            options += listed or []
        if target[1] == "package":
            package += options
        else:
            libraries.append(options)
    # Every target and every field that can set an option must be one read above:
    # an indented declaration or an inline field would otherwise go unseen.
    readable = (
        readable
        and targets == len(re.findall(r"\b(?:package|lean_lib|lean_exe)\b", source))
        and fields == len(re.findall(r"\b(?:leanOptions|moreLeanArgs|weakLeanArgs)\b", source))
    )
    return package, libraries, readable


def _supported_lean_option(name: str, value: Any) -> bool:
    bare = name.removeprefix("weak.")
    return (
        (bare in LEAN_OPTION_NAMES or bare.startswith(LEAN_OPTION_PREFIXES))
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", bare) is not None
        and (
            type(value) is bool
            or (type(value) is int and 0 <= value < 2**63)
            or (type(value) is str and len(value.encode("utf-8")) <= 256 and value.isprintable())
        )
    )


def project_lean_options(root: str | Path) -> ProjectLeanOptions:
    """The Lean options the project's lakefile sets, read without executing it.

    Lake builds each library and executable with the package's options and then
    its own, the later value of an option winning. LeanWarp checks every file with
    one set of options, so it applies an option only where every target builds with the same
    value. Any other option is reported, and if any option the lakefile sets cannot
    be read, none is applied: an unread override could change any of them.
    """
    project = Path(root)
    text = _lakefile_text(project, "lakefile.toml")
    if text is not None:
        package, libraries, complete = _toml_options(text)
    elif (text := _lakefile_text(project, "lakefile.lean")) is not None:
        package, libraries, complete = _lean_options(text)
    else:
        return ProjectLeanOptions({}, ())
    scopes = [dict(package + library) for library in libraries] or [dict(package)]
    named = set().union(*scopes)
    applied = {
        name: scopes[0][name]
        for name in named
        if complete
        and all(name in scope for scope in scopes)
        and len({json.dumps(scope[name]) for scope in scopes}) == 1
        and _supported_lean_option(name, scopes[0][name])
    }
    ignored = named - set(applied) | (set() if complete else {"leanOptions"})
    return ProjectLeanOptions(applied, tuple(sorted(ignored)))


def dependency_fingerprint(raw: bytes) -> str:
    """Versioned dependency identity of a Lake manifest, independent of the root project.

    Every Lake release's manifest is accepted, whatever its format version or
    root-level fields: only `packages` names dependencies. Each package keeps
    every field except request provenance (`inputRev`, `inherited`), so an
    unknown field still distinguishes dependencies. Git dependencies must be
    pinned to commits; path dependencies are identified by their directory.
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
        or not manifest["version"]
        or not isinstance(manifest.get("packages"), list)
    ):
        raise ProjectError("lake-manifest.json is not a Lake manifest")
    packages = []
    names: set[str] = set()
    for package in manifest["packages"]:
        if (
            not isinstance(package, dict)
            or not isinstance(package.get("name"), str)
            or not package["name"]
            or package["name"] in names
        ):
            raise ProjectError("lake-manifest.json must list uniquely named dependencies")
        if package.get("type") == "git":
            if (
                not isinstance(package.get("url"), str)
                or not package["url"]
                or not isinstance(package.get("rev"), str)
                or re.fullmatch(r"[0-9a-f]{40}", package["rev"]) is None
            ):
                raise ProjectError("lake-manifest.json must pin Git dependencies to commits")
        elif package.get("type") == "path":
            if not isinstance(package.get("dir"), str) or not package["dir"]:
                raise ProjectError("lake-manifest.json must give path dependencies a directory")
        else:
            raise ProjectError("lake-manifest.json lists a dependency that is neither Git nor path")
        names.add(package["name"])
        packages.append({k: v for k, v in package.items() if k not in _PROVENANCE_FIELDS})
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


def newest_toolchain(toolchains: Sequence[str]) -> str | None:
    """The newest stable Lean release among qualified toolchains, for plain files."""
    releases: list[tuple[tuple[int, int, int], str]] = []
    for toolchain in toolchains:
        match = re.fullmatch(
            r"leanprover/lean4:v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", toolchain
        )
        if match is not None:
            major, minor, patch = match.groups()
            releases.append(((int(major), int(minor), int(patch)), toolchain))
    return max(releases)[1] if releases else None


#: Ignore files read from the project's top level, in order; later patterns win.
IGNORE_FILES = (".gitignore", ".leanwarpignore")
MAX_UPLOAD_FILES = 256
MAX_UPLOAD_BYTES = 1024 * 1024
_IGNORE_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class UploadPlan:
    """The files an operation would upload, and why any cannot be."""

    sizes: dict[str, int]
    total_bytes: int
    ignored: int
    ignore_files: tuple[str, ...]
    problems: tuple[str, ...]
    max_total_bytes: int

    def public(self) -> dict[str, Any]:
        return {
            "files": [{"path": path, "bytes": size} for path, size in self.sizes.items()],
            "file_count": len(self.sizes),
            "total_bytes": self.total_bytes,
            "limits": {"files": MAX_UPLOAD_FILES, "bytes": self.max_total_bytes},
            "ignored_lean_files": self.ignored,
            "ignore_files": list(self.ignore_files),
            "problems": list(self.problems),
        }


def collect_lean_sources(
    root: str | Path,
    *,
    max_file_bytes: int = MAX_UPLOAD_BYTES,
    max_total_bytes: int = MAX_UPLOAD_BYTES,
) -> dict[str, str]:
    """Read regular UTF-8 .lean files below root, excluding links, hidden and ignored paths.

    The selected environment supplies the toolchain and dependencies.
    Symlinks are errors rather than silently dereferenced outside the project.
    """
    plan, sources = _scan_upload(
        root, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes, collect=True
    )
    if plan.problems:
        raise ProjectError(plan.problems[0])
    return sources


def plan_upload(
    root: str | Path,
    *,
    max_file_bytes: int = MAX_UPLOAD_BYTES,
    max_total_bytes: int = MAX_UPLOAD_BYTES,
) -> UploadPlan:
    """Preview every selected file and problem, without retaining file contents.

    Paths matching the project's top-level ``.gitignore`` or ``.leanwarpignore``
    are skipped, as git would skip them.
    """
    plan, _ = _scan_upload(
        root, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes, collect=False
    )
    return plan


def _scan_upload(
    root: str | Path, *, max_file_bytes: int, max_total_bytes: int, collect: bool
) -> tuple[UploadPlan, dict[str, str]]:
    """One validation path; only a bounded upload retains source bodies."""
    project = Path(root).resolve(strict=True)
    if not project.is_dir():
        raise ProjectError(f"{project} is not a directory")
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise UsageError("source size limits must be positive")
    ignore_files, ignored_by = _ignore_rules(project)
    sources: dict[str, str] = {}
    sizes: dict[str, int] = {}
    import_problems: list[str] = []
    problems: list[str] = []
    total = ignored = 0
    for directory, directories, names, directory_fd in os.fwalk(project, follow_symlinks=False):
        relative_directory = Path(directory).relative_to(project).as_posix()
        prefix = "" if relative_directory == "." else f"{relative_directory}/"
        directories[:] = sorted(
            name
            for name in directories
            if name not in _EXCLUDED
            and not name.startswith(".")
            and not ignored_by(f"{prefix}{name}/")
        )
        problems.extend(
            f"{prefix}{name} is a symlinked directory; LeanWarp uploads only regular files "
            "inside the project. Exclude it in .leanwarpignore if it is not needed"
            for name in directories
            if (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            if not name.endswith(".lean") or name.startswith(".") or name == "lakefile.lean":
                continue
            relative = f"{prefix}{name}"
            if ignored_by(relative):
                ignored += 1
                continue
            problem = _module_path_problem(relative)
            source = ""
            if problem is None:
                source, problem = _read_source(relative, name, directory_fd, max_file_bytes)
            if problem is not None:
                problems.append(problem)
                if collect:
                    break
                continue
            size = len(source.encode("utf-8"))
            sizes[relative] = size
            total += size + len(relative.encode("utf-8"))
            import_problems.extend(_ignored_imports(project, relative, source, ignored_by))
            if collect:
                if len(sizes) > MAX_UPLOAD_FILES or total > max_total_bytes:
                    break
                sources[relative] = source
        if collect and (problems or len(sizes) > MAX_UPLOAD_FILES or total > max_total_bytes):
            break
    if not (collect and problems) and (len(sizes) > MAX_UPLOAD_FILES or total > max_total_bytes):
        problems.append(
            f"the project's Lean files ({len(sizes)} files, {total} bytes) exceed the upload "
            f"limit of {MAX_UPLOAD_FILES} files and {_size(max_total_bytes)}. Run `leanwarp "
            "files` to see them, then exclude unrelated ones in .leanwarpignore"
        )
    if not (collect and problems):
        problems.extend(import_problems)
    return UploadPlan(
        sizes=sizes,
        total_bytes=total,
        ignored=ignored,
        ignore_files=ignore_files,
        problems=tuple(problems),
        max_total_bytes=max_total_bytes,
    ), sources


def _module_path_problem(relative: str) -> str | None:
    module_root = relative.split("/", 1)[0].removesuffix(".lean")
    if (
        len(relative) <= 240
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*\.lean", relative)
        is not None
        and module_root not in {"Init", "Lean", "Lake", "Std", "Mathlib"}
    ):
        return None
    return (
        f"{relative} is not an uploadable Lean module path: use ASCII letters, digits and "
        "underscores, at most 240 characters, outside Init, Lean, Lake, Std and Mathlib. "
        "Exclude it in .leanwarpignore if it is not part of the project"
    )


def _read_source(
    relative: str, name: str, directory_fd: int, max_file_bytes: int
) -> tuple[str, str | None]:
    """Read one bounded file, or say why it cannot be uploaded."""
    # O_NOFOLLOW rejects a file replaced by a symlink during discovery.
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return "", f"{relative} cannot be read; symlinked Lean files are not uploaded"
        return "", f"{relative} cannot be read: {error.strerror or 'unknown error'}"
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            return "", f"{relative} must be a regular file"
        raw = source.read(max_file_bytes + 1)
    if len(raw) > max_file_bytes:
        return "", f"{relative} is larger than the {_size(max_file_bytes)} upload limit"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", f"{relative} is not UTF-8 text"


def _size(limit: int) -> str:
    for unit, scale in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if limit % scale == 0:
            return f"{limit // scale} {unit}"
    return f"{limit} bytes"


# Lean headers contain whitespace-separated imports, including nested comments.
# Match tokens without consuming the declaration body or text inside strings.
_HEADER_COMPONENT = re.compile(r"[^\W\d]\w*|«[^»]+»")
_HEADER_IDENTIFIER = re.compile(
    rf"(?:{_HEADER_COMPONENT.pattern})(?:\.(?:{_HEADER_COMPONENT.pattern}))*"
)
_HEADER_TOKEN = re.compile(rf"{_HEADER_IDENTIFIER.pattern}|\S")
_HEADER_WORDS = {"module", "prelude", "public", "private", "meta", "import", "all"}


def _header_imports(source: str) -> list[str]:
    """Read only Lean's header, skipping line and nested block comments."""
    modules: list[str] = []
    importing = False
    position = 0
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source.startswith("--", position):
            end = source.find("\n", position + 2)
            position = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/-", position):
            depth = 1
            position += 2
            while position < len(source) and depth:
                if source.startswith("/-", position):
                    depth += 1
                    position += 2
                elif source.startswith("-/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
            continue
        token = _HEADER_TOKEN.match(source, position)
        if token is None:
            break
        word = token.group()
        position = token.end()
        if word in _HEADER_WORDS:
            importing = word in {"import", "all"}
        elif importing and _HEADER_IDENTIFIER.fullmatch(word):
            # Each Lean import consumes one identifier, including quoted
            # components such as Foo.«Bar». Only ASCII project module names
            # correspond to uploadable paths; consume other imports as well.
            parts = [
                part[1:-1] if part.startswith("«") else part
                for part in _HEADER_COMPONENT.findall(word)
            ]
            if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
                modules.append(".".join(parts))
            importing = False
        else:
            break
    return modules


def _ignored_imports(
    project: Path, relative: str, source: str, ignored_by: Callable[[str], bool]
) -> list[str]:
    """Report omitted project imports while one bounded file is in memory."""
    problems: list[str] = []
    for module in dict.fromkeys(_header_imports(source)):
        path = module.replace(".", "/") + ".lean"
        parents = Path(path).parents
        omitted = ignored_by(path) or any(
            ignored_by(f"{parent.as_posix()}/") for parent in parents if parent != Path(".")
        )
        if omitted and (project / path).is_file():
            problems.append(
                f"{relative} imports {module}, but an ignore rule in .gitignore or "
                f".leanwarpignore leaves out {path}. Change the rule so it is uploaded; "
                "`leanwarp files` shows the result"
            )
    return problems


def _ignore_rules(project: Path) -> tuple[tuple[str, ...], Callable[[str], bool]]:
    """The top-level ignore files present, and a matcher for project-relative paths."""
    texts: dict[str, bytes] = {}
    for name in IGNORE_FILES:
        path = project / name
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            texts[name] = stream.read(_IGNORE_FILE_BYTES + 1)
    return tuple(texts), _ignore_matcher(texts)


def _ignore_matcher(texts: Mapping[str, bytes]) -> Callable[[str], bool]:
    """A matcher for paths relative to a directory, from its top-level ignore files
    in ``IGNORE_FILES`` order, each read to at most one byte past the limit."""
    lines: list[str] = []
    for name, raw in texts.items():
        if len(raw) > _IGNORE_FILE_BYTES:
            raise ProjectError(f"{name} is larger than 64 KiB")
        try:
            lines.extend(raw.decode("utf-8").splitlines())
        except UnicodeDecodeError as error:
            raise ProjectError(f"{name} is not UTF-8 text") from error
    return GitIgnoreSpec.from_lines(lines).match_file

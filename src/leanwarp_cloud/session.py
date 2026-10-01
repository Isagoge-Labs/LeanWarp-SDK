"""A project journal shared by Python, CLI and MCP. No credentials are stored here."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .client import LeanWarpCloud, LeanWarpCloudError
from .project import collect_lean_sources, matching_bundles, project_environment

_MAX_JOURNAL_BYTES = 8 * 1024 * 1024


class SessionError(RuntimeError):
    """Local recovery or project reconciliation is required before another write."""


class ProjectSession:
    """One local project and one durable hosted workspace.

    Mutations journal their exact payload before sending. An ambiguous request is
    replayed only through recover(), never replaced by a new request. A process
    lock serializes CLI/MCP clients on this project; server revisions protect
    separate devices. Closing the HTTP client does not stop paid compute.
    """

    def __init__(self, cloud: LeanWarpCloud, root: str | Path = ".") -> None:
        self.cloud = cloud
        self.root = Path(root).resolve(strict=True)
        self.directory = self.root / ".leanwarp"
        self.path = self.directory / "session.json"

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        if os.name != "posix":
            raise SessionError("project sessions require macOS, Linux or WSL")
        import fcntl

        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise SessionError(".leanwarp must be a real project directory")
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SessionError("another LeanWarp command is using this project") from error
            state = self._read()
            owner = self.cloud.account().get("owner_id")
            if not isinstance(owner, str) or not owner:
                raise SessionError("API returned an invalid account identity")
            if state.get("owner_id", owner) != owner:
                raise SessionError("project belongs to another account; restore its credentials")
            state["owner_id"] = owner
            yield state
        finally:
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"schema": 1, "base_url": self.cloud.base_url, "files": {}, "pending": None}
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SessionError("project journal must be a regular file")
            raw = stream.read(_MAX_JOURNAL_BYTES + 1)
        try:
            state = json.loads(raw) if len(raw) <= _MAX_JOURNAL_BYTES else None
        except ValueError as error:
            raise SessionError("invalid project journal; preserve it for recovery") from error
        if (
            not isinstance(state, dict)
            or state.get("schema") != 1
            or state.get("base_url") != self.cloud.base_url
            or not isinstance(state.get("files"), dict)
            or not set(state).issubset(
                {
                    "schema",
                    "base_url",
                    "owner_id",
                    "files",
                    "pending",
                    "workspace_id",
                    "revision",
                    "environment",
                    "operation_id",
                    "operation_revision",
                }
            )
        ):
            raise SessionError("project journal is invalid or belongs to another API origin")
        _validate_journal(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        # Never persist an intent that recovery cannot read. Validation happens
        # before replacing the last good journal or sending the request.
        _validate_journal(state)
        raw = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise SessionError("request exceeds the project journal recovery limit")
        fd, name = tempfile.mkstemp(prefix="session-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _workspace(state: Mapping[str, Any]) -> str:
        workspace = state.get("workspace_id")
        if not isinstance(workspace, str) or not workspace:
            raise SessionError("connect the project first")
        return workspace

    @staticmethod
    def _ready(state: Mapping[str, Any]) -> None:
        if state.get("pending") is not None:
            raise SessionError("an uncertain request exists; run recover before another mutation")

    def _environment(self, state: dict[str, Any]) -> None:
        environment = list(project_environment(self.root))
        if environment == state.get("environment"):
            return
        if list(project_environment(self.root, legacy=True)) != state.get("environment"):
            raise SessionError("project dependencies changed; use a new workspace explicitly")
        # Upgrade a byte-bound journal only while the original bytes still match.
        state["environment"] = environment
        self._save(state)

    @staticmethod
    def _checked_operation(state: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        # Workspace sync can advance revision after submission. Compare against
        # the saved submission, not the workspace's current revision. Older
        # journals have no operation_revision; keep their existing ID binding.
        revision = state.get("operation_revision")
        if result.get("operation_id") != state.get("operation_id") or (
            revision is not None
            and (type(result.get("revision")) is not int or result["revision"] != revision)
        ):
            raise SessionError("operation receipt does not match the saved submission")
        return result

    def connect(
        self,
        *,
        resource_profile: str = "standard",
        max_resource_profile: str = "standard",
        bundle_id: str | None = None,
    ) -> dict[str, Any]:
        with self._locked() as state:
            self._ready(state)
            if state.get("workspace_id"):
                self._environment(state)
                return self.cloud.workspace(self._workspace(state))
            environment = project_environment(self.root)
            matches = [
                b
                for b in matching_bundles(self.root, self.cloud.versions()["versions"])
                if bundle_id is None or b["bundle_id"] == bundle_id
            ]
            if not matches:
                raise SessionError(
                    "unsupported toolchain/lockfile; do not change dependencies to pass"
                )
            state["environment"] = list(environment)
            return self._request(
                state,
                "create_workspace",
                {
                    "bundle_id": matches[-1]["bundle_id"],
                    "resource_profile": resource_profile,
                    "max_resource_profile": max_resource_profile,
                    "max_spend_microusd": None,
                },
            )

    def sync(self) -> dict[str, Any]:
        with self._locked() as state:
            self._ready(state)
            return self._sync(state)

    def _sync(self, state: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(state)
        self._environment(state)
        sources = collect_lean_sources(self.root)
        hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()}
        files = {
            name: text for name, text in sources.items() if state["files"].get(name) != hashes[name]
        }
        deleted = sorted(set(state["files"]) - set(sources))
        if not files and not deleted:
            return {"workspace_id": workspace, "revision": state["revision"]}
        return self._request(
            state,
            "sync_files",
            {
                "workspace_id": workspace,
                "expected_revision": state["revision"],
                "files": files,
                "delete_paths": deleted,
            },
            hashes=hashes,
        )

    def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        resource_profile: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Sync acknowledged source, then submit without hiding a second paid operation."""
        if timeout_seconds is not None and (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600
        ):
            raise ValueError("timeout_seconds must be an integer between 1 and 600")
        json.dumps(dict(payload), allow_nan=False)
        with self._locked() as state:
            self._ready(state)
            if state.get("operation_id"):
                previous = self._checked_operation(
                    state, self.cloud.operation(state["operation_id"])
                )
                if previous.get("state") not in {"completed", "failed", "cancelled"}:
                    raise SessionError("previous operation is active; wait or cancel it first")
            self._sync(state)
            body: dict[str, Any] = {
                "workspace_id": self._workspace(state),
                "kind": kind,
                "expected_revision": state["revision"],
                "payload": dict(payload),
            }
            if resource_profile is not None:
                body["resource_profile"] = resource_profile
            if timeout_seconds is not None:
                body["timeout_seconds"] = timeout_seconds
            return self._request(state, "submit", body)

    def _request(
        self,
        state: dict[str, Any],
        method: str,
        arguments: dict[str, Any],
        *,
        hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        pending = {
            "method": method,
            # Own the JSON snapshot before saving or sending it. Caller-owned
            # lists and dictionaries must not diverge from the durable intent.
            "arguments": json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False)),
            "idempotency_key": uuid4().hex,
            "hashes": hashes,
        }
        state["pending"] = pending
        self._save(state)
        return self._replay(state)

    def _replay(self, state: dict[str, Any]) -> dict[str, Any]:
        pending = state["pending"]
        if (
            not isinstance(pending, dict)
            or pending.get("method") not in {"create_workspace", "sync_files", "submit"}
            or not isinstance(pending.get("arguments"), dict)
            or not isinstance(pending.get("idempotency_key"), str)
        ):
            raise SessionError("invalid pending request; preserve journal for recovery")
        method = pending["method"]
        try:
            if method == "create_workspace":
                # Older journals can contain an explicit cap. Replay that exact
                # payload: stripping it changes the durable request's identity.
                result = self.cloud._request(
                    "POST",
                    "workspaces",
                    body=pending["arguments"],
                    idempotency_key=pending["idempotency_key"],
                )
            else:
                result = getattr(self.cloud, method)(
                    **pending["arguments"], idempotency_key=pending["idempotency_key"]
                )
        except LeanWarpCloudError as error:
            # Definite rejection has no accepted effect. Auth/transient/ambiguous
            # errors keep the intent recoverable, including a rotated key later.
            if error.status_code in {400, 404, 409, 413, 422}:
                state["pending"] = None
                self._save(state)
            raise
        # A successful but malformed response is still an uncertain request.
        # Preserve the exact journal and idempotency key for recovery.
        identifier = "operation_id" if method == "submit" else "workspace_id"
        if (
            not isinstance(result.get(identifier), str)
            or not 0 < len(result[identifier]) <= 256
            or type(result.get("revision")) is not int
            or not 0 <= result["revision"] <= 2**31 - 1
            or (
                method != "create_workspace" and result.get("workspace_id") != state["workspace_id"]
            )
            or (
                method == "submit"
                and (
                    result["revision"] != pending["arguments"]["expected_revision"]
                    or result.get("kind") != pending["arguments"]["kind"]
                )
            )
        ):
            raise SessionError("invalid API receipt; preserve journal and run recover")
        if method == "create_workspace":
            state["workspace_id"] = result["workspace_id"]
            state["revision"] = result["revision"]
        elif method == "sync_files":
            state["revision"] = result["revision"]
            state["files"] = pending["hashes"]
        else:
            state["operation_id"] = result["operation_id"]
            state["operation_revision"] = result["revision"]
        state["pending"] = None
        self._save(state)
        return result

    def recover(self) -> dict[str, Any]:
        """Replay the exact saved request even if local files have since changed."""
        with self._locked() as state:
            if state.get("pending") is None:
                return self._status(state)
            return self._replay(state)

    def _status(self, state: dict[str, Any]) -> dict[str, Any]:
        result = {
            "workspace": self.cloud.workspace(self._workspace(state)),
            "local_revision": state.get("revision"),
            "recovery_required": state.get("pending") is not None,
        }
        if state.get("operation_id"):
            result["operation"] = self._checked_operation(
                state, self.cloud.operation(state["operation_id"])
            )
        return result

    def status(self) -> dict[str, Any]:
        with self._locked() as state:
            return self._status(state)

    def wait(self, *, timeout: float = 300) -> dict[str, Any]:
        with self._locked() as state:
            self._ready(state)
            operation = state.get("operation_id")
            if not isinstance(operation, str):
                raise SessionError("no submitted operation")
        # Do not hold the project lock while polling; another process may cancel.
        return self._checked_operation(state, self.cloud.wait(operation, timeout=timeout))

    def cancel(self) -> dict[str, Any]:
        with self._locked() as state:
            self._ready(state)
            if not isinstance(state.get("operation_id"), str):
                raise SessionError("no submitted operation")
            return self._checked_operation(state, self.cloud.cancel(state["operation_id"]))

    def disconnect(self) -> dict[str, Any]:
        """Forget a stopped connection so this project can explicitly choose a new bundle."""
        with self._locked() as state:
            self._ready(state)
            workspace_id = self._workspace(state)
            if state.get("operation_id"):
                operation = self._checked_operation(
                    state, self.cloud.operation(state["operation_id"])
                )
                if operation.get("state") not in {"completed", "failed", "cancelled"}:
                    raise SessionError("previous operation is active; wait or cancel it first")
            workspace = self.cloud.workspace(workspace_id)
            if workspace.get("state") != "stopped":
                raise SessionError("stop the workspace before disconnecting this project")
            # Runtime state alone does not exclude queued or temporary work.
            # The server's guarded stop also checks work submitted elsewhere.
            stopped = self.cloud.stop(workspace_id)
            if stopped.get("workspace_id") != workspace_id or stopped.get("state") != "stopped":
                raise SessionError("stop was not confirmed; retain the project connection")
            for key in ("workspace_id", "revision", "operation_id", "operation_revision"):
                state.pop(key, None)
            state["files"] = {}
            self._save(state)
            return {"status": "disconnected", "workspace_id": workspace_id}

    def stop(self) -> dict[str, Any]:
        with self._locked() as state:
            self._ready(state)
            return self.cloud.stop(self._workspace(state))


def _validate_journal(state: dict[str, Any]) -> None:
    def text(value: object) -> bool:
        return isinstance(value, str) and 0 < len(value) <= 256

    def revision(value: object) -> bool:
        return type(value) is int and 0 <= value <= 2**31 - 1

    def hashes(value: object) -> bool:
        return isinstance(value, dict) and all(
            text(k)
            and isinstance(v, str)
            and len(v) == 64
            and all(c in "0123456789abcdef" for c in v)
            for k, v in value.items()
        )

    valid = text(state.get("owner_id")) and hashes(state.get("files"))
    environment = state.get("environment")
    valid = (
        valid
        and isinstance(environment, list)
        and len(environment) == 2
        and all(map(text, environment))
    )
    if "workspace_id" in state:
        valid = valid and text(state["workspace_id"]) and revision(state.get("revision"))
    if "operation_id" in state:
        valid = valid and text(state["operation_id"]) and "workspace_id" in state
    if "operation_revision" in state:
        valid = valid and "operation_id" in state and revision(state["operation_revision"])
    pending = state.get("pending")
    if pending is not None:
        valid = valid and isinstance(pending, dict)
        if valid:
            valid = set(pending) == {"method", "arguments", "idempotency_key", "hashes"}
        if valid:
            method, args, key = pending["method"], pending["arguments"], pending["idempotency_key"]
            valid = (
                isinstance(method, str)
                and method in {"create_workspace", "sync_files", "submit"}
                and isinstance(args, dict)
                and isinstance(key, str)
                and len(key) == 32
                and all(c in "0123456789abcdef" for c in key)
            )
        if valid and method == "create_workspace":
            valid = (
                set(args) - {"max_spend_microusd"}
                == {"bundle_id", "resource_profile", "max_resource_profile"}
                and all(
                    text(args[k]) for k in ("bundle_id", "resource_profile", "max_resource_profile")
                )
                and (
                    args.get("max_spend_microusd") is None
                    or (
                        type(args["max_spend_microusd"]) is int
                        and 0 <= args["max_spend_microusd"] <= 10**12
                    )
                )
                and "workspace_id" not in state
                and pending["hashes"] is None
            )
        elif valid:
            valid = (
                args.get("workspace_id") == state.get("workspace_id")
                and text(args.get("workspace_id"))
                and revision(args.get("expected_revision"))
            )
            if method == "sync_files":
                valid = (
                    valid
                    and set(args) == {"workspace_id", "expected_revision", "files", "delete_paths"}
                    and isinstance(args["files"], dict)
                    and all(text(k) and isinstance(v, str) for k, v in args["files"].items())
                    and isinstance(args["delete_paths"], list)
                    and all(map(text, args["delete_paths"]))
                    and hashes(pending["hashes"])
                )
            else:
                valid = (
                    valid
                    and set(args).issubset(
                        {
                            "workspace_id",
                            "kind",
                            "expected_revision",
                            "payload",
                            "resource_profile",
                            "timeout_seconds",
                        }
                    )
                    and isinstance(args.get("kind"), str)
                    and args.get("kind") in {"check", "inspect", "try_tactics", "verify_target"}
                    and isinstance(args.get("payload"), dict)
                    and pending["hashes"] is None
                    and (
                        "timeout_seconds" not in args
                        or (
                            type(args["timeout_seconds"]) is int
                            and 1 <= args["timeout_seconds"] <= 600
                        )
                    )
                )
    if not valid:
        raise SessionError("invalid project journal; preserve it for recovery")

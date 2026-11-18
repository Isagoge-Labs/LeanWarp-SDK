"""Synchronous agent client with bounded retries and durable operation handles."""

from __future__ import annotations

import email.utils
import hashlib
import hmac
import itertools
import json
import logging
import random
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import httpx

from ._version import __version__
from .amounts import validate_spending_cap
from .endpoints import api_origin, public_origin
from .errors import LeanWarpCloudError, LeanWarpTransportError, OperationTimeout, UsageError
from .project import choose_environment, project_identity, validate_selection
from .transport import DeadlineTransport
from .updates import record as record_releases

_LOG = logging.getLogger("leanwarp")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Which SDK surface sends requests; the service reads it from the User-Agent.
Interface = Literal["python", "cli", "mcp"]
_INTERFACES = frozenset(get_args(Interface))
#: The longest the API holds a read of an operation that has not finished.
MAX_SERVER_WAIT_SECONDS = 20.0
#: The most operations one history page holds.
MAX_OPERATION_PAGE = 25
#: The most operations one ``wait_operations`` names.
MAX_WAITED_OPERATIONS = 100
#: Every operation the service runs; mirrors the service's runtime contract.
OperationKind = Literal[
    "check",
    "inspect",
    "try_tactics",
    "verify_target",
    "suggest",
    "holes",
    "extract_decls",
    "sorry2lemma",
    "check_files",
    "verify_batch",
]
OPERATION_KINDS: frozenset[str] = frozenset(get_args(OperationKind))
#: How long a finished operation is kept before history may remove it to admit new
#: work: about an hour, or about a minute with ``"short"``, which only check,
#: inspect and try_tactics accept.
Retention = Literal["standard", "short"]
RETENTIONS: frozenset[str] = frozenset(get_args(Retention))
#: The most files one check_files operation checks.
MAX_BATCH_FILES = 64
#: The most proofs one verify_batch operation checks.
MAX_BATCH_PROOFS = 64
#: What a feedback report is about, and how much it got in the way.
FeedbackKind = Literal["bug", "missing_feature", "confusing_result", "docs", "performance", "other"]
FeedbackSeverity = Literal["blocking", "worked_around", "minor"]
FEEDBACK_KINDS: frozenset[str] = frozenset(get_args(FeedbackKind))
FEEDBACK_SEVERITIES: frozenset[str] = frozenset(get_args(FeedbackSeverity))
#: The service's bounds on a feedback summary and its details, in characters.
MIN_FEEDBACK_SUMMARY = 8
MAX_FEEDBACK_SUMMARY = 200
MAX_FEEDBACK_DETAILS = 4000
# Time allowed for a held response to arrive after the server stops waiting.
_RESPONSE_MARGIN = 2.0
# How long a transient failure keeps being retried when the caller set no deadline.
# An MCP tool call must finish inside the client's own limit, about a minute.
_RETRY_WINDOW_SECONDS = 60.0
_MCP_RETRY_WINDOW_SECONDS = 15.0
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_CAP_SECONDS = 5.0


class LeanWarpCloud:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        retries: int | None = None,
        transport: httpx.BaseTransport | None = None,
        interface: Interface = "python",
    ) -> None:
        """Without a key, only ``versions`` works: the production catalog is public,
        so a project can be checked before signing up.

        Reads, ``stop``, ``cancel``, ``delete`` and writes with an idempotency key are
        resent after 429, 502, 503, 504 or a transport failure until the caller's
        deadline, or a minute without one. ``retries`` further limits how many times;
        0 sends each request once.
        """
        if base_url is None:
            base_url = public_origin() if api_key is None else api_origin(api_key)
        parsed = urlparse(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise UsageError("base_url must use HTTPS (HTTP is supported only on localhost)")
        if (
            not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise UsageError(
                "base_url must be an origin without a path, credentials, query or fragment"
            )
        if api_key is not None and (
            not api_key or api_key.strip() != api_key or "\n" in api_key or "\r" in api_key
        ):
            raise UsageError("api_key must be a nonempty token")
        if timeout <= 0 or (retries is not None and not 0 <= retries <= 5):
            raise UsageError("timeout must be positive and retries must be between zero and five")
        if interface not in _INTERFACES:
            raise UsageError("interface must be python, cli or mcp")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={
                **({"Authorization": f"Bearer {api_key}"} if api_key is not None else {}),
                "Accept-Encoding": "identity",
                "User-Agent": f"leanwarp-sdk/{__version__} ({interface})",
            },
            timeout=timeout,
            transport=transport if transport is not None else DeadlineTransport(base_url),
            follow_redirects=False,
            trust_env=False,
        )
        self._retries = retries
        self._timeout = timeout
        self._interface = interface
        self._retry_window = (
            _MCP_RETRY_WINDOW_SECONDS if interface == "mcp" else _RETRY_WINDOW_SECONDS
        )
        self.base_url = base_url.rstrip("/")
        self._owner: str | None = None
        # Proves an account binding was made with this key at this service, so a
        # project can trust its saved account without asking again.
        self._binding_secret = (
            None
            if api_key is None
            else hashlib.sha256(f"leanwarp-account\0{self.base_url}\0{api_key}".encode()).digest()
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LeanWarpCloud:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def account(self) -> dict[str, Any]:
        """Authenticated owner and exact posted, reserved and available microdollars."""
        account = self._request("GET", "account")
        owner = account.get("owner_id") if isinstance(account, dict) else None
        if isinstance(owner, str) and owner:
            self._owner = owner
        return account

    def account_binding(self, owner_id: str) -> str | None:
        """An HMAC binding ``owner_id`` to this key, or None without a key.

        Only a holder of the key can produce it, so a saved binding cannot be forged.
        """
        if self._binding_secret is None:
            return None
        return hmac.new(self._binding_secret, owner_id.encode(), hashlib.sha256).hexdigest()

    def owner_id(self) -> str | None:
        """The key's account, read once: a client's key, and so its owner, never changes."""
        if self._owner is None:
            self.account()
        return self._owner

    def versions(self) -> dict[str, Any]:
        """Served environments under `versions`, one current build per environment."""
        return self._request("GET", "versions")

    def resources(self) -> dict[str, Any]:
        """Raw release billing catalog for wire integrations; starts no compute."""
        return self._request("GET", "resources")

    def create_workspace_for_project(
        self,
        root: str | Path,
        *,
        environment: str | None = None,
        bundle_id: str | None = None,
        resource_profile: str | None = None,
        max_resource_profile: str | None = None,
        max_spend_microusd: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace in the environment `ProjectSession.connect` would choose."""
        validate_selection(environment, bundle_id)
        validate_spending_cap(max_spend_microusd)
        catalog = self.versions()["versions"]
        choice = choose_environment(
            project_identity(root), catalog, environment, bundle_id=bundle_id
        )
        return self.create_workspace(
            choice.build["bundle_id"],
            resource_profile=resource_profile,
            max_resource_profile=max_resource_profile,
            max_spend_microusd=max_spend_microusd,
            idempotency_key=idempotency_key,
        )

    def create_workspace(
        self,
        bundle_id: str,
        *,
        resource_profile: str | None = None,
        max_resource_profile: str | None = None,
        max_spend_microusd: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace without compute, optionally capped in exact microdollars.

        The cap covers total spending plus reserved credit; None means no cap.
        """
        # Omitted profiles use the service's reviewed default. Explicit names
        # still select that profile, including clients that request standard.
        # Explicit null prevents an older API's implicit zero-dollar cap from
        # creating a workspace that can never execute.
        validate_spending_cap(max_spend_microusd)
        body: dict[str, Any] = {
            "bundle_id": bundle_id,
            "max_spend_microusd": max_spend_microusd,
        }
        if resource_profile is not None:
            body["resource_profile"] = resource_profile
        if max_resource_profile is not None:
            body["max_resource_profile"] = max_resource_profile
        return self._request(
            "POST",
            "workspaces",
            body=body,
            idempotency_key=idempotency_key or uuid4().hex,
        )

    def workspaces(self) -> dict[str, Any]:
        return self._request("GET", "workspaces")

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"workspaces/{_segment(workspace_id)}")

    def workspace_files(self, workspace_id: str) -> dict[str, Any]:
        """The workspace's committed revision and each file's SHA-256, without contents."""
        return self._request("GET", f"workspaces/{_segment(workspace_id)}/files")

    def operations(
        self, workspace_id: str, *, limit: int = MAX_OPERATION_PAGE, cursor: str | None = None
    ) -> dict[str, Any]:
        """One page of a workspace's operations with their results, newest first.

        Pass the page's ``next_cursor`` to read the next, older page; it is None
        after the last one. A page may hold fewer than ``limit`` operations.
        """
        if isinstance(limit, bool) or not 1 <= limit <= MAX_OPERATION_PAGE:
            raise UsageError(f"limit must be between 1 and {MAX_OPERATION_PAGE}")
        query = {"limit": limit} | ({"cursor": cursor} if cursor is not None else {})
        path = f"workspaces/{_segment(workspace_id)}/operations?{urlencode(query)}"
        return self._request("GET", path)

    def workspace_usage(self, workspace_id: str) -> dict[str, Any]:
        """Each history cap with its current use; finished work makes room after an hour."""
        return self._request("GET", f"workspaces/{_segment(workspace_id)}/usage")

    def sync_files(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        files: Mapping[str, str],
        delete_paths: tuple[str, ...] = (),
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"workspaces/{_segment(workspace_id)}/files",
            body={
                "expected_revision": expected_revision,
                "files": dict(files),
                "delete_paths": list(delete_paths),
            },
            idempotency_key=idempotency_key or uuid4().hex,
        )

    def submit(
        self,
        workspace_id: str,
        kind: str,
        *,
        expected_revision: int,
        payload: Mapping[str, Any],
        resource_profile: str | None = None,
        timeout_seconds: int | None = None,
        retention: Retention | None = None,
        idempotency_key: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": kind,
            "expected_revision": expected_revision,
            "payload": dict(payload),
        }
        if resource_profile is not None:
            body["resource_profile"] = resource_profile
        if timeout_seconds is not None:
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
                raise UsageError("timeout_seconds must be an integer between 1 and 600")
            body["timeout_seconds"] = timeout_seconds
        _set_retention(body, retention)
        return self._request(
            "POST",
            f"workspaces/{_segment(workspace_id)}/operations",
            body=body,
            idempotency_key=idempotency_key or uuid4().hex,
            deadline=deadline,
        )

    def run(
        self,
        workspace_id: str,
        kind: str,
        *,
        expected_revision: int,
        payload: Mapping[str, Any],
        files: Mapping[str, str] | None = None,
        delete_paths: tuple[str, ...] | list[str] = (),
        resource_profile: str | None = None,
        timeout_seconds: int | None = None,
        retention: Retention | None = None,
        idempotency_key: str | None = None,
        deadline: float | None = None,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Upload changed sources and submit an operation on the revision they create.

        One request in place of ``sync_files`` and then ``submit``. The response is
        ``{"workspace_id", "revision", "operation"}``. With ``wait_seconds`` (at most
        20), the server holds it until the operation ends, as ``operation`` does.
        """
        if not 0 <= wait_seconds <= MAX_SERVER_WAIT_SECONDS:
            raise UsageError(f"wait_seconds must be between 0 and {MAX_SERVER_WAIT_SECONDS:g}")
        body: dict[str, Any] = {
            "kind": kind,
            "expected_revision": expected_revision,
            "payload": dict(payload),
            "files": dict(files or {}),
            "delete_paths": list(delete_paths),
        }
        if resource_profile is not None:
            body["resource_profile"] = resource_profile
        if timeout_seconds is not None:
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
                raise UsageError("timeout_seconds must be an integer between 1 and 600")
            body["timeout_seconds"] = timeout_seconds
        _set_retention(body, retention)
        path = f"workspaces/{_segment(workspace_id)}/run"
        if wait_seconds > 0:
            path += f"?wait_seconds={wait_seconds:.3f}"
        return self._request(
            "POST",
            path,
            body=body,
            idempotency_key=idempotency_key or uuid4().hex,
            deadline=deadline,
            hold_seconds=wait_seconds,
        )

    def operation(
        self, operation_id: str, *, deadline: float | None = None, wait_seconds: float = 0
    ) -> dict[str, Any]:
        """Read an operation; ``deadline`` (``time.monotonic()``) bounds the whole read.

        With ``wait_seconds`` (at most 20), the server holds the read until the
        operation ends or that many seconds pass, instead of answering at once.
        """
        if not 0 <= wait_seconds <= MAX_SERVER_WAIT_SECONDS:
            raise UsageError(f"wait_seconds must be between 0 and {MAX_SERVER_WAIT_SECONDS:g}")
        return self._read_operation(operation_id, deadline, wait_seconds)

    def _read_operation(
        self,
        operation_id: str,
        deadline: float | None,
        wait_seconds: float,
        *,
        retry: bool | None = None,
    ) -> dict[str, Any]:
        path = f"operations/{_segment(operation_id)}"
        if wait_seconds > 0:
            path += f"?wait_seconds={wait_seconds:.3f}"
        result = self._request(
            "GET", path, deadline=deadline, hold_seconds=wait_seconds, retry=retry
        )
        if result.get("operation_id") != operation_id:
            raise LeanWarpCloudError(502, "invalid_response", "operation identity does not match")
        return result

    def wait_operations(
        self,
        operation_ids: Sequence[str],
        *,
        wait_seconds: float = 0,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Read up to 100 operations in one request.

        With ``wait_seconds`` (at most 20), the server answers as soon as any of them
        ends. The response is ``{"operations": [...], "unread": [...]}``: pending ones
        without results, finished ones in full, and in ``unread`` the IDs of finished
        ones left out to keep the response small; read each with ``operation``.
        """
        if not 1 <= len(operation_ids) <= MAX_WAITED_OPERATIONS or any(
            not isinstance(item, str) or "," in item for item in operation_ids
        ):
            raise UsageError(f"name 1 to {MAX_WAITED_OPERATIONS} operation IDs")
        if not 0 <= wait_seconds <= MAX_SERVER_WAIT_SECONDS:
            raise UsageError(f"wait_seconds must be between 0 and {MAX_SERVER_WAIT_SECONDS:g}")
        path = "operations?ids=" + ",".join(_segment(item) for item in operation_ids)
        if wait_seconds > 0:
            path += f"&wait_seconds={wait_seconds:.3f}"
        result = self._request("GET", path, deadline=deadline, hold_seconds=wait_seconds)
        if not isinstance(result.get("operations"), list) or not isinstance(
            result.get("unread"), list
        ):
            raise LeanWarpCloudError(502, "invalid_response", "expected operations and unread")
        return result

    def check(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        strict: bool = True,
        retention: Retention | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "check",
            expected_revision=expected_revision,
            payload={"file": file, "strict": strict},
            retention=retention,
            idempotency_key=idempotency_key,
        )

    def inspect(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        line: int,
        column: int,
        retention: Retention | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "inspect",
            expected_revision=expected_revision,
            payload={"file": file, "line": line, "column": column},
            retention=retention,
            idempotency_key=idempotency_key,
        )

    def try_tactics(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        line: int,
        column: int,
        tactics: list[str],
        timeout_ms: int = 30_000,
        retention: Retention | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "try_tactics",
            expected_revision=expected_revision,
            payload={
                "file": file,
                "line": line,
                "column": column,
                "tactics": tactics,
                "timeout_ms": timeout_ms,
            },
            retention=retention,
            idempotency_key=idempotency_key,
        )

    def verify_target(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        candidate_declaration: str,
        target_statement: str,
        target_context: str = "",
        execution_mode: Literal["reusable", "fresh"] = "reusable",
        timeout: int = 300,
        resource_profile: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "verify_target",
            expected_revision=expected_revision,
            payload={
                "file": file,
                "candidate_declaration": candidate_declaration,
                "target_statement": target_statement,
                "target_context": target_context,
                "execution_mode": execution_mode,
                "timeout": timeout,
            },
            resource_profile=resource_profile,
            idempotency_key=idempotency_key,
        )

    def suggest(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        line: int,
        column: int,
        tactics: list[str] | None = None,
        timeout_ms: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Close the hole at a position with a freshly rechecked patch.

        Apply the patch with ``leanwarp_cloud.edits.edited_source`` or, for a local
        project, ``ProjectSession.apply``; it binds to the exact source it read.

        Without tactics, LeanWarp ranks its own portfolio for the hole. Without
        timeout_ms, each tactic gets LeanWarp's learned budget.
        """
        payload: dict[str, Any] = {"file": file, "line": line, "column": column}
        if tactics is not None:
            payload["tactics"] = tactics
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        return self.submit(
            workspace_id,
            "suggest",
            expected_revision=expected_revision,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def holes(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        goals: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """List every `sorry` and `?_`, with goals for the first 16."""
        return self.submit(
            workspace_id,
            "holes",
            expected_revision=expected_revision,
            payload={"file": file, "goals": goals},
            idempotency_key=idempotency_key,
        )

    def extract_decls(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """The declarations a file adds, with types and dependencies."""
        return self.submit(
            workspace_id,
            "extract_decls",
            expected_revision=expected_revision,
            payload={"file": file},
            idempotency_key=idempotency_key,
        )

    def sorry2lemma(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        max_extractions: int = 64,
        name_prefix: str | None = None,
        strict_final_check: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Lift each `sorry` into a named obligation; the file is not changed."""
        payload: dict[str, Any] = {
            "file": file,
            "max_extractions": max_extractions,
            "strict_final_check": strict_final_check,
        }
        if name_prefix is not None:
            payload["name_prefix"] = name_prefix
        return self.submit(
            workspace_id,
            "sorry2lemma",
            expected_revision=expected_revision,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def check_files(
        self,
        workspace_id: str,
        files: list[str],
        *,
        expected_revision: int,
        strict: bool = True,
        timeout: int | None = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Check up to 64 files in one operation; timeout bounds each file in seconds.

        Without timeout each file has 60 seconds. A timeout must fit the
        operation's budget for its files (timeout_seconds less 40, at most 210).
        """
        if not 1 <= len(files) <= MAX_BATCH_FILES:
            raise UsageError(f"check_files takes 1 to {MAX_BATCH_FILES} files")
        payload: dict[str, Any] = {"files": list(files), "strict": strict}
        if timeout is not None:
            payload["timeout"] = timeout
        return self.submit(
            workspace_id,
            "check_files",
            expected_revision=expected_revision,
            payload=payload,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )

    def verify_batch(
        self,
        workspace_id: str,
        codes: list[dict[str, Any]],
        *,
        expected_revision: int,
        strict: bool = True,
        timeout: int | None = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Check up to 64 inline proofs in the workspace's environment in one operation.

        Each code is ``{"custom_id": ..., "proof": ...}`` with a unique custom_id.
        Proofs may import the environment's packages, such as Mathlib, but not the
        workspace's own modules. timeout bounds each proof in seconds: 60 without
        one, and at most the operation's budget for its proofs (timeout_seconds
        less 40, at most 210).

        A code may add ``"target": {"context": ..., "statement": ..., "declaration":
        ...}``: the proof must then prove that statement, checked by the checker,
        and an accepted entry carries its ``receipt`` (strict only). Without one,
        ``ok`` means the source compiles, its declarations pass the kernel and it
        uses no extra axioms, not that it proves what you meant.
        """
        if not 1 <= len(codes) <= MAX_BATCH_PROOFS:
            raise UsageError(f"verify_batch takes 1 to {MAX_BATCH_PROOFS} proofs")
        payload: dict[str, Any] = {
            "codes": [
                {
                    "custom_id": code["custom_id"],
                    "proof": code["proof"],
                    **({"target": code["target"]} if "target" in code else {}),
                }
                for code in codes
            ],
            "strict": strict,
        }
        if timeout is not None:
            payload["timeout"] = timeout
        return self.submit(
            workspace_id,
            "verify_batch",
            expected_revision=expected_revision,
            payload=payload,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )

    def wait(
        self, operation_id: str, *, timeout: float = 300, poll_interval: float = 1
    ) -> dict[str, Any]:
        """Wait until the operation is terminal or ``timeout`` seconds pass.

        Returns only a terminal operation. Raises ``OperationTimeout`` only once
        ``timeout`` has passed, or a definitive error such as not found or
        unauthorized. Anything transient is a missed poll: the operation keeps
        running on the service whether or not this client can read it.

        Each read asks the server to hold it until the operation ends, so a
        result arrives as soon as it exists. An API that answers at once is
        polled every ``poll_interval`` seconds instead. Every read and network
        wait shares the same deadline. Platform DNS and caller-injected
        transports retain their own timeout behavior (see ``_request``).
        """
        if timeout <= 0 or poll_interval <= 0:
            raise UsageError("timeout and poll_interval must be positive")
        deadline = time.monotonic() + timeout
        operation: dict[str, Any] | None = None
        use_held_reads = True
        missed = 0
        while True:
            started = time.monotonic()
            # Hold at most half the remaining time, so a stalled connection
            # still leaves room to retry, and leave time for the response.
            hold = min(
                MAX_SERVER_WAIT_SECONDS, max(0.0, (deadline - started) / 2 - _RESPONSE_MARGIN)
            )
            if not use_held_reads:
                hold = 0
            held = False
            delay = poll_interval
            try:
                operation = self._read_operation(operation_id, deadline, hold, retry=False)
            except LeanWarpTransportError as failure:
                # Some proxies end held reads before the API does; poll from then on.
                use_held_reads = use_held_reads and not failure.timed_out
                missed += 1
                delay = max(poll_interval, _retry_delay(None, missed - 1))
            except LeanWarpCloudError as error:
                if not (error.retryable or error.status == 500):
                    raise
                # A gateway that cannot hold reads answers 502 or 504; a busy
                # service answers 429 or 503 and polling faster would only add load.
                use_held_reads = use_held_reads and error.status not in {502, 504}
                missed += 1
                delay = max(poll_interval, _retry_delay(error.retry_after, missed - 1))
            else:
                if operation.get("state") in {"completed", "failed", "cancelled"}:
                    return operation
                missed = 0
                held = hold > 0 and time.monotonic() - started >= min(hold, poll_interval)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeout(operation_id, operation)
            if not held:
                time.sleep(min(delay, remaining))

    def feedback(
        self,
        kind: FeedbackKind,
        summary: str,
        *,
        details: str = "",
        severity: FeedbackSeverity = "minor",
        workspace_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Send the LeanWarp team a report about LeanWarp itself; free, and starts no compute.

        A workspace or operation the account does not hold is dropped, not refused.
        Returns ``feedback_id``, ``duplicate`` and ``occurrences``. Sent once: a
        failed report is not retried, so it never delays the caller's work.
        """
        if kind not in FEEDBACK_KINDS or severity not in FEEDBACK_SEVERITIES:
            raise UsageError("unknown feedback kind or severity")
        return self._request(
            "POST",
            "feedback",
            body={
                "kind": kind,
                "summary": summary,
                "details": details,
                "severity": severity,
                "workspace_id": workspace_id,
                "operation_id": operation_id,
            },
            idempotency_key=idempotency_key or uuid4().hex,
            deadline=deadline,
            retry=False,
        )

    def cancel(self, operation_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        """Ask to cancel. Asking again changes nothing, so a failed attempt is resent."""
        return self._request(
            "POST", f"operations/{_segment(operation_id)}/cancel", deadline=deadline, retry=True
        )

    def stop(self, workspace_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        """Stop the worker. Stopping twice changes nothing, so a failed attempt is resent."""
        return self._request(
            "POST", f"workspaces/{_segment(workspace_id)}/stop", deadline=deadline, retry=True
        )

    def delete_operation(self, operation_id: str, *, deadline: float | None = None) -> None:
        """Delete a finished operation's stored request and result now, to free the
        account's storage. Deleting again succeeds, so a failed attempt is resent.
        Resubmitting the request afterwards answers ``operation_expired``."""
        self._request(
            "DELETE", f"operations/{_segment(operation_id)}", deadline=deadline, retry=True
        )

    def delete(self, workspace_id: str, *, deadline: float | None = None) -> None:
        """Delete a workspace. A failed attempt is resent; finding it gone then is success."""
        self._request(
            "DELETE",
            f"workspaces/{_segment(workspace_id)}",
            deadline=deadline,
            retry=True,
            gone_is_done=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        deadline: float | None = None,
        hold_seconds: float = 0,
        retry: bool | None = None,
        gone_is_done: bool = False,
    ) -> dict[str, Any]:
        """Send one request, resending it after a transient failure when that is safe.

        By default a read or a write with an idempotency key is resent; ``retry``
        overrides that for writes that are naturally idempotent or must go once.
        Every attempt sends the same bytes and key. With a ``deadline``
        (``time.monotonic()``), no attempt or backoff starts after it; without one,
        no resend starts after the retry window. Each attempt splits its remaining
        budget across connect, response and body reads (see ``_attempt_timeout``).
        ``hold_seconds`` is how long the server may deliberately delay its
        response; the read timeout allows for it. With ``gone_is_done``, a 404 on
        a resend means an earlier attempt took effect and its response was lost.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        # One request identity always replays the same bytes, even if the caller
        # mutates a nested payload while a response is lost or a retry backs off.
        content = None
        if body is not None:
            try:
                encoded = json.dumps(
                    body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
            except ValueError as error:
                raise UsageError(
                    "a request holds NaN or infinity, which JSON cannot carry"
                ) from error
            content = encoded.encode("utf-8")
            headers["Content-Type"] = "application/json"
        may_retry = (method == "GET" or idempotency_key is not None) if retry is None else retry
        retry_until = deadline if deadline is not None else time.monotonic() + self._retry_window
        # A query can hold a page cursor; the log needs only the resource.
        resource = path.partition("?")[0]
        for attempt in itertools.count():
            started = time.monotonic()
            failure: LeanWarpCloudError | LeanWarpTransportError
            try:
                timeout = _attempt_timeout(deadline, hold_seconds, default=self._timeout)
                response = self._send(method, path, content, headers, timeout, deadline)
            except httpx.TransportError as transport_error:
                _log_attempt(method, resource, "transport_error", attempt, started)
                failure = LeanWarpTransportError(
                    "the request timed out"
                    if isinstance(transport_error, httpx.TimeoutException)
                    else "could not reach LeanWarp",
                    timed_out=isinstance(transport_error, httpx.TimeoutException),
                    idempotency_key=idempotency_key,
                )
                failure.__cause__ = transport_error
                retry_after = None
            except LeanWarpCloudError as error:
                _log_attempt(method, resource, error.code, attempt, started)
                error.idempotency_key = idempotency_key
                raise
            else:
                record_releases(response.headers, warn=self._interface == "python")
                _log_attempt(method, resource, response.status_code, attempt, started)
                if response.is_success:
                    return _decoded(response, idempotency_key)
                failure = _refusal(response, idempotency_key)
                if gone_is_done and attempt > 0 and failure.code == "not_found":
                    return {}
                if not failure.retryable:
                    raise failure
                retry_after = failure.retry_after
            if (
                not may_retry
                or (self._retries is not None and attempt >= self._retries)
                or not _pause(_retry_delay(retry_after, attempt), retry_until)
            ):
                raise failure
        raise AssertionError("unreachable: the attempts above never end")

    def _send(
        self,
        method: str,
        path: str,
        request_content: bytes | None,
        headers: Mapping[str, str],
        timeout: httpx.Timeout | None = None,
        deadline: float | None = None,
    ) -> httpx.Response:
        with self._client.stream(
            method,
            f"v1/leanwarp/{path}",
            content=request_content,
            headers=headers,
            extensions={"leanwarp_deadline": deadline},
            # httpx.USE_CLIENT_DEFAULT keeps the client's configured timeout.
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        ) as response:
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise LeanWarpCloudError(
                    502, "invalid_encoding", "expected an uncompressed response"
                )
            content = bytearray()
            # Fixed-size chunks buffer small transport reads until the chunk is
            # full or EOF, hiding a trickling body from the deadline check.
            for chunk in response.iter_bytes():
                if deadline is not None and time.monotonic() >= deadline:
                    raise httpx.ReadTimeout("deadline reached", request=response.request)
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise LeanWarpCloudError(
                        502, "response_too_large", "response exceeds SDK limit"
                    )
                content.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(content),
                request=response.request,
            )


def _attempt_timeout(
    deadline: float | None, hold_seconds: float = 0, *, default: float = 30.0
) -> httpx.Timeout | None:
    """Limit each blocking network wait to a third of the remaining budget.

    A read the server holds for ``hold_seconds`` may wait that long for its
    response, within the remaining budget. The default transport also clamps
    each network read (including headers) to the remaining deadline. Platform
    DNS and caller-injected transports retain their own blocking behavior; no
    background request survives expiry.
    """
    if deadline is None:
        if hold_seconds <= 0:
            return None
        return httpx.Timeout(default, read=max(default, hold_seconds + _RESPONSE_MARGIN))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.TimeoutException("deadline reached")
    if hold_seconds <= 0:
        return httpx.Timeout(remaining / 3)
    return httpx.Timeout(remaining / 3, read=min(remaining, hold_seconds + _RESPONSE_MARGIN))


def _retry_delay(retry_after: float | None, attempt: int) -> float:
    """Seconds before the next attempt: the service's ``Retry-After``, or exponential backoff.

    Both are jittered, so processes that failed together do not retry together. A
    service's value is a floor and is never shortened.
    """
    if retry_after:
        return retry_after + random.uniform(0, min(1.0, retry_after / 4))  # noqa: S311
    # Saturate the exponent first: 2**attempt overflows a float long before a
    # long wait runs out, and every exponent past this one gives the cap anyway.
    ceiling = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** min(attempt, 16))
    return random.uniform(ceiling / 2, ceiling)  # noqa: S311


def _pause(delay: float, until: float) -> bool:
    """Sleep before a retry, or return False when the retry cannot start in time."""
    if time.monotonic() + delay >= until:
        return False
    time.sleep(delay)
    return True


def _log_attempt(
    method: str, resource: str, outcome: int | str, attempt: int, started: float
) -> None:
    """One debug line per attempt. Never a key, token, source or proof."""
    _LOG.debug(
        "%s %s status=%s attempt=%d latency_ms=%d",
        method,
        resource,
        outcome,
        attempt + 1,
        (time.monotonic() - started) * 1000,
    )


def _refusal(response: httpx.Response, idempotency_key: str | None) -> LeanWarpCloudError:
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    return LeanWarpCloudError(
        response.status_code,
        str(error.get("code", "request_failed")),
        str(error.get("message", f"request failed with HTTP {response.status_code}")),
        retry_after=_retry_after(response.headers.get("Retry-After", "")),
        idempotency_key=idempotency_key,
    )


def _retry_after(value: str) -> float | None:
    """Seconds a ``Retry-After`` asks for: a number of seconds, or an HTTP date from now."""
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        # Unparsable, such as a zone offset too large to represent: as if absent.
        return None
    # An HTTP date is always in GMT; a date without a zone is not one.
    if when.tzinfo is None:
        return None
    # datetime.UTC needs Python 3.11; the SDK supports 3.10.
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())  # noqa: UP017


def _decoded(response: httpx.Response, idempotency_key: str | None) -> dict[str, Any]:
    if response.status_code == 204:
        return {}
    try:
        result = response.json()
    except ValueError as error:
        raise LeanWarpCloudError(
            502, "invalid_response", "expected valid JSON", idempotency_key=idempotency_key
        ) from error
    if not isinstance(result, dict):
        raise LeanWarpCloudError(
            502, "invalid_response", "expected a JSON object", idempotency_key=idempotency_key
        )
    return result


def _set_retention(body: dict[str, Any], retention: Retention | None) -> None:
    """Send a retention only when asked for one: the service's default is standard."""
    if retention is None:
        return
    if retention not in RETENTIONS:
        raise UsageError('retention must be "standard" or "short"')
    body["retention"] = retention


def _segment(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise UsageError("identifier must be a nonempty path segment")
    return quote(value, safe="")

"""Synchronous agent client with bounded retries and durable operation handles."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx

from .endpoints import api_origin
from .project import matching_bundles, selects
from .transport import DeadlineTransport

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class LeanWarpCloudError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class OperationTimeout(TimeoutError):
    """Polling expired; the operation remains addressable and may still be running."""

    def __init__(self, operation_id: str, operation: dict[str, Any] | None = None) -> None:
        self.operation_id = operation_id
        # The last non-terminal observation, when polling read one.
        self.operation = operation
        super().__init__(f"operation {operation_id} is still pending; poll again or cancel it")


class LeanWarpCloud:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = api_origin(api_key) if base_url is None else base_url
        parsed = urlparse(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("base_url must use HTTPS (HTTP is supported only on localhost)")
        if (
            not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an origin without a path, credentials, query or fragment"
            )
        if not api_key or api_key.strip() != api_key or "\n" in api_key or "\r" in api_key:
            raise ValueError("api_key must be a nonempty token")
        if timeout <= 0 or not 0 <= retries <= 5:
            raise ValueError("timeout must be positive and retries must be between zero and five")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"},
            timeout=timeout,
            transport=transport if transport is not None else DeadlineTransport(base_url),
            follow_redirects=False,
            trust_env=False,
        )
        self._retries = retries
        self.base_url = base_url.rstrip("/")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LeanWarpCloud:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def account(self) -> dict[str, Any]:
        """Authenticated owner and exact posted, reserved and available microdollars."""
        return self._request("GET", "account")

    def versions(self) -> dict[str, Any]:
        return self._request("GET", "versions")

    def resources(self) -> dict[str, Any]:
        return self._request("GET", "resources")

    def create_workspace_for_project(
        self,
        root: str | Path,
        *,
        bundle_id: str | None = None,
        resource_profile: str = "standard",
        max_resource_profile: str = "standard",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        matches = [
            bundle
            for bundle in matching_bundles(root, self.versions()["versions"])
            if selects(bundle, bundle_id)
        ]
        if not matches:
            raise ValueError("project does not match a supported environment bundle")
        # The service lists each environment's current build; every workspace
        # starts new workers on its environment's current build.
        return self.create_workspace(
            matches[-1]["bundle_id"],
            resource_profile=resource_profile,
            max_resource_profile=max_resource_profile,
            idempotency_key=idempotency_key,
        )

    def create_workspace(
        self,
        bundle_id: str,
        *,
        resource_profile: str = "standard",
        max_resource_profile: str = "standard",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "workspaces",
            body={
                "bundle_id": bundle_id,
                "resource_profile": resource_profile,
                "max_resource_profile": max_resource_profile,
                # Explicit null prevents an older API's implicit zero-dollar
                # default from creating a workspace that can never execute.
                "max_spend_microusd": None,
            },
            idempotency_key=idempotency_key or uuid4().hex,
        )

    def workspaces(self) -> dict[str, Any]:
        return self._request("GET", "workspaces")

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"workspaces/{_segment(workspace_id)}")

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
        idempotency_key: str | None = None,
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
                raise ValueError("timeout_seconds must be an integer between 1 and 600")
            body["timeout_seconds"] = timeout_seconds
        return self._request(
            "POST",
            f"workspaces/{_segment(workspace_id)}/operations",
            body=body,
            idempotency_key=idempotency_key or uuid4().hex,
        )

    def operation(self, operation_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        """Read an operation; ``deadline`` (``time.monotonic()``) bounds the whole read."""
        result = self._request("GET", f"operations/{_segment(operation_id)}", deadline=deadline)
        if result.get("operation_id") != operation_id:
            raise LeanWarpCloudError(502, "invalid_response", "operation identity does not match")
        return result

    def check(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        strict: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "check",
            expected_revision=expected_revision,
            payload={"file": file, "strict": strict},
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
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            workspace_id,
            "inspect",
            expected_revision=expected_revision,
            payload={"file": file, "line": line, "column": column},
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

    def wait(
        self, operation_id: str, *, timeout: float = 300, poll_interval: float = 1
    ) -> dict[str, Any]:
        """Poll until the operation is terminal or ``timeout`` seconds pass.

        Every poll request, retry and network read shares the same deadline.
        Platform DNS and caller-injected transports retain their own timeout
        behavior (see ``_request``).
        """
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        deadline = time.monotonic() + timeout
        operation: dict[str, Any] | None = None
        while True:
            try:
                operation = self.operation(operation_id, deadline=deadline)
            except httpx.TimeoutException:
                # A slow read before the deadline is just a missed poll.
                if time.monotonic() >= deadline:
                    raise OperationTimeout(operation_id, operation) from None
            else:
                if operation.get("state") in {"completed", "failed", "cancelled"}:
                    return operation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeout(operation_id, operation)
            time.sleep(min(poll_interval, remaining))

    def cancel(self, operation_id: str) -> dict[str, Any]:
        return self._request("POST", f"operations/{_segment(operation_id)}/cancel")

    def stop(self, workspace_id: str) -> dict[str, Any]:
        return self._request("POST", f"workspaces/{_segment(workspace_id)}/stop")

    def delete(self, workspace_id: str) -> None:
        self._request("DELETE", f"workspaces/{_segment(workspace_id)}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Send with bounded retries.

        With a ``deadline`` (``time.monotonic()``), no attempt or backoff starts
        after it, and each attempt splits its remaining budget across connect,
        response and body reads (see ``_attempt_timeout``). Expiry raises
        ``httpx.TimeoutException``.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        # One request identity always replays the same bytes, even if the caller
        # mutates a nested payload while a response is lost or a retry backs off.
        content = None
        if body is not None:
            content = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        retryable = method == "GET" or idempotency_key is not None
        attempts = self._retries + 1 if retryable else 1
        for attempt in range(attempts):
            timeout = _attempt_timeout(deadline)
            try:
                response = self._send(method, path, content, headers, timeout, deadline)
            except httpx.TransportError:
                if attempt + 1 == attempts or not _backoff(min(0.25 * 2**attempt, 2), deadline):
                    raise
                continue
            if response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After", "")
                delay = min(float(retry_after), 5) if retry_after.isdigit() else 0.25 * 2**attempt
                if _backoff(delay, deadline):
                    continue
            if not response.is_success:
                try:
                    error = response.json().get("error", {})
                except (ValueError, AttributeError):
                    error = {}
                if not isinstance(error, dict):
                    error = {}
                raise LeanWarpCloudError(
                    response.status_code,
                    str(error.get("code", "request_failed")),
                    str(error.get("message", f"request failed with HTTP {response.status_code}")),
                )
            if response.status_code == 204:
                return {}
            try:
                result = response.json()
            except ValueError as error:
                raise LeanWarpCloudError(502, "invalid_response", "expected valid JSON") from error
            if not isinstance(result, dict):
                raise LeanWarpCloudError(502, "invalid_response", "expected a JSON object")
            return result
        raise AssertionError("request attempts exhausted without a result")

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


def _attempt_timeout(deadline: float | None) -> httpx.Timeout | None:
    """Limit each blocking network wait to a third of the remaining budget.

    The default transport also clamps each network read (including headers)
    to the remaining deadline. Platform DNS and caller-injected transports
    retain their own blocking behavior; no background request survives expiry.
    """
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.TimeoutException("deadline reached")
    return httpx.Timeout(remaining / 3)


def _backoff(delay: float, deadline: float | None) -> bool:
    """Sleep before a retry, or return False when the retry cannot start in time."""
    if deadline is not None and time.monotonic() + delay >= deadline:
        return False
    time.sleep(delay)
    return True


def _segment(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("identifier must be a nonempty path segment")
    return quote(value, safe="")

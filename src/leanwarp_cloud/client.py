"""Synchronous agent client with bounded retries and durable operation handles."""

from __future__ import annotations

import email.utils
import functools
import hashlib
import hmac
import itertools
import json
import logging
import math
import random
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import httpx

from ._version import __version__
from .amounts import validate_spending_cap
from .endpoints import api_origin, public_origin
from .environment import (
    Commit,
    ProjectInputs,
    Stamp,
    answer_fingerprints,
    changed_answers,
    committed_bytes,
    head_of,
    project_answers,
    project_inputs,
)
from .errors import (
    ConnectTimeout,
    EnvironmentBuildError,
    LeanWarpCloudError,
    LeanWarpTransportError,
    OperationTimeout,
    ProjectError,
    UsageError,
)
from .project import newest_toolchain
from .transport import DeadlineTransport
from .updates import record as record_releases

# Local wait budget for building a new environment and checking it before attach.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 4 * 60 * 60

_LOG = logging.getLogger("leanwarp")
# The check a ProjectSession installs for the requests its own command sends.
# The deadline of the connect this thread or task is running, if any: no resend or
# backoff of any request it makes starts after it.
_CONNECT_DEADLINE: ContextVar[float] = ContextVar("leanwarp_connect_deadline", default=math.inf)
_RESEND_CHECK: ContextVar[tuple[tuple[LeanWarpCloud, Callable[[], None]], ...]] = ContextVar(
    "leanwarp_resend_check", default=()
)
# The most answers one request carries.
_ANSWER_BATCH = 64
# How often one connect starts its exchange over because the project's files
# changed under it, before it reports the project as unreadable.
_EXCHANGE_RESTARTS = 3
# The most blobs one upload names.
_UPLOAD_BATCH = 4096
_UPLOAD_RECEIPT_BYTES = 512 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Failures before a connection is made: the request never left the client.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
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
    "have2lemma",
    "simplify_proofs",
    "repair_proofs",
    "check_files",
    "verify_batch",
    "proof_states",
]
OPERATION_KINDS: frozenset[str] = frozenset(get_args(OperationKind))
#: How long a finished operation is kept before history may remove it to admit new
#: work: about an hour (a day for check_files and verify_batch), or about a minute
#: with ``"short"``, which only check, inspect and try_tactics accept.
Retention = Literal["standard", "short"]
RETENTIONS: frozenset[str] = frozenset(get_args(Retention))
#: The most files one check_files operation checks.
MAX_BATCH_FILES = 64
#: The most proofs one verify_batch operation checks.
MAX_BATCH_PROOFS = 64
#: The most bytes a verify_batch payload takes, measured as the API measures it
#: (``payload_bytes``).
MAX_BATCH_BYTES = 262_144
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
# A blob whose address the service is still erasing is sent again this often.
_BUSY_SECONDS = 5.0
_BUSY_RETRIES = 6
_MCP_RETRY_WINDOW_SECONDS = 15.0
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_CAP_SECONDS = 5.0


def batch_payload(
    codes: Iterable[Mapping[str, Any]], *, strict: bool, timeout: int | None
) -> dict[str, Any]:
    """The verify_batch payload for these codes, as it is sent."""
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
    return payload


def payload_bytes(value: Any) -> int:
    """UTF-8 bytes of ``value`` as JSON, as the API measures a verify_batch payload."""
    return len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8"))


def _frozen(value: Any) -> Any:
    """A read-only copy, all the way down: what an observer is given."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


def _upload_batches(sizes: Mapping[str, int]) -> Iterator[dict[str, int]]:
    batch: dict[str, int] = {}
    total = 0
    for digest, size in sizes.items():
        if batch and (len(batch) == _UPLOAD_BATCH or total + size > _UPLOAD_RECEIPT_BYTES):
            yield batch
            batch = {}
            total = 0
        batch[digest] = size
        total += size
    if batch:
        yield batch


def _changed(paths: list[str]) -> ProjectError:
    return ProjectError(
        f"{', '.join(paths[:3])} changed while connect ran, after the build read it; run "
        "connect again, or commit it so LeanWarp reads the committed copy"
    )


@dataclass
class _Exchange:
    """One connect's deadline and the restarts it has made because the project's
    files changed under it."""

    deadline: float = math.inf
    restarts: int = 0
    # Told each change in a waited-on build: its phase, or the reads it asks for.
    progress: Callable[[Mapping[str, Any]], None] | None = None
    _seen: tuple[object, ...] | None = field(default=None, init=False)
    # The connect's first request is made whatever its deadline: an environment
    # already ready is returned at once.
    exempt: bool = True
    # Each answered file its repository's commit holds as it was answered.
    committed: dict[str, Commit] = field(default_factory=dict)
    # The first answer each build was given for a path, with its commit then: every
    # build this connect answered must have read the same, still when it attaches.
    given: dict[str, tuple[Any, Commit | None, Stamp | None]] = field(default_factory=dict)
    # What each latest answer was read from, as it was then.
    stamps: dict[str, Stamp] = field(default_factory=dict)
    # Once a stored copy was lost: an answer a build already got from a commit is
    # given again with its bytes, as that commit holds them.
    restore: bool = False

    def give(self, answers: Mapping[str, Any], committed: Mapping[str, Commit]) -> None:
        """Record answers sent to a build; one that differs from an earlier answer to
        the same path refuses the connect."""
        self.agree(answers)
        for path, answer in answers.items():
            self.given.setdefault(path, (answer, committed.get(path), self.stamps.get(path)))

    def held(self, paths: Iterable[str]) -> dict[str, Any]:
        """The answers builds already got from a commit that still holds: given again
        as they were, whatever the working copies hold since."""
        heads: dict[str, str | None] = {}
        held: dict[str, Any] = {}
        for path in paths:
            answer, commit, _ = self.given.get(path, (None, None, None))
            if commit is None:
                continue
            if commit.repository not in heads:
                heads[commit.repository] = head_of(commit.repository, deadline=self.deadline)
            if heads[commit.repository] == commit.head:
                held[path] = answer
                self.committed[path] = commit
        return held

    def read(
        self, root: str | Path, paths: Iterable[str], committed: dict[str, Commit]
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        """This connect's answers to ``paths``: a committed one a build already got,
        while its commit holds; any other read from the project now."""
        held = self.held(paths)
        answers, blobs = project_answers(
            root,
            sorted(set(paths) - set(held)),
            committed=committed,
            stamps=self.stamps,
            deadline=self.deadline,
        )
        committed.update({path: self.given[path][1] for path in held})  # type: ignore[misc]
        return {**answers, **held}, blobs

    def agree(self, answers: Mapping[str, Any]) -> None:
        """Refuse answers that differ from what a build was already given."""
        conflicts = sorted(
            path
            for path, answer in answers.items()
            if path in self.given and self.given[path][0] != answer
        )
        if conflicts:
            raise _changed(conflicts)

    def changed(self, root: str | Path, answered: Mapping[str, Any]) -> None:
        """Refuse the connect if an answer it gave no longer holds."""
        self.agree(answered)
        answers = {path: answer for path, (answer, _, _) in self.given.items()}
        commits = {path: commit for path, (_, commit, _) in self.given.items() if commit}
        stamps = {path: stamp for path, (_, _, stamp) in self.given.items() if stamp}
        for path, answer in answered.items():
            if path not in self.given:
                answers[path] = answer
                if path in self.committed:
                    commits[path] = self.committed[path]
                if path in self.stamps:
                    stamps[path] = self.stamps[path]
        changed = changed_answers(root, answers, commits, stamps, deadline=self.deadline)
        if changed:
            raise _changed(changed)

    def report(self, environment: Mapping[str, Any]) -> None:
        """Tell the observer of a change, once: a read-only copy it cannot alter the
        exchange through, and whatever it raises, the connect goes on. It runs on the
        caller's own thread, so a slow one spends the caller's own deadline."""
        if self.progress is None:
            return
        requests = tuple(str(path) for path in environment.get("input_requests") or ())
        seen = (environment.get("environment_id"), environment.get("phase"), requests)
        if seen == self._seen:
            return
        self._seen = seen
        try:
            self.progress(_frozen({**environment, "input_requests": requests}))
        except Exception:
            _LOG.debug("leanwarp connect progress observer failed")

    def spend(self) -> None:
        if self.exempt:
            self.exempt = False
        else:
            self.check()

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ConnectTimeout()

    def restart(self) -> None:
        self.restarts += 1
        if self.restarts > _EXCHANGE_RESTARTS:
            raise LeanWarpCloudError(
                409,
                "invalid_project_input",
                "the project's files kept changing while it connected; connect again once "
                "they are saved",
            )


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

    @property
    def resend_horizon(self) -> float:
        """About how long after a request starts this client may still send it again
        without a deadline: its retry window and one attempt's timeout. Connection
        setup and a delayed retry can add a few seconds."""
        return self._retry_window + self._timeout

    @contextmanager
    def resends_checked(self, check: Callable[[], None]) -> Iterator[None]:
        """Run ``check`` before every automatic resend of a write this block sends.

        The check belongs to this thread or task alone, so projects sharing a client
        never run each other's."""
        token = _RESEND_CHECK.set((*_RESEND_CHECK.get(), (self, check)))
        try:
            yield
        finally:
            _RESEND_CHECK.reset(token)

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
        """Qualified Lean toolchains under `toolchains`."""
        return self._request("GET", "versions")

    def resources(self) -> dict[str, Any]:
        """Raw release billing catalog for wire integrations; starts no compute."""
        return self._request("GET", "resources")

    def upload_project_inputs(
        self, inputs: ProjectInputs, *, before: Callable[[], None] = lambda: None
    ) -> None:
        """Send the blobs an environment request names that the account does not hold.

        Each blob is named by its SHA-256 first, so nothing already held is resent.
        ``before`` runs before each request (a connect's deadline check).
        """
        # In the inputs' own order: what names other blobs is stored after them.
        self._upload(
            {name: len(data) for name, data in inputs.blobs.items()},
            inputs.blobs.__getitem__,
            before,
        )

    def _upload(
        self, sizes: Mapping[str, int], read: Callable[[str], bytes], before: Callable[[], None]
    ) -> None:
        """Name each blob by digest and size, a batch to a receipt, then send only those
        the service lacks, each read just before it is sent."""
        for batch in _upload_batches(sizes):
            before()
            receipt = self._request(
                "POST",
                "environment-uploads",
                body={"blobs": batch},
                idempotency_key=uuid4().hex,
            )
            missing = receipt.get("missing")
            upload = receipt.get("upload_id")
            if not isinstance(missing, list) or not isinstance(upload, str):
                raise LeanWarpCloudError(502, "invalid_response", "expected an upload receipt")
            absent = set(missing)
            if not absent <= set(batch):
                raise LeanWarpCloudError(502, "invalid_response", "unexpected upload")
            for name in (name for name in batch if name in absent):
                data = read(name)
                for attempt in itertools.count():
                    before()
                    try:
                        self._request(
                            "PUT",
                            f"environment-uploads/{quote(upload, safe='')}/blobs/{name}",
                            raw=data,
                            retry=True,
                        )
                        break
                    except LeanWarpCloudError as error:
                        # Its stored copy is being removed (it was found lost or
                        # unrecorded): the address is free again within seconds.
                        left = _CONNECT_DEADLINE.get() - time.monotonic()
                        if error.code != "upload_busy" or attempt >= _BUSY_RETRIES or left <= 0:
                            raise
                        time.sleep(min(_BUSY_SECONDS, left))
                # The next blob is read only once this one's bytes are let go.
                del data

    def submit_environment(
        self, inputs: ProjectInputs, *, root: str | Path | None = None
    ) -> dict[str, Any]:
        """Find or start the environment for a project's inputs: ready, or building.

        Inputs the account already holds are not uploaded again. A ready set whose
        build read files outside the project is reused only if the project at
        ``root`` holds the same there; without ``root`` that refusal is raised."""
        return self._submit(inputs, root, _Exchange())[0]

    def _submit(
        self, inputs: ProjectInputs, root: str | Path | None, exchange: _Exchange
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The environment, and the answers this submission gave about the project."""
        body = inputs.request
        uploaded = False
        while True:
            # Every request after the connect's first counts against its deadline.
            exchange.spend()
            try:
                environment = self._request("POST", "environments", body=body, retry=True)
                return environment, dict(body.get("inputs", {}))
            except LeanWarpCloudError as error:
                if error.code == "upload_required" and not uploaded:
                    self.upload_project_inputs(inputs, before=exchange.check)
                    uploaded = True
                elif (
                    error.code == "inputs_required"
                    and root is not None
                    and (
                        asked := [
                            path for path in error.paths if path not in body.get("inputs", {})
                        ]
                    )
                ):
                    # Ready sets are offered in batches: answer each batch's reads too,
                    # in one pass with every earlier answer, which must still hold.
                    previous = body.get("inputs", {})
                    answers, _ = exchange.read(
                        root, sorted({*previous, *asked}), exchange.committed
                    )
                    # Never what a build of this connect was given, changed since.
                    exchange.agree(answers)
                    if any(answers[path] != answer for path, answer in previous.items()):
                        exchange.restart()
                        body = {key: value for key, value in body.items() if key != "inputs"}
                        continue
                    body = {**body, "inputs": answers}
                else:
                    raise

    def answer_environment_inputs(
        self,
        environment: Mapping[str, Any],
        root: str | Path,
        *,
        before: Callable[[], None] = lambda: None,
        exchange: _Exchange | None = None,
    ) -> dict[str, Any]:
        """Answer the reads a build waits on from the project's own files; ``before``
        runs before each request. Within a connect, ``exchange`` records what each
        build was given and refuses an answer that differs from an earlier one."""
        paths = [str(path) for path in environment.get("input_requests", [])]
        committed: dict[str, Commit] = {}
        answers, blobs = (
            project_answers(root, paths, committed=committed)
            if exchange is None
            else exchange.read(root, paths, committed)
        )
        if exchange is not None:
            # Never a different answer than one a build of this connect accepted.
            exchange.agree(answers)
        if blobs:
            self.upload_project_inputs(ProjectInputs(request={}, blobs=blobs), before=before)
        reply: dict[str, Any] = dict(environment)
        names = list(answers)
        for start in range(0, len(names), _ANSWER_BATCH):
            before()
            batch = {name: answers[name] for name in names[start : start + _ANSWER_BATCH]}
            if exchange is not None and exchange.restore:
                self._restore_committed(root, exchange, batch, set(blobs), before)
            try:
                reply = self._request(
                    "POST",
                    f"environments/{quote(str(environment['environment_id']), safe='')}/inputs",
                    body={
                        "attempt_id": environment["attempt_id"],
                        # The reads' epoch: answers read for it never satisfy a later one.
                        "request_token": environment.get("input_token"),
                        "answers": batch,
                    },
                    retry=True,
                )
            except LeanWarpCloudError as error:
                # Refused at every send, a build never read them; any other outcome,
                # a refused resend of a send whose reply was lost included, may have
                # reached one, so those answers are kept.
                if exchange is not None and (error.resent or not 400 <= (error.status or 0) < 500):
                    exchange.give(batch, committed)
                raise
            except LeanWarpTransportError:
                if exchange is not None:
                    exchange.give(batch, committed)
                raise
            if exchange is not None:
                exchange.give(batch, committed)
        return reply

    def _restore_committed(
        self,
        root: str | Path,
        exchange: _Exchange,
        batch: Mapping[str, Any],
        sent: set[str],
        before: Callable[[], None],
    ) -> None:
        """After a stored copy was lost: the answers in ``batch`` reused from this
        connect's record are named to the service once, and each it lacks is read from
        its commit and sent alone, so one file's bytes are held at a time."""
        commits: dict[str, tuple[str, Commit]] = {}
        sizes: dict[str, int] = {}
        for path, answer in batch.items():
            given = exchange.given.get(path)
            commit = None if given is None else given[1]
            if commit is None or answer.get("kind") != "file" or answer["sha256"] in sent:
                continue
            commits[answer["sha256"]] = (path, commit)
            sizes[answer["sha256"]] = int(answer["size"])

        def read(digest: str) -> bytes:
            path, commit = commits[digest]
            data = committed_bytes(root, path, commit, exchange.deadline)
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                raise _changed([path])
            return data

        if sizes:
            self._upload(sizes, read, before)

    def _answer_or_reread(
        self, environment: Mapping[str, Any], root: str | Path, exchange: _Exchange
    ) -> dict[str, Any]:
        """Answer the reads the build waits on; if it has moved on since they were
        read (another waiter answered, or it restarted), read it again."""
        try:
            return self.answer_environment_inputs(
                environment, root, before=exchange.check, exchange=exchange
            )
        except LeanWarpCloudError as error:
            if error.code not in ("stale_attempt", "invalid_request"):
                raise
            exchange.check()
            current = self.environment(str(environment["environment_id"]))
            unchanged = all(
                current.get(key) == environment.get(key)
                for key in ("state", "attempt_id", "input_requests")
            )
            if error.code == "invalid_request" and unchanged:
                raise
            return current

    def environment(self, environment_id: str) -> dict[str, Any]:
        return self._request("GET", f"environments/{quote(environment_id, safe='')}")

    def environments(self) -> dict[str, Any]:
        return self._request("GET", "environments")

    def retry_environment(self, environment_id: str, attempt_id: str) -> dict[str, Any]:
        """Retry a failed or cancelled build, naming the attempt it failed in."""
        return self._request(
            "POST",
            f"environments/{quote(environment_id, safe='')}/retry",
            body={"attempt_id": attempt_id},
            idempotency_key=uuid4().hex,
        )

    def cancel_environment(self, environment_id: str, attempt_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"environments/{quote(environment_id, safe='')}/cancel",
            body={"attempt_id": attempt_id},
            retry=True,
        )

    def connect_environment(
        self,
        root: str | Path,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        poll_seconds: float = 5,
        progress: Callable[[Mapping[str, Any]], None] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        """The ready environment for the project at ``root``, waiting while it builds.

        Its toolchain, lakefile, lockfile and path dependencies define it; LeanWarp
        fetches pinned Git dependencies and builds it once. Raises
        ``EnvironmentBuildError`` with the logical file or package if it fails; with
        ``retry``, a failed or cancelled build is retried once first.
        """
        # One deadline, one restart budget and one record of what each build read
        # for the whole exchange: submission, waiting, replay, a retry and the final
        # check alike, every resend included.
        exchange = _Exchange(deadline=time.monotonic() + timeout, progress=progress)
        bound = _CONNECT_DEADLINE.set(min(exchange.deadline, _CONNECT_DEADLINE.get()))
        try:
            try:
                return self._connect(root, exchange, poll_seconds)
            except EnvironmentBuildError as error:
                failed = error.environment
                if (
                    not retry
                    or failed.get("phase") == "retiring"
                    or failed.get("state") not in ("failed", "cancelled")
                ):
                    raise
                # Nothing is asked once the connect's deadline has passed.
                exchange.check()
                if (failed.get("error") or {}).get("code") == "stored_file_missing":
                    # An answer reused from this connect's record is sent with its
                    # bytes from now on, read from its commit: they may be what was
                    # lost, whichever build of this connect asks next.
                    exchange.restore = True
                    # A stored copy of an input was lost: every input is offered
                    # again, and the service asks for those it no longer holds.
                    current = project_inputs(root, default_toolchain=self._default_toolchain)
                    if current.digest != failed.get("inputs_sha256"):
                        # The project changed since: its own environment, not a retry.
                        return self._connect(root, exchange, poll_seconds)
                    self.upload_project_inputs(current, before=exchange.check)
                    if project_inputs(root, default_toolchain=self._default_toolchain).digest != (
                        current.digest
                    ):
                        return self._connect(root, exchange, poll_seconds)
                    exchange.check()
                self.retry_environment(str(failed["environment_id"]), str(failed["attempt_id"]))
                return self._connect(root, exchange, poll_seconds)
        finally:
            _CONNECT_DEADLINE.reset(bound)

    def _connect(
        self, root: str | Path, exchange: _Exchange, poll_seconds: float
    ) -> dict[str, Any]:
        # One lookup per connect: the final check asks nothing new.
        toolchain = functools.cache(self._default_toolchain)
        while True:
            inputs = project_inputs(root, default_toolchain=toolchain)
            environment, answered = self._submit(inputs, root, exchange)
            environment, answered = self._attach(
                environment, answered, inputs, root, exchange, poll_seconds
            )
            # Attached only if the project still is what was keyed and answered: a
            # file answered from its commit holds while that commit does; any other
            # answered file must still be as it was read.
            if project_inputs(root, default_toolchain=toolchain).digest != inputs.digest:
                exchange.restart()
                continue
            exchange.changed(root, answered)
            # Ready, but only within the deadline: checking took time too.
            exchange.check()
            observations = {path: value[0] for path, value in exchange.given.items()}
            observations.update(answered)
            commits = dict(exchange.committed)
            for path, (_, commit, _) in exchange.given.items():
                if commit is None:
                    commits.pop(path, None)
                else:
                    commits[path] = commit
            return {
                **environment,
                "inputs_sha256": inputs.digest,
                "input_observations": answer_fingerprints(root, observations, commits),
            }

    def _attach(
        self,
        environment: dict[str, Any],
        answered: dict[str, Any],
        inputs: ProjectInputs,
        root: str | Path,
        exchange: _Exchange,
        poll_seconds: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Wait for the build, answering its reads, then replay until attached."""
        while True:
            built = False
            while environment.get("state") == "building":
                # Whoever answers its reads, a build this waits on may be shared.
                built = True
                exchange.report(environment)
                exchange.check()
                if environment.get("input_requests"):
                    # The build reads files outside the project: this project's own.
                    environment = self._answer_or_reread(environment, root, exchange)
                    continue
                # Never asleep past the deadline, and no read once it has passed.
                time.sleep(min(poll_seconds, exchange.remaining()))
                exchange.check()
                environment = self.environment(str(environment["environment_id"]))
            if environment.get("state") != "ready":
                # With what was submitted: a retry is of these inputs only.
                raise EnvironmentBuildError({**environment, "inputs_sha256": inputs.digest})
            if not built:
                return environment, answered
            # What this connect told the build must still hold, before a replay could
            # start another build from changed files.
            exchange.changed(root, {})
            # A build shared with other waiters may have read other copies of those
            # files: attach only to a set whose reads this project's files agree with.
            environment, answered = self._submit(inputs, root, exchange)

    def toolchain_environment(self, lean_toolchain: str | None = None) -> dict[str, Any]:
        """The ready environment plain Lean files run in: the toolchain's public
        Mathlib, by default on the newest supported Lean."""
        chosen = lean_toolchain or self._default_toolchain()
        if chosen is None:
            raise LeanWarpCloudError(503, "unsupported_toolchain", "no toolchain is supported")
        environment = self.submit_environment(
            ProjectInputs(request={"lean_toolchain": chosen}, blobs={})
        )
        if environment.get("state") != "ready":
            raise EnvironmentBuildError(environment)
        return environment

    def _default_toolchain(self) -> str | None:
        """The newest qualified stable toolchain, for plain Lean files."""
        versions = [
            item.get("lean_toolchain")
            for item in self.versions().get("toolchains", [])
            if isinstance(item, dict)
        ]
        return newest_toolchain([item for item in versions if isinstance(item, str)])

    def create_workspace_for_project(
        self,
        root: str | Path,
        *,
        resource_profile: str | None = None,
        max_resource_profile: str | None = None,
        max_spend_microusd: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace in the project's own environment, waiting for it."""
        validate_spending_cap(max_spend_microusd)
        environment = self.connect_environment(root)
        return self.create_workspace(
            str(environment["environment_id"]),
            resource_profile=resource_profile,
            max_resource_profile=max_resource_profile,
            max_spend_microusd=max_spend_microusd,
            idempotency_key=idempotency_key,
        )

    def create_workspace(
        self,
        environment_id: str,
        *,
        resource_profile: str | None = None,
        max_resource_profile: str | None = None,
        max_spend_microusd: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace without compute in a ready environment, optionally
        capped in exact microdollars.

        The cap covers total spending plus reserved credit; None means no cap.
        """
        # Omitted profiles use the service's reviewed default. Explicit names
        # still select that profile, including clients that request standard.
        # Explicit null prevents an older API's implicit zero-dollar cap from
        # creating a workspace that can never execute.
        validate_spending_cap(max_spend_microusd)
        body: dict[str, Any] = {
            "environment_id": environment_id,
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
        """Each history cap with its current use; finished work makes room after an hour
        (a day for a batch)."""
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
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1800:
                raise UsageError("timeout_seconds must be an integer between 1 and 1800")
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
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1800:
                raise UsageError("timeout_seconds must be an integer between 1 and 1800")
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

    def proof_states(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        cursor: int = 0,
        max_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """One page of every tactic Lean ran in the file, with its goals before and after.

        Rows come in command order, each tactic before those nested in it, with its
        ``source_range``, ``occurrence`` (a tactic run once per goal repeats its
        range) and ``before`` and ``after`` states naming entries of the page's
        ``states``. Pass ``next_cursor`` as ``cursor`` for the next page; it is
        None after the last. ``max_bytes`` bounds the page (4096 to 524288, 262144
        by default). ``status`` ``unsupported``: the environment's build cannot
        list tactic states yet.
        """
        payload: dict[str, Any] = {"file": file, "cursor": cursor}
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        return self.submit(
            workspace_id,
            "proof_states",
            expected_revision=expected_revision,
            payload=payload,
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

    def have2lemma(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        max_extractions: int = 64,
        name_prefix: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Lift each named `have` into its own lemma; the file is not changed."""
        payload: dict[str, Any] = {"file": file, "max_extractions": max_extractions}
        if name_prefix is not None:
            payload["name_prefix"] = name_prefix
        return self.submit(
            workspace_id,
            "have2lemma",
            expected_revision=expected_revision,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def simplify_proofs(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        tactics: list[str] | None = None,
        timeout_ms: int | None = None,
        max_simplifications: int = 16,
        min_reduction_bytes: int = 1,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Replace theorem proofs with shorter ones that still check; the file is not changed.

        Without timeout_ms, each attempt gets its tactic's own budget.
        """
        payload: dict[str, Any] = {
            "file": file,
            "max_simplifications": max_simplifications,
            "min_reduction_bytes": min_reduction_bytes,
        }
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        if tactics is not None:
            payload["tactics"] = tactics
        return self.submit(
            workspace_id,
            "simplify_proofs",
            expected_revision=expected_revision,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def repair_proofs(
        self,
        workspace_id: str,
        file: str,
        *,
        expected_revision: int,
        tactics: list[str] | None = None,
        timeout_ms: int | None = None,
        max_repairs: int = 16,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Try to repair `sorry` holes within the limits. Leaves the local file unchanged.

        Without timeout_ms, each attempt gets its tactic's own budget.
        """
        payload: dict[str, Any] = {"file": file, "max_repairs": max_repairs}
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        if tactics is not None:
            payload["tactics"] = tactics
        return self.submit(
            workspace_id,
            "repair_proofs",
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
        operation's budget for its files (timeout_seconds less 31, at most 300).
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
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Check up to 64 inline proofs in the workspace's environment in one operation.

        Each code is ``{"custom_id": ..., "proof": ...}`` with a unique custom_id.
        Proofs may import the environment's packages, such as Mathlib, but not the
        workspace's own modules. timeout bounds each proof in seconds: 60 without
        one, and at most the operation's budget for its proofs (timeout_seconds
        less 31, at most 300).

        A code may add ``"target": {"context": ..., "statement": ..., "declaration":
        ...}``: the proof must then prove that statement, checked by the checker,
        and an accepted entry carries its ``receipt`` (strict only). Without one,
        ``ok`` means the source compiles, its declarations pass the kernel and it
        uses no extra axioms, not that it proves what you meant.
        """
        if not 1 <= len(codes) <= MAX_BATCH_PROOFS:
            raise UsageError(f"verify_batch takes 1 to {MAX_BATCH_PROOFS} proofs")
        return self.submit(
            workspace_id,
            "verify_batch",
            expected_revision=expected_revision,
            payload=batch_payload(codes, strict=strict, timeout=timeout),
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            deadline=deadline,
        )

    def verify_many(self, items: Iterable[Mapping[str, Any]], **options: Any) -> dict[str, Any]:
        """Verify many proofs against their statements, streamed to a JSONL file and
        resumable; see `leanwarp_cloud.bulk.verify_many` for the options."""
        from .bulk import verify_many

        return verify_many(self, items, **options)

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

    def stop(
        self,
        workspace_id: str,
        *,
        generation: int | None = None,
        require_idle: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """End billing for this generation. GET the workspace to follow cleanup.

        Pass the generation from a prior response to retry that close safely
        across restarts. An unqualified stop acts on the current generation. Set
        require_idle to check for active operations even when replaying a close.
        """
        path = f"workspaces/{_segment(workspace_id)}/stop"
        query: dict[str, str] = {}
        if generation is not None:
            if type(generation) is not int or not 0 <= generation < 2**63:
                raise UsageError("generation must be a nonnegative 64-bit integer")
            query["generation"] = str(generation)
        if require_idle:
            query["require_idle"] = "true"
        if query:
            path += "?" + urlencode(query)
        return self._request("POST", path, deadline=deadline, retry=generation is not None)

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
        raw: bytes | None = None,
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
        elif raw is not None:
            content = raw
            headers["Content-Type"] = "application/octet-stream"
        may_retry = (method == "GET" or idempotency_key is not None) if retry is None else retry
        retry_until = min(
            deadline if deadline is not None else time.monotonic() + self._retry_window,
            _CONNECT_DEADLINE.get(),
        )
        # A query can hold a page cursor; the log needs only the resource.
        resource = path.partition("?")[0]
        # Whether an earlier attempt may have reached the service: every one except
        # those that never connected.
        sent = False
        for attempt in itertools.count():
            if attempt and method != "GET":
                # The innermost check this client installed, whatever other clients
                # installed since.
                check = next(
                    (item for owner, item in reversed(_RESEND_CHECK.get()) if owner is self),
                    None,
                )
                if check is not None:
                    check()
            started = time.monotonic()
            failure: LeanWarpCloudError | LeanWarpTransportError
            try:
                timeout = _attempt_timeout(deadline, hold_seconds, default=self._timeout)
                io_deadline = deadline
                connect_deadline = _CONNECT_DEADLINE.get()
                if math.isfinite(connect_deadline) and (attempt or started < connect_deadline):
                    io_deadline = min(connect_deadline, deadline or math.inf)
                    if deadline is None:
                        timeout = httpx.Timeout(min(self._timeout, max(0.0, io_deadline - started)))
                response = self._send(method, path, content, headers, timeout, io_deadline)
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
                sent = sent or not isinstance(transport_error, _NOT_SENT)
            except LeanWarpCloudError as error:
                _log_attempt(method, resource, error.code, attempt, started)
                error.idempotency_key = idempotency_key
                error.resent = sent
                raise
            else:
                record_releases(response.headers, warn=self._interface == "python")
                _log_attempt(method, resource, response.status_code, attempt, started)
                if response.is_success:
                    try:
                        return _decoded(response, idempotency_key)
                    except LeanWarpCloudError as error:
                        error.resent = sent
                        raise
                failure = _refusal(response, idempotency_key)
                failure.resent = sent
                sent = True
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
                if time.monotonic() >= _CONNECT_DEADLINE.get():
                    raise ConnectTimeout() from failure
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
    # A sleep that ran late starts nothing past the window either.
    return time.monotonic() < until


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
    message = str(error.get("message", f"request failed with HTTP {response.status_code}"))
    # A project error names the logical file and package to fix.
    where = [
        f"{name} {error[name]}"
        for name in ("package", "file")
        if isinstance(error.get(name), str) and len(error[name]) <= 512
    ]
    paths = error.get("paths")
    return LeanWarpCloudError(
        response.status_code,
        str(error.get("code", "request_failed")),
        message + (f" ({', '.join(where)})" if where else ""),
        retry_after=_retry_after(response.headers.get("Retry-After", "")),
        idempotency_key=idempotency_key,
        paths=(
            tuple(paths)
            if isinstance(paths, list)
            and len(paths) <= 256
            and all(isinstance(item, str) and len(item) <= 512 for item in paths)
            else ()
        ),
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

"""Resumable statement-bound verification of many proofs, streamed to a JSONL file.

Invariants:

- An item is finished exactly when its line is in the output. A line is written
  and flushed before the operation that produced it is deleted, so a killed run
  never loses a verdict it was charged for.
- A chunk's idempotency key depends only on the run, the items in it (their IDs
  and content), its request and its attempt. The journal records a chunk's exact
  request before it is submitted, and the operation that answered it once
  submission returns. A resumed run reads that operation back, or sends the
  journaled request again with its key in the same workspace, from which the
  service answers with the stored operation and charges nothing. Such a replay
  starts within REPLAY_SECONDS (6 days) of the journal line; a send may overrun
  that margin by at most one attempt's connection and retry slack (seconds),
  which stays well inside the service's 7-day replay window. So the chunk never
  runs or is charged a second time.
  A chunk that can be neither, so its verdicts would be paid for again, needs
  ``rerun_lost``. Items an answered chunk left without a verdict (unchecked by
  its deadline, or the operation failed) go again as a new attempt, as they do
  in an uninterrupted run, at most CHUNK_ATTEMPTS times; an answer that is not
  one result per item is refused whole, and never resent. Only unfinished items
  go into new chunks, and a chunk that follows another is journaled before the
  earlier operation is deleted.
- A verdict is reused only for what produced it. Each line carries the
  ``input_sha256`` of the item's proof and target and the run's settings; a
  resumed run whose item or settings differ from a written line, or from a
  journaled chunk it would replay, is refused before anything is sent.
- A run verifies in one environment, which its first journaled proof chunk or
  written line names: each chunk and line names it. A resumed run in another,
  or with workspaces in another, is refused before anything is sent or skipped.
- One run at a time uses an output: the run's owner thread holds an exclusive
  lock on the journal from before either file is read until every lane it
  started has ended; its process's exit also releases it. Another run of that
  output, by any path to it, is refused at once, as is an output with other
  hard links. A caller that stops waiting stops the run: its lanes finish the
  request they are in and send nothing more, so the journal resumes it.
- A chunk's verify_batch payload, measured as the API measures it, fits
  MAX_BATCH_BYTES; an item that cannot fit alone is refused before anything is sent.
- The journal names every workspace the run asks for, before it asks. A run that
  ends with every journaled item's verdict written deletes them all, unless
  ``keep_workspaces``, even if a run before it wrote the last verdict or never
  got its creation's answer. A workspace the caller supplied is never deleted.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import functools
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import MAX_BATCH_BYTES, MAX_BATCH_PROOFS, batch_payload, payload_bytes
from .errors import LeanWarpCloudError, LeanWarpError, LeanWarpTransportError, UsageError
from .outcome import BATCH_COUNTERS

if TYPE_CHECKING:
    from .client import LeanWarpCloud

#: How many times a chunk whose operation failed outright is sent again.
CHUNK_ATTEMPTS = 3
# A verdict left undecided by the batch's own deadline: the item is sent again.
_UNFINISHED = frozenset({"not_checked"})
_STATUSES = frozenset().union(*BATCH_COUNTERS.values())


def target_sha256(target: Mapping[str, str]) -> str:
    """SHA-256 of the target's canonical JSON: the statement an item is bound to."""
    canonical = {
        "context": target.get("context", ""),
        "declaration": target["declaration"],
        "statement": target["statement"],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Item:
    id: str
    proof: str
    target: dict[str, str] | None
    digest: str

    @functools.cached_property
    def size(self) -> int:
        """Its entry's bytes in a verify_batch payload, as the API measures it."""
        return payload_bytes(self.code())

    def code(self) -> dict[str, Any]:
        code: dict[str, Any] = {"custom_id": self.id, "proof": self.proof}
        if self.target is not None:
            code["target"] = self.target
        return code


def _items(raw: Iterable[Mapping[str, Any]], *, require_target: bool, room: int) -> list[_Item]:
    items: list[_Item] = []
    seen: set[str] = set()
    for number, entry in enumerate(raw, start=1):
        proof = entry.get("proof")
        if not isinstance(proof, str) or not proof.strip():
            raise UsageError(f"item {number} needs a nonempty proof")
        target = entry.get("target")
        if target is not None:
            if (
                not isinstance(target, Mapping)
                or not isinstance(target.get("statement"), str)
                or not isinstance(target.get("declaration"), str)
                or not isinstance(target.get("context", ""), str)
            ):
                raise UsageError(
                    f'item {number}: target is {{"statement", "declaration", "context"?}}'
                )
            target = {
                key: str(target[key])
                for key in ("context", "statement", "declaration")
                if key in target
            }
        elif require_target:
            raise UsageError(
                f"item {number} has no target: a dataset run checks each proof against its "
                "statement; pass require_target=False to check that proofs only compile"
            )
        content = json.dumps(
            {"proof": proof, "target": target}, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        # A stable ID: the caller's, or one derived from the item's content.
        identifier = entry.get("id", digest[:32])
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise UsageError(f"item {number}: id must be a string of 1 to 128 characters")
        if identifier in seen:
            raise UsageError(f"item {number}: id {identifier!r} is not unique")
        seen.add(identifier)
        item = _Item(identifier, proof, target, digest)
        if item.size > room:
            raise UsageError(
                f"item {identifier!r} takes {item.size} bytes in a verify_batch request, "
                f"which leaves room for {room}"
            )
        items.append(item)
    return items


#: The JSON encoder the API measures with joins list entries with ", ".
_JOIN = len(", ")


def _empty_bytes(strict: bool, timeout: int | None) -> int:
    """Bytes of a verify_batch payload around its entries."""
    return payload_bytes(batch_payload([], strict=strict, timeout=timeout))


def _chunks(items: list[_Item], *, strict: bool, timeout: int | None) -> Iterator[list[_Item]]:
    """Consecutive chunks whose verify_batch payload fits MAX_BATCH_BYTES: its size is
    the empty payload's, each entry's, and the join between entries."""
    chunk: list[_Item] = []
    size = empty = _empty_bytes(strict, timeout)
    for item in items:
        if chunk and (len(chunk) == MAX_BATCH_PROOFS or size + _JOIN + item.size > MAX_BATCH_BYTES):
            yield chunk
            chunk, size = [], empty
        size += item.size + (_JOIN if chunk else 0)
        chunk.append(item)
    if chunk:
        yield chunk


@dataclass
class _Run:
    """The run's files: verdicts in ``output``, its plan in the journal beside it."""

    output: Path
    journal: Path = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Set once the caller stops waiting: no lane starts another request.
    stopped: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        # Any path to the output, a symlink included, names the same journal.
        self.output = self.output.resolve()
        self.journal = self.output.with_name(self.output.name + ".journal")

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold both files for this run alone, by a lock on the journal."""
        # Another name for the output would have its own journal and lock.
        with contextlib.suppress(FileNotFoundError):
            if self.output.stat().st_nlink > 1:
                raise UsageError(f"{self.output} has other hard links; write to a new output")
        descriptor = os.open(self.journal, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UsageError(f"{self.output} is in use by another run; let it finish") from None
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _lines(path: Path) -> list[dict[str, Any]]:
        """Complete lines; a line a kill cut short is dropped from the file."""
        if not path.exists():
            return []
        data = path.read_bytes()
        complete = data[: data.rfind(b"\n") + 1]
        if len(complete) != len(data):
            with path.open("r+b") as stream:
                stream.truncate(len(complete))
        return [json.loads(line) for line in complete.splitlines() if line.strip()]

    def finished(self) -> tuple[dict[str, str | None], str | None]:
        """Each written item's ID, with the ``input_sha256`` of what its verdict checked,
        and the environment the first line naming one names."""
        checked: dict[str, str | None] = {}
        environment: str | None = None
        for line in self._lines(self.output):
            checked[line["id"]] = line.get("input_sha256")
            environment = environment or line.get("environment_id")
        return checked, environment

    def plan(self) -> list[dict[str, Any]]:
        return self._lines(self.journal)

    def _append(self, path: Path, lines: list[dict[str, Any]]) -> None:
        with self.lock, path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record(self, entry: dict[str, Any]) -> None:
        self._append(self.journal, [entry])

    def write(self, lines: list[dict[str, Any]]) -> None:
        self._append(self.output, lines)


# A run journal's format: one whose chunks name their environment.
_JOURNAL_VERSION = 3


def _key(*parts: object) -> str:
    return "vm-" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:48]


#: The settings that decide a verdict besides its proof and target.
_SETTINGS = ("strict", "timeout", "timeout_seconds")
#: The fields of a chunk's request besides its proofs, as journaled, hashed and sent.
_REQUEST = (*_SETTINGS, "revision")


def _input_sha256(item: _Item, settings: list[Any]) -> str:
    """What a verdict checked: the item's proof and target, and the settings."""
    return hashlib.sha256(json.dumps([item.digest, settings]).encode()).hexdigest()


#: A journaled chunk whose submission's answer was never recorded is replayed by its
#: key only this long. It is a margin, not an exact bound on every send: a send may
#: overrun it by one attempt's connection and retry slack (seconds), well inside the
#: service's 7-day window, in which a replay is answered from the stored operation
#: and charges nothing.
REPLAY_SECONDS = 6 * 24 * 3600
# What _submit answers once a chunk's key is too old to send.
_TOO_OLD: dict[str, Any] = {}
# What _submit answers once the run is stopped: nothing was sent.
_STOPPED: dict[str, Any] = {}


@dataclass
class _Chunk:
    """One verify_batch request, and the items whose verdicts it writes.

    ``items`` is the whole request in order: its key covers them all. ``owned``
    are the ones this chunk answers for: unfinished items whose latest journal
    line it is. ``request`` and ``key`` are set once it is journaled, and
    ``operation_id`` once its submission was answered.
    """

    items: list[_Item]
    owned: set[str]
    attempt: int
    request: dict[str, Any] | None = None
    key: str | None = None
    operation_id: str | None = None
    # When it was journaled: replaying its key is safe only for REPLAY_SECONDS.
    journaled_at: float | None = None


def verify_many(
    client: LeanWarpCloud,
    items: Iterable[Mapping[str, Any]],
    *,
    output: str | Path,
    environment_id: str | None = None,
    workspace_ids: Iterable[str] = (),
    workspaces: int = 4,
    strict: bool = True,
    require_target: bool = True,
    timeout: int | None = None,
    timeout_seconds: int | None = None,
    keep_workspaces: bool = False,
    rerun_lost: bool = False,
) -> dict[str, Any]:
    """Verify every item, writing one JSON line per verdict to ``output`` as it arrives.

    Each item is ``{"id", "proof", "target": {"statement", "declaration",
    "context"}}``; ``id`` defaults to a hash of the item's content. Every item
    needs a target unless ``require_target`` is False. Items go out in chunks of up
    to 64, one at a time per workspace, over ``workspace_ids`` or ``workspaces``
    new workspaces in ``environment_id``, which are deleted at the end unless
    ``keep_workspaces``. Run it again with the same ``output`` to resume: items
    already written are skipped and an interrupted chunk is read back, or replayed
    exactly as it was sent, never charged again. A chunk that cannot be (its result
    was removed, its workspace is not in this run, its items changed, or it is too
    old to replay safely) runs again, and is charged again, only with
    ``rerun_lost``. A resumed run must give each written item the same proof and
    target, and the same ``strict``, ``timeout`` and ``timeout_seconds``; otherwise
    it is refused, and changed items go to a new ``output``. An output is verified in
    one environment: once its first proof chunk is journaled or a verdict written,
    a resumed run with another ``environment_id``, or ``workspace_ids`` in another,
    is refused, and so are ``workspace_ids`` in different environments or in none.
    One run at a time uses an output; another run of it meanwhile, by any path to
    it, is refused at once.

    Each line has ``id``, ``status``, ``passed``, ``decided``, ``environment_id``,
    ``target_sha256`` (the statement it was checked against), ``input_sha256`` (its
    proof, target and settings), ``operation_id`` and the batch entry under
    ``result``. ``decided`` is false for ``timeout``, ``infrastructure_failure`` and
    ``not_checked``: no verdict was reached, and ``passed`` is false. Such a line
    is written, so a resumed run skips it; to try those items again, write them
    to a new output. Returns the counts: ``passed``, ``failed`` (decided failures),
    ``undecided``, ``written`` and ``skipped``.
    """
    room = MAX_BATCH_BYTES - _empty_bytes(strict, timeout)
    # Read here, on the caller's thread, which an iterable such as a database
    # cursor may be bound to.
    pending = _items(items, require_target=require_target, room=room)
    run = _Run(Path(output))
    outcome: list[dict[str, Any] | BaseException] = []

    def own() -> None:
        try:
            # A caller that already stopped waiting starts nothing.
            if run.stopped.is_set():
                return
            with run.exclusive():
                if run.stopped.is_set():
                    return
                result = _verify(
                    client,
                    pending,
                    run,
                    environment_id=environment_id,
                    workspace_ids=workspace_ids,
                    workspaces=workspaces,
                    strict=strict,
                    timeout=timeout,
                    timeout_seconds=timeout_seconds,
                    keep_workspaces=keep_workspaces,
                    rerun_lost=rerun_lost,
                )
        except BaseException as error:
            outcome.append(error)
        else:
            # Only once the lock is let go without an error.
            outcome.append(result)

    # The run's one owner holds the output's lock until every lane it started has
    # ended, also after this caller stops waiting. It runs in the caller's context.
    owner = threading.Thread(target=contextvars.copy_context().run, args=(own,), daemon=True)
    try:
        owner.start()
        owner.join()
    except BaseException:
        run.stopped.set()
        raise
    (result,) = outcome
    if isinstance(result, BaseException):
        raise result
    return result


def _verify(
    client: LeanWarpCloud,
    pending: list[_Item],
    run: _Run,
    *,
    environment_id: str | None,
    workspace_ids: Iterable[str],
    workspaces: int,
    strict: bool,
    timeout: int | None,
    timeout_seconds: int | None,
    keep_workspaces: bool,
    rerun_lost: bool,
) -> dict[str, Any]:
    """`verify_many`, under its run's lock."""
    settings = [strict, timeout, timeout_seconds]
    by_id = {item.id: item for item in pending}
    plan = run.plan()
    header = next((line for line in plan if line.get("type") == "run"), None)
    if header is not None and header.get("version") != _JOURNAL_VERSION:
        # Written in an earlier SDK's format: never resumed, but named.
        # Each creation it journaled, by its index and build: answered with its
        # workspace, or still open unless its latest request was refused.
        made: dict[tuple[object, object], str] = {}
        creations: dict[tuple[object, object], bool] = {}
        for line in plan:
            if line.get("type") == "workspace":
                slot = (line.get("index"), line.get("bundle_id", line.get("environment_id")))
                if line.get("workspace_id"):
                    made[slot] = str(line["workspace_id"])
                else:
                    creations[slot] = not line.get("refused")
        cleanup = "".join(
            f"; `leanwarp delete --confirm {item}`" for item in sorted(set(made.values()))
        )
        raise UsageError(
            f"{run.output} was written by an earlier SDK and cannot be resumed; "
            "write to a new output"
            + (f". Delete the workspaces it created{cleanup}" if made else "")
            + (
                ". It may have created others: `leanwarp workspaces` lists them"
                if any(waiting and slot not in made for slot, waiting in creations.items())
                else ""
            )
        )
    if header is None:
        header = {"type": "run", "run_id": uuid.uuid4().hex, "version": _JOURNAL_VERSION}
        run.record(header)
    run_id = header["run_id"]
    finished, written_in = run.finished()
    skipped = sum(item.id in finished for item in pending)
    unfinished = [item for item in pending if item.id not in finished]
    # Each unfinished item belongs to the latest journaled chunk that holds it.
    owner: dict[str, int] = {}
    answered: dict[str, str] = {}
    for index, line in enumerate(plan):
        if line.get("type") == "chunk":
            for identifier in line["ids"]:
                if identifier in by_id and identifier not in finished:
                    owner[identifier] = index
        elif line.get("type") == "submitted":
            answered[line["key"]] = line["operation_id"]
    # A written verdict stands only for the proof, target and settings it checked,
    # and a journaled chunk would be replayed with the settings it was sent with.
    changed = [
        item.id
        for item in pending
        if item.id in finished and finished[item.id] != _input_sha256(item, settings)
    ]
    for identifier, index in owner.items():
        request = plan[index].get("request")
        # Compared as encoded, as input_sha256 is: 1 and True are different settings.
        sent = [request.get(name) for name in _SETTINGS] if isinstance(request, dict) else None
        if sent is not None and json.dumps(sent) != json.dumps(settings):
            changed.append(identifier)
    if changed:
        raise UsageError(
            f"{len(changed)} items, such as {changed[0]!r}, have a verdict in {run.output} "
            "or were sent with another proof, target or settings (strict, timeout, "
            "timeout_seconds) than this run gives them; resume with the same ones, or "
            "write the changed items to a new output"
        )

    asked = {
        (line["index"], line["environment_id"]) for line in plan if line.get("type") == "workspace"
    }

    def create(index: int, environment: str) -> str:
        """The run's workspace ``index`` in ``environment``. Its key is the same on every
        run of this output, so a resumed run gets the same workspace; it is journaled
        before it is requested, so whichever run writes the last verdict finds and
        deletes it."""
        slot = {"type": "workspace", "index": index, "environment_id": environment}
        first = (index, environment) not in asked
        asked.add((index, environment))
        run.record(slot)
        try:
            workspace = client.create_workspace(
                environment, idempotency_key=_key(run_id, "workspace", index, environment)
            )
        except LeanWarpCloudError as error:
            # Refused on its very first attempt (an invalid request, or an environment
            # that is not ready), that key made no workspace. A replay's refusal shows
            # nothing, whether this run's automatic resend or a later run's: a lost
            # creation may still stand behind it.
            if (
                first
                and not error.resent
                and (error.status == 422 or error.code == "environment_not_ready")
            ):
                run.record({**slot, "refused": True})
            raise
        run.record({**slot, "workspace_id": workspace["workspace_id"]})
        return str(workspace["workspace_id"])

    supplied = [str(identifier) for identifier in workspace_ids]
    # The run's one environment: the caller's workspaces must share it, and once a
    # chunk is journaled or a line written, every run of this output must use the
    # one they name.
    environment = environment_id
    if supplied:
        found: set[str] = set()
        for identifier in supplied:
            named = client.workspace(identifier).get("environment_id")
            if not isinstance(named, str) or not named:
                raise UsageError(
                    f"workspace {identifier} is in no environment; use workspaces made in "
                    "one, or name an environment_id"
                )
            found.add(named)
        if len(found) > 1:
            raise UsageError("workspace_ids are in different environments; a run uses one")
        (environment,) = found
        if environment_id is not None and environment != environment_id:
            raise UsageError(
                f"workspace_ids are in environment {environment}, not {environment_id}"
            )
    bound = next(
        (line["environment_id"] for line in plan if line.get("type") == "chunk"), written_in
    )
    if bound is not None and environment is not None and environment != bound:
        raise UsageError(
            f"{run.output} is verified in environment {bound}, not {environment}; "
            "write to a new output to verify in another"
        )
    lanes = list(supplied)
    if not lanes and unfinished:
        if environment_id is None:
            raise UsageError("name an environment_id, or workspace_ids to use")
        if not 1 <= workspaces <= 64:
            raise UsageError("workspaces must be between 1 and 64")
        lanes = [create(index, environment_id) for index in range(workspaces)]

    def chunk_key(chunk: list[_Item], request: Mapping[str, Any], attempt: int) -> str:
        return _key(
            run_id,
            [[item.id, item.digest] for item in chunk],
            [request[name] for name in _REQUEST],
            attempt,
        )

    def again(owned: Iterable[_Item], attempt: int) -> list[_Chunk]:
        """New chunks of the given unfinished items: a new attempt, charged anew."""
        parts = _chunks(list(owned), strict=strict, timeout=timeout)
        return [_Chunk(part, {item.id for item in part}, attempt) for part in parts]

    replays: dict[str, queue.Queue[_Chunk]] = {lane: queue.Queue() for lane in lanes}
    fresh: queue.Queue[_Chunk] = queue.Queue()
    lost: list[_Chunk] = []
    for index in sorted(set(owner.values())):
        line = plan[index]
        owned_ids = {identifier for identifier, at in owner.items() if at == index}
        owned = [by_id[identifier] for identifier in line["ids"] if identifier in owned_ids]
        known = [by_id[identifier] for identifier in line["ids"] if identifier in by_id]
        request = line.get("request")
        key = str(line.get("key"))
        replayable = (
            isinstance(request, dict)
            and sorted(request) == sorted(_REQUEST)
            and len(known) == len(line["ids"])
            and key == chunk_key(known, request, line["attempt"])
            and line["workspace_id"] in replays
            and (key in answered or time.time() - line.get("at", 0) < REPLAY_SECONDS)
        )
        if replayable:
            replays[line["workspace_id"]].put(
                _Chunk(
                    known,
                    owned_ids,
                    line["attempt"],
                    request,
                    key,
                    answered.get(key),
                    line.get("at", 0),
                )
            )
        else:
            lost += again(owned, line["attempt"] + 1)
    if lost and not rerun_lost:
        raise UsageError(
            f"{sum(len(chunk.owned) for chunk in lost)} items were sent in chunks this run "
            "cannot read back or replay safely (their workspace is not in it, their items "
            "changed, or they are too old); pass rerun_lost=True to send them again, which "
            "is charged again"
        )
    for chunk in lost:
        fresh.put(chunk)
    for chunk in again((item for item in unfinished if item.id not in owner), 0):
        fresh.put(chunk)

    counts = {"passed": 0, "failed": 0, "undecided": 0, "written": 0}
    counting = threading.Lock()
    failures: list[BaseException] = []

    def lane(workspace_id: str) -> None:
        try:
            revision = client.workspace(workspace_id)["revision"]
            while not failures and not run.stopped.is_set():
                try:
                    chunk = replays[workspace_id].get_nowait()
                except queue.Empty:
                    try:
                        chunk = fresh.get_nowait()
                    except queue.Empty:
                        return
                _send(chunk, workspace_id, revision)
        except BaseException as error:
            failures.append(error)

    def _line(item: _Item, entry: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
        status = entry.get("status")
        passed = status in BATCH_COUNTERS["passed"]
        # A timeout, an infrastructure failure or a final not_checked: no verdict.
        decided = passed or status in BATCH_COUNTERS["failed"]
        with counting:
            counts["passed" if passed else "failed" if decided else "undecided"] += 1
        return {
            "id": item.id,
            "status": status,
            "passed": passed,
            "decided": decided,
            "environment_id": environment,
            "target_sha256": target_sha256(item.target) if item.target else None,
            # Every chunk this run sends, replays included, has the run's settings.
            "input_sha256": _input_sha256(item, settings),
            "operation_id": operation_id,
            "result": dict(entry),
        }

    def _journal(chunk: _Chunk, workspace_id: str, revision: int) -> None:
        """Record the exact request before it is first sent."""
        chunk.request = dict(
            zip(_REQUEST, (strict, timeout, timeout_seconds, revision), strict=True)
        )
        chunk.key = chunk_key(chunk.items, chunk.request, chunk.attempt)
        chunk.journaled_at = time.time()
        run.record(
            {
                "type": "chunk",
                "ids": [item.id for item in chunk.items],
                "workspace_id": workspace_id,
                "environment_id": environment,
                "attempt": chunk.attempt,
                "key": chunk.key,
                "request": chunk.request,
                "at": chunk.journaled_at,
            }
        )

    def _lost(chunk: _Chunk, why: str) -> list[_Chunk]:
        if not rerun_lost:
            raise LeanWarpCloudError(
                409,
                "operation_expired",
                f"the verdicts of {len(chunk.owned)} items {why}; pass rerun_lost=True to "
                "send them again, which is charged again",
            )
        return again((item for item in chunk.items if item.id in chunk.owned), chunk.attempt + 1)

    def _send(first: _Chunk, workspace_id: str, revision: int) -> None:
        work = [first]
        while work:
            chunk = work.pop(0)
            # Stopped: nothing more is sent or waited for; the journal resumes the rest.
            if run.stopped.is_set():
                return
            if chunk.request is None:
                _journal(chunk, workspace_id, revision)
            if chunk.operation_id is None:
                submitted = _submit(chunk, workspace_id)
                if submitted is _STOPPED:
                    return
                if submitted is None:
                    work += _lost(chunk, "were removed before they were read")
                    continue
                if submitted is _TOO_OLD:
                    work += _lost(chunk, "can no longer be replayed safely")
                    continue
                chunk.operation_id = submitted["operation_id"]
                run.record(
                    {"type": "submitted", "key": chunk.key, "operation_id": chunk.operation_id}
                )
            try:
                operation = client.wait(chunk.operation_id, timeout=6 * 3600)
            except LeanWarpCloudError as error:
                if error.code != "not_found":
                    raise
                work += _lost(chunk, "were removed before they were read")
                continue
            owned = [item for item in chunk.items if item.id in chunk.owned]
            last = chunk.attempt + 1 >= CHUNK_ATTEMPTS
            if operation["state"] != "completed":
                if last:
                    raise LeanWarpCloudError(
                        502,
                        operation.get("error_code") or "chunk_failed",
                        f"a chunk failed {CHUNK_ATTEMPTS} times: {operation.get('error_code')}",
                    )
                work += again(owned, chunk.attempt + 1)
                continue
            answer = operation.get("result")
            result = answer.get("result") if isinstance(answer, dict) else None
            entries = result.get("results") if isinstance(result, dict) else None
            if not isinstance(entries, list):
                entries = []
            by_custom = {
                entry["custom_id"]: entry
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("custom_id"), str)
            }
            # The service answers each item once, with a batch status. Any other
            # answer is refused whole, kept and never resent, so reading it back on
            # resume charges nothing.
            if (
                len(entries) != len(chunk.items)
                or by_custom.keys() != {item.id for item in chunk.items}
                or not all(
                    isinstance(entry.get("status"), str) and entry["status"] in _STATUSES
                    for entry in by_custom.values()
                )
            ):
                raise LeanWarpCloudError(
                    502,
                    "invalid_response",
                    f"operation {chunk.operation_id} did not answer one result per item of "
                    "its chunk, each with a known status; nothing of it was written",
                )
            lines, left = [], []
            for item in owned:
                entry = by_custom[item.id]
                if last or entry.get("status") not in _UNFINISHED:
                    lines.append(_line(item, entry, chunk.operation_id))
                else:
                    left.append(item)
            run.write(lines)
            with counting:
                counts["written"] += len(lines)
            # Entries the batch's deadline left unchecked go again as a new chunk,
            # journaled before the operation that answered for them is deleted.
            follow = again(left, chunk.attempt + 1)
            for part in follow:
                _journal(part, workspace_id, revision)
            # Written first: deleting frees the account's history, never a verdict.
            with contextlib.suppress(LeanWarpError):
                client.delete_operation(chunk.operation_id)
            work += follow

    def _submit(chunk: _Chunk, workspace_id: str) -> dict[str, Any] | None:
        request = chunk.request or {}
        while True:
            # No replay starts past the margin. Near it, the cutoff is the client's
            # deadline for its own retries; a send can still overrun it by one
            # attempt's connection and retry slack, which the day between this
            # margin and the service's 7-day window absorbs.
            # Sampled together, before the request is built, so no pause extends it.
            anchor = time.monotonic()
            remaining = (chunk.journaled_at or 0) + REPLAY_SECONDS - time.time()
            if remaining <= 0:
                return _TOO_OLD
            # The client resends within its own retry window (resend_horizon, up to
            # its connection slack); a cutoff past that window leaves its timeouts
            # and retries as they are.
            deadline = None if remaining > client.resend_horizon else anchor + remaining
            # The last check before a proof is sent, after its journaling.
            if run.stopped.is_set():
                return _STOPPED
            try:
                return client.verify_batch(
                    workspace_id,
                    [item.code() for item in chunk.items],
                    expected_revision=request["revision"],
                    strict=request["strict"],
                    timeout=request["timeout"],
                    timeout_seconds=request["timeout_seconds"],
                    idempotency_key=chunk.key,
                    deadline=deadline,
                )
            except LeanWarpTransportError:
                if (chunk.journaled_at or 0) + REPLAY_SECONDS <= time.time():
                    return _TOO_OLD
                raise
            except LeanWarpCloudError as error:
                if error.code == "operation_expired":
                    return None
                # History or capacity frees with time; the same key keeps it one request.
                if not error.retryable and error.code != "workspace_busy":
                    raise
                run.stopped.wait(min(60.0, error.retry_after or 2.0))

    threads = [threading.Thread(target=lane, args=(lane_id,), daemon=True) for lane_id in lanes]
    started: list[threading.Thread] = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except BaseException:
        run.stopped.set()
        raise
    finally:
        # The output stays locked until every lane that started has ended.
        for thread in started:
            thread.join()
    if failures:
        raise failures[0]
    if run.stopped.is_set():
        raise LeanWarpError("the run was stopped; run verify_many again to resume it")
    # Every item must have its verdict before the run reports success or deletes
    # the workspaces that hold any result not yet read.
    written, _ = run.finished()
    missing = len(by_id.keys() - written.keys())
    if missing:
        raise LeanWarpError(f"{missing} items have no verdict yet; run verify_many again")
    journal = run.plan()
    # A journaled item left out of this run may have an unread result in them.
    unread = any(
        identifier not in written
        for line in journal
        if line.get("type") == "chunk"
        for identifier in line["ids"]
    )
    if not keep_workspaces and not unread:
        # Each journaled workspace, with its ID once its creation was answered, and
        # those whose latest request was refused when first asked for.
        requested: dict[tuple[int, str], str | None] = {}
        refused: set[tuple[int, str]] = set()
        for line in journal:
            if line.get("type") != "workspace":
                continue
            slot = (line["index"], line["environment_id"])
            if requested.get(slot):
                continue
            requested[slot] = line.get("workspace_id")
            if line.get("refused"):
                refused.add(slot)
            else:
                refused.discard(slot)
        for slot, answer in requested.items():
            if answer is None and slot in refused:
                continue
            # One whose creation went unanswered is asked for again by its key.
            workspace_id = answer or create(*slot)
            if workspace_id in supplied:
                continue
            try:
                client.delete(workspace_id)
            except LeanWarpCloudError as error:
                # Deleted by a run stopped while cleaning up: that part is done.
                if error.code != "not_found":
                    raise
    return {
        "items": len(pending),
        "skipped": skipped,
        **counts,
        "output": str(run.output),
    }

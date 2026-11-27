from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from leanwarp_cloud import (
    AsyncLeanWarpCloud,
    LeanWarpCloud,
    LeanWarpCloudError,
    LeanWarpError,
    UsageError,
    target_sha256,
)
from leanwarp_cloud.bulk import _Run


class Killed(Exception):
    """The process stopping between a chunk's submission and its result."""


class Service:
    """Workspaces and verify_batch with per-workspace idempotency, as the API keeps them."""

    def __init__(self) -> None:
        self.workspaces: dict[str, str] = {}
        self.operations: dict[str, dict] = {}
        self.keys: dict[tuple[str, str], str] = {}
        self.charged: list[list[str]] = []
        self.kill_on_read: set[int] = set()
        self.reads = 0
        self.unchecked_once: set[str] = set()
        # Items whose every check ends with this status.
        self.statuses: dict[str, str] = {}
        self.unreadable = False
        # Operations history removed: reads answer not_found, replays operation_expired.
        self.removed: set[str] = set()
        self.kill_on_delete = False
        self.busy_posts = 0
        self.posts = 0
        self.deleted: list[str] = []
        # Workspaces created whose answer never reaches the client.
        self.unanswered: set[str] = set()
        self.environments: dict[str, str] = {}
        self.deny_creates = False
        # Environments the service does not serve: refused before any key is looked up.
        self.retired = {"refused"}
        # Retire an environment as soon as a creation in it loses its answer.
        self.retire_on_loss = False
        # Rewrites a completed operation's result, as a malformed answer would.
        self.answer = lambda result: result
        self.deleted_operations: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/leanwarp/")
        key = request.headers.get("Idempotency-Key", "")
        if request.method == "POST" and path == "workspaces":
            if self.deny_creates:
                return httpx.Response(403, json={"error": {"code": "leanwarp_key_scope_denied"}})
            environment = json.loads(request.content)["environment_id"]
            if environment in self.retired:
                # As the API refuses a workspace pinned to an environment not ready.
                return httpx.Response(409, json={"error": {"code": "environment_not_ready"}})
            if self.environments.setdefault(key, environment) != environment:
                return httpx.Response(422, json={"error": {"code": "idempotency_conflict"}})
            workspace_id = self.workspaces.setdefault(key, f"w{len(self.workspaces)}")
            if workspace_id in self.unanswered:
                self.unanswered.discard(workspace_id)
                if self.retire_on_loss:
                    self.retired.add(environment)
                raise httpx.ReadError("lost", request=request)
            return httpx.Response(201, json={"workspace_id": workspace_id, "revision": 0})
        if match := re.fullmatch(r"workspaces/(w\d+)", path):
            if self.unreadable:
                raise httpx.ConnectError("unreachable", request=request)
            if request.method == "DELETE":
                if match[1] in self.deleted:
                    return httpx.Response(404, json={"error": {"code": "not_found"}})
                self.deleted.append(match[1])
                return httpx.Response(204)
            # A workspace this service did not create is in b, except w9, from before
            # environments.
            environment = next(
                (self.environments[k] for k, w in self.workspaces.items() if w == match[1]),
                None if match[1] == "w9" else "b",
            )
            return httpx.Response(
                200, json={"workspace_id": match[1], "revision": 0, "environment_id": environment}
            )
        if match := re.fullmatch(r"workspaces/(w\d+)/operations", path):
            self.posts += 1
            if self.busy_posts:
                self.busy_posts -= 1
                return httpx.Response(
                    503, headers={"Retry-After": "1"}, json={"error": {"code": "busy"}}
                )
            existing = self.keys.get((match[1], key))
            if existing in self.removed:
                return httpx.Response(409, json={"error": {"code": "operation_expired"}})
            if existing is None:
                body = json.loads(request.content)
                codes = body["payload"]["codes"]
                self.charged.append([code["custom_id"] for code in codes])
                existing = self.keys[(match[1], key)] = f"op{len(self.operations)}"
                results = []
                for code in codes:
                    status = "ok" if "good" in code["proof"] else "rejected"
                    if code["custom_id"] in self.unchecked_once:
                        self.unchecked_once.discard(code["custom_id"])
                        status = "not_checked"
                    status = self.statuses.get(code["custom_id"], status)
                    results.append({"custom_id": code["custom_id"], "status": status})
                self.operations[existing] = {
                    "operation_id": existing,
                    "kind": "verify_batch",
                    "state": "completed",
                    "result": self.answer({"result": {"status": "ok", "results": results}}),
                }
            return httpx.Response(202, json={**self.operations[existing], "state": "queued"})
        if match := re.fullmatch(r"operations/(op\d+)", path):
            if request.method == "DELETE":
                if self.kill_on_delete:
                    self.kill_on_delete = False
                    raise Killed
                self.deleted_operations.append(match[1])
                return httpx.Response(204)
            if match[1] in self.removed:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            self.reads += 1
            if self.reads in self.kill_on_read:
                raise Killed
            return httpx.Response(200, json=self.operations[match[1]])
        raise AssertionError(f"unexpected {request.method} {path}")


def items(count: int) -> list[dict]:
    return [
        {
            "id": f"p{index}",
            "proof": f"theorem t{index} : True := {'good' if index % 3 else 'bad'}",
            "target": {"statement": "True", "declaration": f"t{index}"},
        }
        for index in range(count)
    ]


def test_a_killed_run_resumes_sending_only_unfinished_items_and_never_charges_twice(
    tmp_path: Path,
) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    service.kill_on_read = {2}
    # One entry the batch's deadline leaves unchecked: it is sent again on its own.
    service.unchecked_once = {"p70"}
    dataset = items(200)
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        with pytest.raises(Killed):
            api.verify_many(dataset, output=output, environment_id="b", workspaces=1)
        first = [json.loads(line) for line in output.read_text().splitlines()]
        assert len(first) == 64
        summary = api.verify_many(dataset, output=output, environment_id="b", workspaces=1)
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert sorted(line["id"] for line in lines) == sorted(item["id"] for item in dataset)
    assert summary["skipped"] == 64 and summary["written"] == 136
    # The chunk in flight at the kill was replayed under its key, not charged again.
    charged = [identifier for chunk in service.charged for identifier in chunk]
    assert sorted(charged) == sorted([*(item["id"] for item in dataset), "p70"])
    assert ["p70"] in service.charged
    target = target_sha256({"statement": "True", "declaration": "t5"})
    assert {line["id"]: line["target_sha256"] for line in lines}["p5"] == target
    assert {line["id"]: line["passed"] for line in lines}["p3"] is False


def test_dataset_runs_need_a_statement_unless_told_otherwise(tmp_path: Path) -> None:
    unbound = [{"id": "a", "proof": "theorem a : True := good"}]
    service = Service()
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        with pytest.raises(UsageError, match="no target"):
            api.verify_many(unbound, output=tmp_path / "a.jsonl", environment_id="b")
        assert service.charged == []
        caller = threading.current_thread()

        def cursor() -> Iterator[dict]:
            # As a database cursor is: readable only on the thread that opened it.
            if threading.current_thread() is not caller:
                raise RuntimeError("read on another thread")
            yield from unbound

        api.verify_many(
            cursor(), output=tmp_path / "b.jsonl", environment_id="b", require_target=False
        )
        client = AsyncLeanWarpCloud(client=api)

        async def together() -> list[dict]:
            return await asyncio.gather(*(client.workspace(f"w{index}") for index in range(3)))

        assert [workspace["workspace_id"] for workspace in asyncio.run(together())] == [
            "w0",
            "w1",
            "w2",
        ]

    # A cancelled call keeps its place until its thread ends.
    gate = threading.Event()
    calls: list[str] = []

    def held(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        gate.wait(5)
        return httpx.Response(200, json={"workspace_id": "w0", "revision": 0})

    async def cancelled_then_next() -> int:
        client = AsyncLeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(held),
            concurrency=1,
        )
        first = asyncio.create_task(client.workspace("w0"))
        await asyncio.sleep(0.1)
        first.cancel()
        second = asyncio.create_task(client.workspace("w1"))
        await asyncio.sleep(0.2)
        started = len(calls)
        # Closing, even a close that is cancelled, refuses new calls and never
        # frees a running call's place.
        queued = asyncio.create_task(client.workspace("w3"))
        await asyncio.sleep(0.05)
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0.05)
        closing.cancel()
        with pytest.raises(UsageError):
            await client.workspace("w2")
        gate.set()
        # Calls still waiting for a slot when close began are refused, and close
        # returns only once nothing runs.
        for waiting in (second, queued):
            with pytest.raises(UsageError):
                await waiting
        await client.close()
        return started

    assert asyncio.run(cancelled_then_next()) == 1
    (line,) = (tmp_path / "b.jsonl").read_text().splitlines()
    assert json.loads(line)["target_sha256"] is None


def test_a_chunk_cut_off_while_writing_is_replayed_exactly_as_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    write = _Run.write

    def first_line_then_kill(self: _Run, lines: list[dict]) -> None:
        write(self, lines[:1])
        raise Killed

    dataset = items(3)
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        with monkeypatch.context() as patch:
            patch.setattr(_Run, "write", first_line_then_kill)
            with pytest.raises(Killed):
                api.verify_many(dataset, output=output, environment_id="b", workspaces=1)
        # Other options on the resumed run: refused, as the verdicts were made without them.
        with pytest.raises(UsageError, match="settings"):
            api.verify_many(dataset, output=output, environment_id="b", workspaces=1, timeout=30)
        api.verify_many(dataset, output=output, environment_id="b", workspaces=1)
        assert service.charged == [["p0", "p1", "p2"]]
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        assert sorted(line["id"] for line in lines) == ["p0", "p1", "p2"]
    # A workspace that cannot be read fails the run instead of reporting success.
    service.unreadable = True
    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            retries=0,
            transport=httpx.MockTransport(service.handle),
        ) as api,
        pytest.raises(LeanWarpError),
    ):
        api.verify_many(items(4), output=tmp_path / "other.jsonl", environment_id="b")


def test_lost_and_followed_chunks_send_only_unfinished_items(tmp_path: Path) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        # Stopped after the unchecked entry's follow-up is journaled, before its
        # predecessor is deleted: the resumed run sends the follow-up, not a lost chunk.
        service.unchecked_once, service.kill_on_delete = {"p1"}, True
        with pytest.raises(Killed):
            api.verify_many(items(3), output=output, environment_id="b", workspaces=1)
        service.removed.add("op0")
        api.verify_many(items(3), output=output, environment_id="b", workspaces=1)
        assert service.charged == [["p0", "p1", "p2"], ["p1"]]
        # A chunk whose result history removed before every verdict was written is
        # sent again only on request, and then only for its unwritten items.
        other = tmp_path / "other.jsonl"
        service.kill_on_read = {service.reads + 1}
        with pytest.raises(Killed):
            api.verify_many(items(5)[3:], output=other, environment_id="b", workspaces=1)
        service.removed.add(f"op{len(service.operations) - 1}")
        with pytest.raises(LeanWarpError, match="rerun_lost"):
            api.verify_many(items(5)[3:], output=other, environment_id="b", workspaces=1)
        api.verify_many(
            items(5)[3:], output=other, environment_id="b", workspaces=1, rerun_lost=True
        )
        assert service.charged[-2:] == [["p3", "p4"], ["p3", "p4"]]
        # A key older than the service is sure to remember is never sent again,
        # retries included.
        charged = len(service.charged)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("leanwarp_cloud.bulk.REPLAY_SECONDS", 0)
            with pytest.raises(LeanWarpError, match="rerun_lost"):
                api.verify_many(items(7)[5:], output=tmp_path / "old.jsonl", environment_id="b")
        assert len(service.charged) == charged
        # Nor resent by the client's own retries once the window ends meanwhile.
        posts, service.busy_posts = service.posts, 5
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("leanwarp_cloud.bulk.REPLAY_SECONDS", 0.5)
            with pytest.raises(LeanWarpError, match="rerun_lost"):
                api.verify_many(items(9)[7:], output=tmp_path / "late.jsonl", environment_id="b")
        assert service.posts == posts + 1 and len(service.charged) == charged
        # Far from the cutoff, a submission keeps the client's own timeouts and retries.
        deadlines: list[float | None] = []
        submit = api.verify_batch

        def recorded(*args: object, **kwargs: object) -> dict:
            deadlines.append(kwargs.get("deadline"))  # type: ignore[arg-type]
            return submit(*args, **kwargs)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(api, "verify_batch", recorded)
            api.verify_many(items(10)[9:], output=tmp_path / "soon.jsonl", environment_id="b")
        assert deadlines == [None]


def test_a_verdict_is_reused_only_for_the_proof_target_settings_and_environment_it_had(
    tmp_path: Path,
) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    dataset = items(3)
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        api.verify_many(dataset, output=output, environment_id="b", workspaces=1)
        written = output.read_text()
        assert json.loads(written.splitlines()[1])["passed"] is True
        # p1 passed; the same ID now holds another proof of another statement.
        changed = [*dataset[:1], {**dataset[1], "proof": "theorem t : False := bad"}, dataset[2]]
        changed_target = [
            *dataset[:1],
            {**dataset[1], "target": {"statement": "False", "declaration": "t1"}},
            dataset[2],
        ]
        for again, options in (
            (changed, {}),
            (changed_target, {}),
            (dataset, {"timeout": 30}),
            (dataset, {"timeout_seconds": 300}),
        ):
            with pytest.raises(UsageError, match="new output"):
                api.verify_many(again, output=output, environment_id="b", workspaces=1, **options)
        assert {json.loads(line)["environment_id"] for line in written.splitlines()} == {"b"}
        # Nor in another environment, named or of the caller's workspaces, nor a mix of them.
        other = api.create_workspace("c")["workspace_id"]
        for options, refusal in (
            ({"environment_id": "c"}, "new output"),
            ({"workspace_ids": [other]}, "new output"),
            ({"workspace_ids": [other], "environment_id": "b"}, "not b"),
            ({"workspace_ids": ["w7", other]}, "different environments"),
            ({"workspace_ids": ["w9"]}, "w9 is in no environment"),
        ):
            with pytest.raises(UsageError, match=refusal):
                api.verify_many(dataset, output=output, **options)
        # Written lines bind it too, without the journal.
        output.with_name(output.name + ".journal").unlink()
        with pytest.raises(UsageError, match="new output"):
            api.verify_many(dataset, output=output, environment_id="c")
        assert output.read_text() == written and len(service.charged) == 1
        assert len(service.workspaces) == 2
        assert (
            api.verify_many(dataset, output=output, environment_id="b", workspaces=1)["skipped"]
            == 3
        )


def test_a_check_without_a_verdict_is_written_undecided_and_a_missing_one_stops_the_run(
    tmp_path: Path,
) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    # p0's proof is wrong; no check of the others reaches a verdict.
    service.statuses = {"p1": "timeout", "p2": "infrastructure_failure", "p3": "not_checked"}
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(service.handle)
    ) as api:
        summary = api.verify_many(items(4), output=output, environment_id="b", workspaces=1)
        assert (summary["passed"], summary["failed"], summary["undecided"]) == (0, 1, 3)
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        assert {line["id"]: (line["passed"], line["decided"]) for line in lines} == {
            "p0": (False, True),
            "p1": (False, False),
            "p2": (False, False),
            "p3": (False, False),
        }
        # Written once its attempts are spent, and skipped on resume: nothing more is spent.
        assert api.verify_many(items(4), output=output, environment_id="b")["skipped"] == 4
    assert service.charged == [["p0", "p1", "p2", "p3"], ["p3"], ["p3"]]

    # An answer that is not one result per item (one missing, repeated or unknown, or
    # none at all), or one with a result whose status is missing or not a batch
    # status, is refused whole, on resume too: nothing of it is written, sent again
    # or deleted.
    def results(edit):
        return lambda answer: {
            "result": {**answer["result"], "results": edit(answer["result"]["results"])}
        }

    for index, answer in enumerate(
        (
            results(lambda results: results[1:]),
            results(lambda results: [*results, results[0]]),
            results(lambda results: [*results[1:], {**results[0], "custom_id": "p9"}]),
            results(lambda results: [*results[1:], {**results[0], "custom_id": []}]),
            results(lambda results: {"p0": results[0]}),
            results(lambda results: [*results[1:], {"custom_id": "p0"}]),
            results(lambda results: [*results[1:], {**results[0], "status": []}]),
            results(lambda results: [*results[1:], {**results[0], "status": "passed"}]),
            lambda answer: None,
        )
    ):
        service = Service()
        service.answer = answer
        output = tmp_path / f"malformed-{index}.jsonl"
        with LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(service.handle),
        ) as api:
            for _ in range(2):
                with pytest.raises(LeanWarpCloudError, match="one result per item") as error:
                    api.verify_many(items(4), output=output, environment_id="b", workspaces=1)
                assert error.value.code == "invalid_response"
        assert not output.exists()
        assert service.charged == [["p0", "p1", "p2", "p3"]]
        assert service.deleted_operations == []


def test_one_run_at_a_time_uses_an_output(tmp_path: Path) -> None:
    service = Service()
    sent, gate = threading.Event(), threading.Event()

    def held(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operations"):
            sent.set()
            gate.wait(5)
        return service.handle(request)

    transport = httpx.MockTransport(held)
    options = {"output": tmp_path / "verdicts.jsonl", "environment_id": "b", "workspaces": 1}

    async def cancelled_then_retried() -> None:
        async with AsyncLeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=transport
        ) as client:
            first = asyncio.ensure_future(client.verify_many(items(3), **options))
            assert await asyncio.to_thread(sent.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # Its worker thread still runs the first run, which holds the output, by any path.
            link = tmp_path / "link.jsonl"
            link.symlink_to(options["output"])
            for output in (options["output"], link):
                with pytest.raises(UsageError, match="in use by another run"):
                    await client.verify_many(items(3), **{**options, "output": output})
            gate.set()

    asyncio.run(cancelled_then_retried())
    # Interrupted while it waits, a run keeps the output until its lanes end, and they
    # send nothing more: not even the entry their chunk in flight leaves unchecked.
    sent.clear()
    gate.clear()
    service.unchecked_once = {"p1"}
    interrupted = {**options, "output": tmp_path / "interrupted.jsonl"}
    join, owners = threading.Thread.join, []

    def interrupt(self: threading.Thread, timeout: float | None = None) -> None:
        if threading.current_thread() is not threading.main_thread():
            return join(self, timeout)
        sent.wait(5)
        owners.append(self)
        raise KeyboardInterrupt

    with LeanWarpCloud("secret", base_url="https://cloud.example", transport=transport) as api:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(threading.Thread, "join", interrupt)
            with pytest.raises(KeyboardInterrupt):
                api.verify_many(items(3), **interrupted)
        with pytest.raises(UsageError, match="in use by another run"):
            api.verify_many(items(3), **interrupted)
        gate.set()
        join(owners[0])
        assert service.charged == [["p0", "p1", "p2"]] * 2
        # Released once its lanes ended; resuming sends the unchecked entry alone.
        assert api.verify_many(items(3), **options)["skipped"] == 3
        assert api.verify_many(items(3), **interrupted)["skipped"] == 2
        # Another name for an output is refused, a hard link as a symlink is.
        os.link(options["output"], tmp_path / "hard.jsonl")
        with pytest.raises(UsageError, match="hard links"):
            api.verify_many(items(3), **{**options, "output": tmp_path / "hard.jsonl"})
    assert service.charged == [["p0", "p1", "p2"]] * 2 + [["p1"]]


def test_a_run_stopped_after_its_last_verdict_deletes_its_own_workspaces_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service()
    output = tmp_path / "verdicts.jsonl"
    write = _Run.write
    kept = 1

    def write_then_kill(self: _Run, lines: list[dict]) -> None:
        write(self, lines[:kept])
        raise Killed

    transport = httpx.MockTransport(service.handle)
    with (
        LeanWarpCloud("secret", base_url="https://cloud.example", transport=transport) as api,
        # Its one attempt at a creation whose answer is lost fails the run.
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", retries=0, transport=transport
        ) as once,
    ):
        with monkeypatch.context() as patch:
            patch.setattr(_Run, "write", write_then_kill)
            with pytest.raises(Killed):
                api.verify_many(items(3), output=output, environment_id="b", workspaces=2)
            # Only the written item: the rest of its chunk is unread, so nothing is deleted.
            api.verify_many(items(3)[:1], output=output, environment_id="b", workspaces=2)
            assert service.deleted == []
            # Stopped right after the last verdict is written.
            kept = 3
            with pytest.raises(Killed):
                api.verify_many(items(3), output=output, environment_id="b", workspaces=2)
        assert service.deleted == []
        summary = api.verify_many(items(3), output=output, environment_id="b", workspaces=2)
        assert summary["skipped"] == 3 and sorted(service.deleted) == ["w0", "w1"]
        # Cleanup again finds them gone, which is done.
        api.verify_many(items(3), output=output, environment_id="b", workspaces=2)
        # A workspace created without an answer is still deleted by a run with fewer lanes.
        service.unanswered = {"w3"}
        with pytest.raises(LeanWarpError):
            once.verify_many(
                items(3), output=tmp_path / "lost.jsonl", environment_id="b", workspaces=2
            )
        # Resumed in another environment, then in a workspace of the caller's after a refusal.
        api.verify_many(items(3), output=tmp_path / "lost.jsonl", environment_id="d", workspaces=1)
        assert sorted(service.deleted) == ["w0", "w1", "w2", "w3", "w4"]
        with pytest.raises(LeanWarpError):
            api.verify_many(items(3), output=tmp_path / "refused.jsonl", environment_id="refused")
        api.verify_many(items(3), output=tmp_path / "refused.jsonl", workspace_ids=["w7"])
        # A replay that does not show nothing was made fails cleanup, which a rerun finishes.
        service.unanswered = {"w5"}
        with pytest.raises(LeanWarpError):
            once.verify_many(items(3), output=tmp_path / "denied.jsonl", environment_id="b")
        service.deny_creates = True
        with pytest.raises(LeanWarpError):
            api.verify_many(items(3), output=tmp_path / "denied.jsonl", workspace_ids=["w7"])
        service.deny_creates = False
        api.verify_many(items(3), output=tmp_path / "denied.jsonl", workspace_ids=["w7"])
        assert "w5" in service.deleted
        # Nor does a refusal of the replay: its environment retired after a lost creation.
        service.unanswered = {"w6"}
        with pytest.raises(LeanWarpError):
            once.verify_many(items(3), output=tmp_path / "retired.jsonl", environment_id="e")
        service.retired.add("e")
        with pytest.raises(LeanWarpError):
            api.verify_many(items(3), output=tmp_path / "retired.jsonl", workspace_ids=["w7"])
        service.retired.discard("e")
        api.verify_many(items(3), output=tmp_path / "retired.jsonl", workspace_ids=["w7"])
        # A workspace the caller supplied is never deleted, and keep_workspaces keeps them.
        api.verify_many(items(3), output=tmp_path / "theirs.jsonl", workspace_ids=["w7"])
        api.verify_many(
            items(3), output=tmp_path / "kept.jsonl", environment_id="c", keep_workspaces=True
        )
        api.verify_many(
            items(3), output=tmp_path / "kept.jsonl", environment_id="c", keep_workspaces=True
        )
        # The same within one run: its own resend is refused once the environment retired.
        lost = f"w{len(service.workspaces)}"
        service.unanswered, service.retire_on_loss = {lost}, True
        with pytest.raises(LeanWarpError):
            api.verify_many(items(3), output=tmp_path / "resent.jsonl", environment_id="f")
        with pytest.raises(LeanWarpError):
            api.verify_many(items(3), output=tmp_path / "resent.jsonl", workspace_ids=["w7"])
        service.retired.discard("f")
        api.verify_many(items(3), output=tmp_path / "resent.jsonl", workspace_ids=["w7"])
    assert set(service.deleted) == {"w0", "w1", "w2", "w3", "w4", "w5", "w6", lost}


def test_a_journal_from_before_environments_is_named_not_resumed(tmp_path: Path) -> None:
    service = Service()
    transport = httpx.MockTransport(service.handle)

    def earlier(output: Path) -> None:
        """Rewrite a journal as an earlier SDK wrote it: unversioned, by catalog build."""
        journal = output.with_name(output.name + ".journal")
        lines = [json.loads(line) for line in journal.read_text().splitlines()]
        for line in lines:
            line.pop("version", None)
            if line.get("type") == "workspace":
                line["bundle_id"] = line.pop("environment_id")
        journal.write_text("".join(json.dumps(line) + "\n" for line in lines))

    with LeanWarpCloud("secret", base_url="https://cloud.example", transport=transport) as api:
        created = tmp_path / "created.jsonl"
        api.verify_many(
            items(1), output=created, environment_id="b", workspaces=1, keep_workspaces=True
        )
        earlier(created)
        with pytest.raises(UsageError, match=r"earlier SDK.*`leanwarp delete --confirm w0`$"):
            api.verify_many(items(1), output=created, environment_id="b", workspaces=1)
        # A creation never answered, in another build at the same index, is pointed at.
        journal = created.with_name(created.name + ".journal")
        with journal.open("a") as stream:
            stream.write(json.dumps({"type": "workspace", "index": 0, "bundle_id": "c"}) + "\n")
        with pytest.raises(UsageError, match=r"w0`\. It may have created others"):
            api.verify_many(items(1), output=created, environment_id="b", workspaces=1)
        # One that ran in the caller's own workspaces journals none, and resumes neither.
        given = tmp_path / "given.jsonl"
        api.verify_many(items(1), output=given, workspace_ids=["w7"])
        earlier(given)
        with pytest.raises(UsageError, match=r"earlier SDK.*new output$"):
            api.verify_many(items(2), output=given, workspace_ids=["w7"])

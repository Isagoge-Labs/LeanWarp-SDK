from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from leanwarp_cloud import (
    LeanWarpCloud,
    LeanWarpCloudError,
    LeanWarpTransportError,
    OperationTimeout,
)


def test_workspace_default_belongs_to_service_and_explicit_profiles_are_preserved() -> None:
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"workspace_id": "w"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        api.create_workspace("e")
        api.create_workspace("e", resource_profile="standard", max_resource_profile="large")
    assert bodies[0] == {"environment_id": "e", "max_spend_microusd": None}
    assert bodies[1] == {
        "environment_id": "e",
        "max_spend_microusd": None,
        "resource_profile": "standard",
        "max_resource_profile": "large",
    }


def test_retry_preserves_mutation_identity_and_body() -> None:
    requests: list[httpx.Request] = []
    tactics = ["simp"]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            tactics.append("omega")
            raise httpx.ReadTimeout("response lost after acceptance", request=request)
        return httpx.Response(202, json={"operation_id": "op-1", "state": "queued"})

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        patch("leanwarp_cloud.client.time.sleep"),
    ):
        result = api.submit(
            "workspace-1",
            "try_tactics",
            expected_revision=3,
            payload={"file": "A.lean", "tactics": tactics},
        )
    assert result["operation_id"] == "op-1"
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[0].content == requests[1].content
    assert json.loads(requests[1].content)["payload"]["tactics"] == ["simp"]
    assert requests[1].headers["Content-Type"] == "application/json"
    assert requests[0].url.path == "/v1/leanwarp/workspaces/workspace-1/operations"


def test_capped_workspace_retry_keeps_exact_cap_and_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("lost creation response", request=request)
        return httpx.Response(200, json={"workspace_id": "w"})

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        patch("leanwarp_cloud.client.time.sleep"),
    ):
        api.create_workspace("b", max_spend_microusd=2_000_001)
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert json.loads(requests[0].content)["max_spend_microusd"] == 2_000_001


@pytest.mark.parametrize("cap", [True, -1, 1.0, 10**12 + 1])
def test_invalid_workspace_cap_sends_no_request(cap) -> None:
    requests: list[httpx.Request] = []
    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(lambda request: requests.append(request)),
        ) as api,
        pytest.raises(ValueError, match="max_spend_microusd"),
    ):
        api.create_workspace("b", max_spend_microusd=cap)
    assert requests == []


def test_revision_conflict_is_not_retried_or_rebased() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            409, json={"error": {"code": "revision_conflict", "message": "refresh"}}
        )

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        pytest.raises(LeanWarpCloudError) as caught,
    ):
        api.sync_files(
            "workspace", expected_revision=1, files={"A.lean": "example : True := by trivial"}
        )
    assert caught.value.code == "revision_conflict"
    assert len(requests) == 1


def test_does_not_forward_authentication_on_redirect() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example"})

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        pytest.raises(LeanWarpCloudError),
    ):
        api.versions()
    assert len(requests) == 1


def test_polling_returns_failure_as_operation_and_timeout_keeps_handle() -> None:
    with LeanWarpCloud(
        "secret",
        base_url="http://localhost:8000",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"operation_id": "op-1", "state": "failed", "error_code": "timeout"}
            )
        ),
    ) as api:
        assert api.wait("op-1")["error_code"] == "timeout"
    with (
        LeanWarpCloud(
            "secret",
            base_url="http://localhost:8000",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"operation_id": "op-1", "state": "queued"})
            ),
        ) as api,
        # Every clock read advances time, so the one-second budget expires mid-poll.
        patch("leanwarp_cloud.client.time.monotonic", side_effect=itertools.count(0, 0.4)),
        pytest.raises(OperationTimeout) as caught,
    ):
        api.wait("op-1", timeout=1)
    assert caught.value.operation_id == "op-1"


def test_trickling_response_checks_the_wait_deadline_before_eof(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", lambda: clock[0])
    payload = json.dumps({"operation_id": "op-1", "state": "queued"}).encode()
    received: list[int] = []

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for byte in payload:
                # Each read succeeds before its inactivity timeout. Fixed-size
                # iteration used to hide all these reads until EOF.
                clock[0] += 1
                received.append(byte)
                yield bytes([byte])

    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            retries=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Trickle())),
        ) as api,
        pytest.raises(OperationTimeout) as caught,
    ):
        api.wait("op-1", timeout=20)
    assert caught.value.operation_id == "op-1"
    assert clock[0] == 1020
    assert len(received) < len(payload)


@pytest.mark.parametrize("identity", [None, "", "another-operation"])
def test_polling_rejects_an_uncorrelated_terminal_result(identity) -> None:
    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"operation_id": identity, "state": "completed"})
            ),
        ) as api,
        pytest.raises(LeanWarpCloudError) as caught,
    ):
        api.wait("requested-operation")
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "ftp://localhost",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
    ],
)
def test_rejects_unsafe_base_urls(url: str) -> None:
    with pytest.raises(ValueError):
        LeanWarpCloud("secret", base_url=url)


def test_explicit_idempotency_key_supports_recovery_after_client_restart() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        body = json.loads(request.content)
        assert body["resource_profile"] == "large"
        return httpx.Response(202, json={"operation_id": "stable"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        api.submit(
            "w",
            "verify_target",
            expected_revision=2,
            payload={"file": "A.lean"},
            resource_profile="large",
            idempotency_key="persisted-intent-123",
        )
    assert seen == ["persisted-intent-123"]


def test_streamed_response_limit_stops_before_an_unbounded_result() -> None:
    class ExcessiveBody(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(40):
                yield b"x" * 65536

    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ExcessiveBody())),
        ) as api,
        pytest.raises(LeanWarpCloudError) as caught,
    ):
        api.operation("op")
    assert caught.value.code == "response_too_large"


def test_project_workspace_is_created_in_the_projects_own_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leanwarp_cloud.client as client_module

    monkeypatch.setattr(client_module, "_BUSY_SECONDS", 0.0)
    example = Path(__file__).parents[1] / "examples/lean-4.35"
    for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        (tmp_path / name).write_bytes((example / name).read_bytes())
    requests: list[tuple[str, str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/leanwarp/")
        body = (
            json.loads(request.content)
            if request.headers.get("content-type", "").startswith("application/json")
            else request.content
        )
        requests.append((request.method, path.split("/")[0], body))
        if path == "environment-uploads":
            return httpx.Response(
                201, json={"upload_id": "u", "expires_at": "x", "missing": sorted(body["blobs"])}
            )
        if path.startswith("environment-uploads/"):
            if sum(method == "PUT" for method, _, _ in requests) == 1:
                # Its address is still being erased: sent again once it is free.
                return httpx.Response(409, json={"error": {"code": "upload_busy"}})
            return httpx.Response(204)
        if path == "environments":
            if not any(method == "PUT" for method, _, _ in requests):
                return httpx.Response(409, json={"error": {"code": "upload_required"}})
            return httpx.Response(200, json={"environment_id": "e", "state": "ready"})
        return httpx.Response(201, json={"workspace_id": "w"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        created = api.create_workspace_for_project(tmp_path, max_spend_microusd=2_000_001)
    assert created["workspace_id"] == "w"
    lakefile = (example / "lakefile.toml").read_bytes()
    # Held inputs are never resent: an upload happens only when the service asks.
    assert [(method, route) for method, route, _ in requests] == [
        ("POST", "environments"),
        ("POST", "environment-uploads"),
        ("PUT", "environment-uploads"),
        ("PUT", "environment-uploads"),
        ("POST", "environments"),
        ("POST", "workspaces"),
    ]
    assert requests[2][2] == requests[3][2] == lakefile
    submitted = requests[4][2]
    assert isinstance(submitted, dict)
    assert submitted["manifest"] == (example / "lake-manifest.json").read_text()
    assert submitted["lakefile"] == {
        "name": "lakefile.toml",
        "sha256": hashlib.sha256(lakefile).hexdigest(),
    }
    assert requests[5][2] == {"environment_id": "e", "max_spend_microusd": 2_000_001}


def test_execution_deadline_is_forwarded_and_retried_independently_of_polling() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("accepted response lost", request=request)
        return httpx.Response(202, json={"operation_id": "op"})

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        patch("leanwarp_cloud.client.time.sleep"),
    ):
        api.submit(
            "workspace",
            "verify_target",
            expected_revision=1,
            payload={
                "file": "Main.lean",
                "candidate_declaration": "proof",
                "target_statement": "True",
                "timeout": 300,
            },
            timeout_seconds=360,
        )
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert json.loads(requests[0].content)["timeout_seconds"] == 360
    assert json.loads(requests[0].content)["payload"]["timeout"] == 300


def test_verify_batch_submits_inline_proofs_and_bounds_their_count() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"operation_id": "op"})

    codes = [{"custom_id": "a", "proof": "example : True := ⟨⟩"}]
    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        api.verify_batch("workspace", codes, expected_revision=4, strict=False, timeout=20)
        api.verify_batch("workspace", codes, expected_revision=4)
        for count in (0, 65):
            with pytest.raises(ValueError, match="1 to 64"):
                api.verify_batch("workspace", codes * count, expected_revision=4)
    bodies = [json.loads(request.content) for request in requests]
    assert len(bodies) == 2
    assert bodies[0] == {
        "kind": "verify_batch",
        "expected_revision": 4,
        "payload": {"codes": codes, "strict": False, "timeout": 20},
    }
    assert bodies[1]["payload"] == {"codes": codes, "strict": True}


@pytest.mark.parametrize(
    ("method", "limits"),
    [
        ("simplify_proofs", {"max_simplifications": 16, "min_reduction_bytes": 1}),
        ("repair_proofs", {"max_repairs": 16}),
    ],
)
def test_proof_rewrites_leave_tactic_budgets_to_leanwarp_unless_given(
    method: str, limits: dict[str, int]
) -> None:
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"operation_id": "op"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        getattr(api, method)("workspace", "Main.lean", expected_revision=2)
        getattr(api, method)("workspace", "Main.lean", expected_revision=2, timeout_ms=2000)
    # A fixed per-tactic budget would crowd most targets out of the operation's time.
    assert bodies[0]["payload"] == {"file": "Main.lean", **limits}
    assert bodies[1]["payload"] == {"file": "Main.lean", "timeout_ms": 2000, **limits}


def test_short_retention_is_sent_only_when_asked_for() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"operation_id": "op"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        api.check("workspace", "Main.lean", expected_revision=1)
        api.check("workspace", "Main.lean", expected_revision=1, retention="short")
        api.inspect(
            "workspace", "Main.lean", expected_revision=1, line=1, column=0, retention="short"
        )
        api.try_tactics(
            "workspace",
            "Main.lean",
            expected_revision=1,
            line=1,
            column=0,
            tactics=["simp"],
            retention="short",
        )
        api.submit(
            "workspace",
            "check",
            expected_revision=1,
            payload={"file": "Main.lean"},
            retention="standard",
        )
        api.run("workspace", "check", expected_revision=1, payload={}, retention="short")
        with pytest.raises(ValueError, match="retention"):
            api.check("workspace", "Main.lean", expected_revision=1, retention="brief")  # type: ignore[arg-type]
    bodies = [json.loads(request.content) for request in requests]
    assert [body.get("retention") for body in bodies] == [
        None,
        "short",
        "short",
        "short",
        "standard",
        "short",
    ]
    assert "retention" not in bodies[0]
    assert requests[-1].url.path.endswith("/run")


@pytest.mark.parametrize("deadline", [0, 1801, True, 1.5])
def test_invalid_execution_deadline_is_rejected_before_submission(deadline: object) -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid deadline sent")

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(unexpected)
        ) as api,
        pytest.raises(ValueError, match="timeout_seconds"),
    ):
        api.submit(
            "workspace",
            "check",
            expected_revision=1,
            payload={"file": "Main.lean"},
            timeout_seconds=deadline,
        )


class _Clock:
    """A fake monotonic clock that sleeps and held reads advance."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(
    ("timeout", "poll_interval", "completes_after", "expected_elapsed"),
    [(0.8, 0.1, 0.15, 0.2), (3.5, 1.0, 3.0, 3.2)],
)
def test_wait_observes_completion_in_the_final_fraction_of_its_budget(
    monkeypatch, timeout, poll_interval, completes_after, expected_elapsed
) -> None:
    clock = _Clock()
    started = clock.now
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", clock.sleep)
    read_timeouts: list[float] = []
    response_seconds = 0.05

    def handle(request: httpx.Request) -> httpx.Response:
        read_timeout = request.extensions["timeout"]["read"]
        read_timeouts.append(read_timeout)
        # Even the last read has enough time for this API's response.
        assert response_seconds < read_timeout <= timeout - (clock.now - started)
        clock.now += response_seconds
        state = "completed" if clock.now - started >= completes_after else "running"
        return httpx.Response(200, json={"operation_id": "op-1", "state": state})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        operation = api.wait("op-1", timeout=timeout, poll_interval=poll_interval)

    assert operation["state"] == "completed"
    assert len(read_timeouts) > 1
    assert read_timeouts[-1] < 1
    assert clock.now - started == pytest.approx(expected_elapsed)
    assert clock.now - started < timeout


def test_waiting_asks_the_server_to_hold_each_read_until_the_operation_ends(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", clock.sleep)
    holds: list[float] = []
    read_timeouts: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hold = float(request.url.params["wait_seconds"])
        holds.append(hold)
        read_timeouts.append(request.extensions["timeout"]["read"])
        # The first read is held for its whole window; the operation then ends.
        clock.now += hold
        state = "running" if len(holds) == 1 else "completed"
        return httpx.Response(200, json={"operation_id": "op-1", "state": state})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        operation = api.wait("op-1", timeout=300)

    assert operation["state"] == "completed"
    assert holds[0] == 20
    assert all(timeout > hold for timeout, hold in zip(read_timeouts, holds, strict=True))
    # Held reads pace themselves; there is no sleeping between them.
    assert clock.sleeps == []


def test_waiting_polls_an_api_that_answers_without_holding_the_read(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", clock.sleep)
    reads = itertools.count(1)

    def handle(request: httpx.Request) -> httpx.Response:
        state = "completed" if next(reads) == 3 else "running"
        return httpx.Response(200, json={"operation_id": "op-1", "state": state})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        assert api.wait("op-1", timeout=60, poll_interval=1)["state"] == "completed"
    assert clock.sleeps == [1, 1]


def test_reads_cannot_ask_the_server_to_hold_longer_than_it_will() -> None:
    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ) as api,
        pytest.raises(ValueError, match="wait_seconds"),
    ):
        api.operation("op-1", wait_seconds=21)


@pytest.mark.parametrize("failure", ["gateway", "timeout"])
def test_wait_falls_back_when_a_proxy_cannot_hold_reads(monkeypatch, failure) -> None:
    clock = _Clock()
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", clock.sleep)
    holds = []
    completes_at = clock.now + 60

    def handle(request):
        hold = float(request.url.params.get("wait_seconds", 0))
        holds.append(hold)
        if hold > 5:
            clock.now += 5
            if failure == "timeout":
                raise httpx.ReadTimeout("proxy hold expired", request=request)
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={
                "operation_id": "op-1",
                "state": "completed" if clock.now >= completes_at else "running",
            },
        )

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        assert api.wait("op-1", timeout=300)["state"] == "completed"
    assert holds[0] == 20
    assert holds[1:] and set(holds[1:]) == {0}
    assert clock.now == completes_at


@pytest.mark.parametrize(
    "status,code", [(403, "forbidden"), (404, "not_found"), (502, "invalid_response")]
)
def test_held_reads_do_not_hide_authorization_or_response_errors(status, code) -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code}})

    with (
        LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api,
        pytest.raises(LeanWarpCloudError) as caught,
    ):
        api.wait("op-1")
    assert caught.value.code == code
    assert len(calls) == 1


@pytest.mark.parametrize("held", [False, True])
def test_held_reads_preserve_rate_limit_backoff(monkeypatch, held):
    clock = _Clock()
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("leanwarp_cloud.client.time.sleep", clock.sleep)
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"operation_id": "op-1", "state": "completed"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        result = api.wait("op-1") if held else api.operation("op-1")
    assert result["state"] == "completed"
    # The service's value is a floor; jitter spreads out processes it refused together.
    assert len(calls) == 2 and 1 <= clock.sleeps[0] <= 1.25


@pytest.mark.parametrize("action", ["cancel", "stop", "delete", "submit"])
def test_cleanup_requests_bound_trickling_body_with_the_shared_deadline(monkeypatch, action):
    clock = [1000.0]
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", lambda: clock[0])
    received, requests = [], []
    payload = json.dumps({"workspace_id": "owned-workspace", "state": "stopping"}).encode()

    class Trickle(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            for byte in payload:
                clock[0] += 1
                received.append(byte)
                yield bytes([byte])

        def close(self):
            self.closed = True

    stream = Trickle()

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            retries=1,
            transport=httpx.MockTransport(handle),
        ) as cloud,
        pytest.raises(LeanWarpTransportError),
    ):
        if action == "submit":
            cloud.submit(
                "owned-id",
                "check",
                expected_revision=0,
                payload={"file": "Main.lean"},
                idempotency_key="saved-key",
                deadline=1020.0,
            )
        else:
            getattr(cloud, action)("owned-id", deadline=1020.0)
    assert len(requests) == 1
    assert requests[0].extensions["leanwarp_deadline"] == 1020.0
    assert requests[0].extensions["timeout"]["read"] == pytest.approx(20 / 3)
    assert clock[0] == 1020 and len(received) < len(payload) and stream.closed


@pytest.mark.parametrize("action", ["cancel", "stop", "delete", "submit"])
def test_cleanup_expired_deadline_never_starts_an_attempt(monkeypatch, action):
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", lambda: 1000.0)
    requests = []
    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            transport=httpx.MockTransport(requests.append),
        ) as cloud,
        pytest.raises(LeanWarpTransportError),
    ):
        if action == "submit":
            cloud.submit(
                "owned-id",
                "check",
                expected_revision=0,
                payload={"file": "Main.lean"},
                idempotency_key="saved-key",
                deadline=1000.0,
            )
        else:
            getattr(cloud, action)("owned-id", deadline=1000.0)
    assert requests == []
    # A backoff that wakes past the window sends no resend.
    from leanwarp_cloud import client as client_module

    now = [0.0]
    monkeypatch.setattr("leanwarp_cloud.client.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "leanwarp_cloud.client.time.sleep", lambda s: now.__setitem__(0, now[0] + s + 5)
    )
    assert client_module._pause(0.5, 2.0) is False


@pytest.mark.parametrize(
    "status,method,attempts", [(503, "GET", 3), (503, "POST", 3), (500, "GET", 1)]
)
def test_transient_persistence_retries_remain_bounded_and_safe(status, method, attempts):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": "persistence_request_unavailable"}})

    with (
        LeanWarpCloud(
            "secret",
            base_url="https://cloud.example",
            retries=2,
            transport=httpx.MockTransport(handle),
        ) as api,
        patch("leanwarp_cloud.client.time.sleep"),
        pytest.raises(LeanWarpCloudError) as raised,
    ):
        if method == "GET":
            api.workspace("w")
        else:
            api.stop("w", generation=2)
    assert raised.value.status == status
    assert len(requests) == attempts
    assert all("Idempotency-Key" not in request.headers for request in requests)


def test_a_retry_after_a_lost_stored_input_offers_every_input_again(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "examples/lean-4.35"
    for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        (tmp_path / name).write_bytes((example / name).read_bytes())
    requests: list[tuple[str, str]] = []
    failed = {"environment_id": "e", "attempt_id": "a1", "state": "failed",
              "error": {"code": "stored_file_missing", "action": "retry"}}  # fmt: skip

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/leanwarp/")
        requests.append((request.method, path))
        if path == "environment-uploads":
            # The service no longer holds the input whose stored copy was lost.
            blobs = json.loads(request.content)["blobs"]
            return httpx.Response(201, json={"upload_id": "u", "expires_at": "x",
                                             "missing": sorted(blobs)})  # fmt: skip
        if path.startswith("environment-uploads/"):
            return httpx.Response(204)
        if path == "environments/e/retry":
            return httpx.Response(202, json={**failed, "attempt_id": "a2", "state": "building"})
        if path == "environments":
            retried = any(route == "environments/e/retry" for _, route in requests)
            return httpx.Response(
                200, json={**failed, "state": "ready", "error": None} if retried else failed
            )
        return httpx.Response(404)

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        ready = api.connect_environment(tmp_path, retry=True, timeout=30, poll_seconds=0)
    assert ready["state"] == "ready"
    # The inputs are offered (and the missing one sent) before the build is retried.
    routes = [route for _, route in requests]
    assert routes.index("environment-uploads") < routes.index("environments/e/retry")
    assert any(method == "PUT" for method, _ in requests)
    # A project edited before the failure reached it is connected as it is now: the
    # failed environment, of other inputs, is not retried.
    requests.clear()
    edited = {**failed, "environment_id": "e2"}
    submitted: list[str] = []

    def changed(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/leanwarp/")
        requests.append((request.method, path))
        if path == "environments":
            submitted.append(json.loads(request.content)["lakefile"]["sha256"])
            if len(submitted) == 1:
                (tmp_path / "lakefile.toml").write_text(
                    (tmp_path / "lakefile.toml").read_text() + "\n"
                )
                return httpx.Response(200, json=failed)
            return httpx.Response(200, json={**edited, "state": "ready", "error": None})
        return handle(request)

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(changed)
    ) as api:
        assert api.connect_environment(tmp_path, retry=True, timeout=30)["environment_id"] == "e2"
    assert len(set(submitted)) == 2
    assert not any(route.endswith("/retry") for _, route in requests)


def test_after_a_lost_input_a_reused_committed_answer_carries_its_commits_bytes(
    tmp_path: Path,
) -> None:
    import subprocess
    import time

    from leanwarp_cloud.client import _Exchange
    from leanwarp_cloud.environment import Commit, head_of

    def git(*argv: str) -> None:
        subprocess.run(  # noqa: S603 - the test's own fixed commands
            ["git", "-C", str(tmp_path), *argv],  # noqa: S607
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    example = Path(__file__).parents[1] / "examples/lean-4.35"
    project = tmp_path / "project"
    (project / "Sub").mkdir(parents=True)
    for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        (project / name).write_bytes((example / name).read_bytes())
    committed = {"../a.json": b'{"flag": 1}', "../b.json": b'{"flag": 22}'}
    for path, data in committed.items():
        (project / path).write_bytes(data)
    git("add", "a.json", "b.json")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    head = head_of(str(tmp_path))
    assert head is not None
    answers = {
        path: {"kind": "file", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in committed.items()
    }
    # The working copies changed since; the commit, which the builds were answered
    # from, holds.
    for path in committed:
        (project / path).write_bytes(b"edited")
    uploads: list[list[str]] = []
    puts: list[str] = []
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/leanwarp/")
        if path == "environment-uploads":
            blobs = json.loads(request.content)["blobs"]
            uploads.append(sorted(blobs))
            return httpx.Response(201, json={"upload_id": "u", "expires_at": "x",
                                             "missing": sorted(blobs)})  # fmt: skip
        if path.startswith("environment-uploads/"):
            puts.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        sent.append(json.loads(request.content)["answers"])
        return httpx.Response(202, json={"environment_id": "e", "state": "building"})

    environment = {"environment_id": "e", "attempt_id": "a", "input_token": "t" * 64,
                   "input_requests": sorted(committed)}  # fmt: skip
    for start, restore in ((project, False), (project / "Sub", True)):
        exchange = _Exchange(deadline=time.monotonic() + 30)
        exchange.give(answers, dict.fromkeys(committed, Commit(str(tmp_path), head)))
        exchange.restore = restore
        uploads.clear()
        puts.clear()
        with LeanWarpCloud(
            "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
        ) as api:
            api.answer_environment_inputs(environment, start, exchange=exchange)
        # Reused as given; after a lost copy, from the project or a directory inside
        # it, named once on one receipt and then sent one file at a time, each with
        # its commit's bytes.
        assert sent[-1] == answers
        digests = sorted(item["sha256"] for item in answers.values())
        assert (uploads, sorted(puts)) == (([digests], digests) if restore else ([], []))

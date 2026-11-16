"""Releasing results, waiting on many operations, and limits that free with time."""

from __future__ import annotations

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpCloudError, UsageError


def client(handle: object) -> LeanWarpCloud:
    return LeanWarpCloud(
        "secret",
        base_url="https://cloud.example",
        transport=httpx.MockTransport(handle),  # type: ignore[arg-type]
        retries=2,
    )


def test_deleting_a_result_is_resent_and_repeating_it_succeeds() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("lost", request=request)
        return httpx.Response(204)

    with client(handle) as api:
        assert api.delete_operation("op-1") is None
    assert [(r.method, r.url.path) for r in requests] == [
        ("DELETE", "/v1/leanwarp/operations/op-1")
    ] * 2


def test_waiting_on_many_operations_is_one_bounded_request() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"operations": [{"operation_id": "a"}], "unread": ["b"]})

    with client(handle) as api:
        found = api.wait_operations(["a", "b"], wait_seconds=5)
        assert found["unread"] == ["b"]
        for bad in ([], ["x"] * 101, ["a,b"]):
            with pytest.raises(UsageError):
                api.wait_operations(bad)
        with pytest.raises(UsageError):
            api.wait_operations(["a"], wait_seconds=21)
    (request,) = requests
    assert request.url.path == "/v1/leanwarp/operations"
    assert request.url.params["ids"] == "a,b" and request.url.params["wait_seconds"] == "5.000"


def test_a_full_account_is_resent_after_retry_after_with_the_same_key() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"code": "operation_storage_limit_exceeded", "message": "m"}},
            )
        return httpx.Response(202, json={"operation_id": "op"})

    with client(handle) as api:
        api.submit("w", "check", expected_revision=1, payload={"file": "A.lean"})
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]


def test_a_limit_that_outlasts_the_deadline_is_raised_with_its_wait() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "3000"},
            json={"error": {"code": "operation_limit_exceeded", "message": "m"}},
        )

    with client(handle) as api, pytest.raises(LeanWarpCloudError) as raised:
        api.submit("w", "check", expected_revision=1, payload={"file": "A.lean"})
    assert raised.value.status == 429 and raised.value.code == "operation_limit_exceeded"
    assert raised.value.retry_after == 3000

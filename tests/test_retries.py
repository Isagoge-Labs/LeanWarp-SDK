from __future__ import annotations

import json
import logging
import pickle
import warnings

import httpx
import leanwarp_cloud
import pytest
from leanwarp_cloud import (
    LeanWarpCloud,
    LeanWarpCloudError,
    LeanWarpError,
    LeanWarpTransportError,
    OperationTimeout,
    updates,
)
from leanwarp_cloud import (
    client as client_module,
)

OPERATION_ID = "op-1"
RUNNING = {"operation_id": OPERATION_ID, "state": "running"}
FINISHED = {"operation_id": OPERATION_ID, "state": "completed"}
BASE = "https://api.example"


class Clock:
    """A fake monotonic clock that only sleeping advances."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(client_module.time, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def scripted(steps: list, *, interface: str = "python", retries: int | None = None):
    """A client whose responses come from ``steps``; the last step repeats."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        step = steps[min(len(requests), len(steps)) - 1]
        if isinstance(step, Exception):
            raise step
        return step() if callable(step) else step

    cloud = LeanWarpCloud(
        "secret-token",
        base_url=BASE,
        transport=httpx.MockTransport(handle),
        interface=interface,  # type: ignore[arg-type]
        retries=retries,
    )
    return cloud, requests


def refused(status: int, *, retry_after: str | None = None, code: str = "unavailable"):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(
        status, json={"error": {"code": code, "message": "not now"}}, headers=headers
    )


OK_STOP = httpx.Response(200, json={"workspace_id": "w", "state": "stopped"})
OK_CANCEL = httpx.Response(200, json={"operation_id": OPERATION_ID, "state": "cancelled"})
GONE = httpx.Response(204)


def cleanup(cloud: LeanWarpCloud, action: str):
    if action == "stop":
        return cloud.stop("w", generation=3)
    return getattr(cloud, action)(OPERATION_ID if action == "cancel" else "w")


@pytest.mark.parametrize("action", ["stop", "cancel", "delete"])
@pytest.mark.parametrize(
    "failure",
    [
        refused(429),
        refused(502),
        refused(503),
        refused(504),
        httpx.ConnectError("reset"),
        httpx.ReadTimeout("lost"),
    ],
    ids=["429", "502", "503", "504", "connect", "timeout"],
)
def test_naturally_idempotent_calls_are_retried(monkeypatch, action, failure):
    Clock(monkeypatch)
    success = {"stop": OK_STOP, "cancel": OK_CANCEL, "delete": GONE}[action]
    cloud, requests = scripted([failure, failure, success])
    with cloud:
        cleanup(cloud, action)
    assert len(requests) == 3
    if action == "stop":
        assert all(request.url.params["generation"] == "3" for request in requests)
    assert all("Idempotency-Key" not in request.headers for request in requests)


def test_a_definite_refusal_is_not_retried(monkeypatch):
    Clock(monkeypatch)
    cloud, requests = scripted([refused(409, code="workspace_busy")])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.stop("w", generation=3)
    assert len(requests) == 1 and raised.value.retryable is False


def test_a_deleted_workspace_found_on_a_resend_is_success(monkeypatch):
    Clock(monkeypatch)
    missing = refused(404, code="not_found")
    cloud, requests = scripted([httpx.ReadTimeout("deleted, response lost"), missing])
    with cloud:
        assert cloud.delete("w") is None
    assert len(requests) == 2


def test_a_first_answer_of_not_found_is_still_an_error(monkeypatch):
    Clock(monkeypatch)
    cloud, _ = scripted([refused(404, code="not_found")])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.delete("typo")
    assert raised.value.code == "not_found"


@pytest.mark.parametrize(("interface", "window"), [("python", 60), ("cli", 60), ("mcp", 15)])
def test_retries_stop_at_the_window_when_there_is_no_deadline(monkeypatch, interface, window):
    clock = Clock(monkeypatch)
    cloud, requests = scripted([refused(503)], interface=interface)
    with cloud, pytest.raises(LeanWarpCloudError):
        cloud.stop("w", generation=3)
    assert clock.now - 1000 < window
    # A patient loop, not two quick tries: backoff is capped, so it used the window.
    assert clock.now - 1000 > window / 2
    assert len(requests) > 4


def test_retries_stop_at_the_callers_deadline(monkeypatch):
    clock = Clock(monkeypatch)
    cloud, _ = scripted([refused(503)])
    with cloud, pytest.raises(LeanWarpCloudError):
        cloud.stop("w", generation=3, deadline=clock.now + 5)
    assert clock.now < 1005


def test_retries_can_be_limited_or_disabled(monkeypatch):
    Clock(monkeypatch)
    cloud, requests = scripted([refused(503)], retries=0)
    with cloud, pytest.raises(LeanWarpCloudError):
        cloud.stop("w", generation=3)
    assert len(requests) == 1


def test_retry_after_is_honoured_beyond_five_seconds_within_the_deadline(monkeypatch):
    clock = Clock(monkeypatch)
    cloud, requests = scripted([refused(503, retry_after="30"), OK_STOP])
    with cloud:
        cloud.stop("w", generation=3, deadline=clock.now + 60)
    assert len(requests) == 2 and 30 <= clock.sleeps[0] <= 31


def test_retry_after_as_an_http_date_is_honoured(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    clock = Clock(monkeypatch)
    in_thirty = datetime.now(timezone.utc) + timedelta(seconds=30)  # noqa: UP017 - 3.10
    when = format_datetime(in_thirty, usegmt=True)
    cloud, requests = scripted([refused(503, retry_after=when), OK_STOP])
    with cloud:
        cloud.stop("w", generation=3, deadline=clock.now + 60)
    # HTTP dates have whole seconds, so the wait is within a second of thirty.
    assert len(requests) == 2 and 28.5 <= clock.sleeps[0] <= 31.5


@pytest.mark.parametrize(
    "value",
    ["Wed, 21 Oct 2015 07:28:00 +99999999999999999999", "Wed, 21 Oct 2015 07:28:00 +9999", "soon"],
)
def test_an_unparsable_retry_after_is_treated_as_absent(monkeypatch, value):
    Clock(monkeypatch)
    cloud, requests = scripted([refused(503, retry_after=value), OK_STOP])
    with cloud:
        cloud.stop("w", generation=3)
    assert len(requests) == 2


def test_retry_after_past_the_deadline_fails_at_once_and_says_when(monkeypatch):
    clock = Clock(monkeypatch)
    cloud, requests = scripted([refused(429, retry_after="120")])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.stop("w", generation=3, deadline=clock.now + 10)
    assert len(requests) == 1 and clock.sleeps == []
    assert raised.value.retry_after == 120 and raised.value.retryable is True


def test_backoff_is_jittered_so_processes_do_not_retry_in_step(monkeypatch):
    runs = []
    for _ in range(2):
        clock = Clock(monkeypatch)
        cloud, _requests = scripted([refused(503), refused(503), refused(503), OK_STOP])
        with cloud:
            cloud.stop("w", generation=3)
        runs.append(clock.sleeps)
    assert runs[0] != runs[1]
    for attempt, delay in enumerate(runs[0]):
        ceiling = 0.25 * 2**attempt
        assert ceiling / 2 <= delay <= ceiling


def test_a_lost_response_retries_a_write_with_the_same_key(monkeypatch):
    Clock(monkeypatch)
    accepted = httpx.Response(202, json={"operation_id": "stable"})
    cloud, requests = scripted([httpx.ReadTimeout("lost"), accepted])
    with cloud:
        cloud.submit("w", "check", expected_revision=1, payload={"file": "A.lean"})
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]


# wait()


def wait_client(monkeypatch, steps):
    clock = Clock(monkeypatch)
    cloud, requests = scripted(steps)
    return clock, cloud, requests


def body(value: dict) -> httpx.Response:
    return httpx.Response(200, json=value)


@pytest.mark.parametrize(
    "failure",
    [
        refused(429),
        refused(429, retry_after="3"),
        refused(500),
        refused(502),
        refused(503),
        refused(503, retry_after="3"),
        refused(504),
        httpx.ConnectError("reset"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["429", "429-after", "500", "502", "503", "503-after", "504", "connect", "timeout"],
)
def test_wait_treats_every_transient_failure_as_a_missed_poll(monkeypatch, failure):
    _, cloud, requests = wait_client(
        monkeypatch, [failure, failure, failure, failure, failure, body(FINISHED)]
    )
    with cloud:
        assert cloud.wait(OPERATION_ID, timeout=300)["state"] == "completed"
    assert len(requests) == 6


def test_wait_outlasts_a_long_outage_and_raises_only_at_its_deadline(monkeypatch):
    clock, cloud, requests = wait_client(monkeypatch, [body(RUNNING), refused(503)])
    with cloud, pytest.raises(OperationTimeout) as raised:
        cloud.wait(OPERATION_ID, timeout=120)
    assert clock.now == pytest.approx(1120)
    assert raised.value.operation_id == OPERATION_ID
    # The last thing it saw of the operation before the outage.
    assert raised.value.operation == RUNNING
    assert len(requests) > 10


def test_a_two_hour_outage_never_overflows_the_backoff(monkeypatch):
    # Thousands of missed polls: an unbounded exponent overflowed after about 5,100 s.
    clock, cloud, _ = wait_client(monkeypatch, [refused(503)])
    with cloud, pytest.raises(OperationTimeout):
        cloud.wait(OPERATION_ID, timeout=7200)
    assert clock.now == pytest.approx(1000 + 7200)
    assert max(clock.sleeps) <= 5


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication_required"),
        (403, "leanwarp_key_scope_denied"),
        (404, "not_found"),
        (502, "invalid_response"),
    ],
)
def test_wait_raises_a_definitive_error_at_once(monkeypatch, status, code):
    _, cloud, requests = wait_client(monkeypatch, [body(RUNNING), refused(status, code=code)])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.wait(OPERATION_ID, timeout=300)
    assert raised.value.code == code and len(requests) == 2


def test_wait_keeps_holding_reads_through_a_busy_service(monkeypatch):
    # Falling back to polling because the service is overloaded would add load to it.
    _, cloud, requests = wait_client(monkeypatch, [refused(503), body(FINISHED)])
    with cloud:
        cloud.wait(OPERATION_ID, timeout=300)
    assert float(requests[1].url.params["wait_seconds"]) > 0


def test_wait_honours_retry_after_up_to_its_deadline(monkeypatch):
    clock, cloud, _ = wait_client(monkeypatch, [refused(503, retry_after="30"), body(FINISHED)])
    with cloud:
        cloud.wait(OPERATION_ID, timeout=300)
    assert 30 <= clock.sleeps[0] <= 31


# Exceptions


def test_every_exported_exception_derives_from_the_one_root():
    exported = [
        value
        for name in leanwarp_cloud.__all__
        if isinstance(value := getattr(leanwarp_cloud, name), type) and issubclass(value, Exception)
    ]
    assert len(exported) == 10 and all(issubclass(kind, LeanWarpError) for kind in exported)


def test_a_refused_write_carries_what_the_caller_needs(monkeypatch):
    Clock(monkeypatch)
    cloud, requests = scripted([refused(503, retry_after="7")], retries=0)
    with cloud, pytest.raises(LeanWarpError) as raised:
        cloud.submit("w", "check", expected_revision=1, payload={"file": "A.lean"})
    error = raised.value
    assert isinstance(error, LeanWarpCloudError)
    assert (error.code, error.status, error.message) == ("unavailable", 503, "not now")
    assert str(error) == "not now"
    assert error.retryable is True and error.retry_after == 7
    assert error.idempotency_key == requests[0].headers["Idempotency-Key"]


def test_a_lost_response_returns_the_key_so_the_caller_can_resend_safely(monkeypatch):
    Clock(monkeypatch)
    cloud, requests = scripted([httpx.ReadTimeout("lost")], retries=0)
    with cloud, pytest.raises(LeanWarpTransportError) as raised:
        cloud.submit("w", "check", expected_revision=1, payload={"file": "A.lean"})
    error = raised.value
    assert error.code == "transport_error" and error.status is None
    assert error.retryable is True and error.timed_out is True
    assert error.idempotency_key == requests[0].headers["Idempotency-Key"]
    assert "lost" not in str(error)


def test_a_rejected_write_says_it_is_not_retryable_and_keeps_its_key(monkeypatch):
    Clock(monkeypatch)
    cloud, requests = scripted([refused(409, code="revision_conflict")])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.sync_files("w", expected_revision=1, files={"A.lean": ""})
    assert raised.value.retryable is False
    assert raised.value.idempotency_key == requests[0].headers["Idempotency-Key"]


def test_reads_have_no_idempotency_key_on_their_errors(monkeypatch):
    Clock(monkeypatch)
    cloud, _ = scripted([refused(404, code="not_found")])
    with cloud, pytest.raises(LeanWarpCloudError) as raised:
        cloud.workspace("w")
    assert raised.value.idempotency_key is None


@pytest.mark.parametrize(
    "error",
    [
        LeanWarpCloudError(503, "unavailable", "no", retry_after=2, idempotency_key="k" * 16),
        LeanWarpTransportError("lost", timed_out=True, idempotency_key="k" * 16),
        OperationTimeout("op", {"state": "running"}),
    ],
    ids=["cloud", "transport", "timeout"],
)
def test_exceptions_survive_crossing_a_process_boundary(error):
    copy = pickle.loads(pickle.dumps(error))  # noqa: S301 - this test's own object
    assert type(copy) is type(error) and copy.__dict__ == error.__dict__
    assert str(copy) == str(error)


# Logging


def test_debug_log_names_each_attempt_and_no_secret(monkeypatch, caplog):
    Clock(monkeypatch)
    source = "theorem hidden_proof_body : True := by trivial"
    cloud, requests = scripted(
        [httpx.ReadTimeout("lost"), httpx.Response(202, json={"operation_id": "op"})]
    )
    with cloud, caplog.at_level(logging.DEBUG, logger="leanwarp"):
        cloud.run(
            "w",
            "check",
            expected_revision=1,
            payload={"file": "A.lean", "proof": source},
            files={"A.lean": source},
        )
    lines = [record.getMessage() for record in caplog.records if record.name == "leanwarp"]
    assert len(lines) == 2
    assert lines[0].startswith("POST workspaces/w/run status=transport_error attempt=1")
    assert "status=202 attempt=2 latency_ms=" in lines[1]
    text = caplog.text
    assert "secret-token" not in text and "hidden_proof_body" not in text
    assert requests[0].headers["Idempotency-Key"] not in text


def test_log_lines_omit_a_querys_cursor(monkeypatch, caplog):
    Clock(monkeypatch)
    cloud, _ = scripted([httpx.Response(200, json={"operations": [], "next_cursor": None})])
    with cloud, caplog.at_level(logging.DEBUG, logger="leanwarp"):
        cloud.operations("w", cursor="opaque-cursor-value")
    assert "opaque-cursor-value" not in caplog.text


# Sunset


@pytest.fixture
def no_sunset_seen():
    updates._latest.clear()
    updates._sunset.clear()
    yield
    updates._latest.clear()
    updates._sunset.clear()


SUNSET = httpx.Response(200, json={"owner_id": "o"}, headers={"LeanWarp-SDK-Sunset": "2027-03-01"})


def test_a_sunset_date_warns_the_library_once_per_process(no_sunset_seen):
    cloud, requests = scripted([SUNSET])
    with cloud, warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        cloud.account()
        cloud.account()
        cloud.resources()
    assert len(requests) == 3
    deprecations = [item for item in seen if item.category is DeprecationWarning]
    assert len(deprecations) == 1 and "2027-03-01" in str(deprecations[0].message)


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_cli_and_mcp_report_the_sunset_instead_of_warning(no_sunset_seen, interface):
    cloud, _ = scripted([SUNSET], interface=interface)
    with cloud, warnings.catch_warnings():
        warnings.simplefilter("error")
        cloud.account()
    assert updates.noted({})["update_available"]["sunset"] == "2027-03-01"


def test_cli_prints_the_sunset_under_update_available(no_sunset_seen, monkeypatch, capsys):
    from leanwarp_cloud import cli

    monkeypatch.setattr(
        cli, "load_client", lambda *, interface: scripted([SUNSET], interface=interface)[0]
    )
    assert cli.main(["account"]) == 0
    assert json.loads(capsys.readouterr().out)["update_available"]["sunset"] == "2027-03-01"


@pytest.mark.parametrize("value", ["", "x" * 41, "2027\r\nInjected: 1"])
def test_a_malformed_sunset_is_ignored(no_sunset_seen, value):
    cloud, _ = scripted([httpx.Response(200, json={}, headers={"LeanWarp-SDK-Sunset": value})])
    with cloud, warnings.catch_warnings():
        warnings.simplefilter("error")
        cloud.versions()
    assert "update_available" not in updates.noted({})


# Argument validation


def _calls():
    from leanwarp_cloud.amounts import usd_spending_cap
    from leanwarp_cloud.project import plan_upload

    cloud = LeanWarpCloud("secret", base_url=BASE, transport=httpx.MockTransport(pytest.fail))
    return [
        lambda: LeanWarpCloud("secret", base_url=BASE, timeout=0),
        lambda: LeanWarpCloud("secret", base_url="http://remote.example"),
        lambda: LeanWarpCloud("secret", base_url=BASE, retries=9),
        lambda: cloud.wait(OPERATION_ID, timeout=0),
        lambda: cloud.operation(OPERATION_ID, wait_seconds=21),
        lambda: cloud.operations("w", limit=0),
        lambda: cloud.check_files("w", [], expected_revision=1),
        lambda: cloud.verify_batch("w", [], expected_revision=1),
        lambda: cloud.feedback("praise", "a summary of it"),  # type: ignore[arg-type]
        lambda: cloud.workspace("a/b"),
        lambda: cloud.create_workspace("b", max_spend_microusd=-1),
        lambda: cloud.submit("w", "check", expected_revision=1, payload={}, timeout_seconds=0),
        lambda: cloud.submit("w", "check", expected_revision=1, payload={"x": float("nan")}),
        lambda: usd_spending_cap("lots"),
        lambda: plan_upload(".", max_file_bytes=0),
    ]


@pytest.mark.parametrize("index", range(15))
def test_every_rejected_argument_is_a_typed_usage_error(index):
    from leanwarp_cloud import UsageError

    with pytest.raises(UsageError) as raised:
        _calls()[index]()
    assert isinstance(raised.value, LeanWarpError) and isinstance(raised.value, ValueError)


def test_service_errors_keep_the_status_code_name_earlier_releases_used():
    error = LeanWarpCloudError(503, "persistence_request_unavailable", "busy")
    assert error.status_code == error.status == 503
    assert pickle.loads(pickle.dumps(error)).status_code == 503  # noqa: S301 - this test's own object


def test_an_unqualified_stop_is_sent_once_when_the_response_is_lost():
    cloud, requests = scripted([httpx.ReadTimeout("lost"), OK_STOP])
    with cloud, pytest.raises(LeanWarpTransportError):
        cloud.stop("w")
    assert len(requests) == 1

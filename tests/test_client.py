from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpCloudError, OperationTimeout


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
        patch("leanwarp_cloud.client.time.monotonic", side_effect=[0, 10]),
        pytest.raises(OperationTimeout) as caught,
    ):
        api.wait("op-1", timeout=1)
    assert caught.value.operation_id == "op-1"


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


def test_project_selects_exact_environment_and_rejects_mismatch(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    manifest = b'{"version":"1.1.0","packages": []}\n'
    (tmp_path / "lake-manifest.json").write_bytes(manifest)
    created: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "versions": [
                        {
                            "bundle_id": "exact",
                            "lean_toolchain": "leanprover/lean4:v4.26.0",
                            "lake_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                        }
                    ]
                },
            )
        created.append(json.loads(request.content))
        return httpx.Response(201, json={"workspace_id": "w"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        assert api.create_workspace_for_project(tmp_path)["workspace_id"] == "w"
        (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
        with pytest.raises(ValueError, match="supported"):
            api.create_workspace_for_project(tmp_path)
    assert len(created) == 1 and created[0]["bundle_id"] == "exact"


def test_project_connects_using_locked_dependencies_not_example_name(tmp_path: Path) -> None:
    from leanwarp_cloud.project import dependency_fingerprint

    example = Path(__file__).parents[1] / "examples/lean-4.26"
    original = (example / "lake-manifest.json").read_bytes()
    (tmp_path / "lean-toolchain").write_bytes((example / "lean-toolchain").read_bytes())
    manifest = json.loads(original)
    manifest["name"] = "my_research_project"
    manifest["packages"].reverse()
    (tmp_path / "lake-manifest.json").write_text(json.dumps(manifest, indent=4))
    selected = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "versions": [
                        {
                            "bundle_id": "qualified",
                            "lean_toolchain": (example / "lean-toolchain").read_text().strip(),
                            "lake_manifest_sha256": hashlib.sha256(original).hexdigest(),
                            "lake_dependencies_sha256": dependency_fingerprint(original),
                        }
                    ]
                },
            )
        selected.append(json.loads(request.content)["bundle_id"])
        return httpx.Response(201, json={"workspace_id": "w"})

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        assert api.create_workspace_for_project(tmp_path)["workspace_id"] == "w"
        manifest["packages"][0]["rev"] = "f" * 40
        (tmp_path / "lake-manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="supported"):
            api.create_workspace_for_project(tmp_path)
    assert selected == ["qualified"]


def test_new_project_uses_latest_matching_build_and_allows_an_older_pin(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    manifest = b'{"version":"1.1.0","packages": []}\n'
    (tmp_path / "lake-manifest.json").write_bytes(manifest)
    matching = {
        "lean_toolchain": "leanprover/lean4:v4.26.0",
        "lake_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    catalog = [
        {**matching, "bundle_id": "old"},
        {**matching, "bundle_id": "new"},
        {**matching, "bundle_id": "other", "lean_toolchain": "leanprover/lean4:v4.27.0"},
    ]
    selected: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"versions": catalog})
        body = json.loads(request.content)
        selected.append(body["bundle_id"])
        return httpx.Response(201, json=body)

    with LeanWarpCloud(
        "secret", base_url="https://cloud.example", transport=httpx.MockTransport(handle)
    ) as api:
        api.create_workspace_for_project(tmp_path)
        api.create_workspace_for_project(tmp_path, bundle_id="old")
        for invalid in ("other", "missing"):
            with pytest.raises(ValueError, match="supported"):
                api.create_workspace_for_project(tmp_path, bundle_id=invalid)
    assert selected == ["new", "old"]


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


@pytest.mark.parametrize("deadline", [0, 601, True, 1.5])
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

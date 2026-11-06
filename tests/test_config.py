from __future__ import annotations

import json

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpCloudError, __version__, config, endpoints
from leanwarp_cloud.cli import parser
from leanwarp_cloud.endpoints import ConfigurationError

TEST_KEY = f"lw_test_{'a' * 32}.{'s' * 43}"
LIVE_KEY = TEST_KEY.replace("lw_test_", "lw_live_")
LEGACY_KEY = TEST_KEY.replace("lw_test_", "lw_")
TEST_ORIGIN = "https://test-api.example"


@pytest.fixture(autouse=True)
def test_service(monkeypatch):
    monkeypatch.setenv(endpoints.TEST_ORIGIN_VARIABLE, TEST_ORIGIN)


def test_environment_key_chooses_exact_origin_without_fallback(monkeypatch):
    monkeypatch.setattr(endpoints, "_LIVE_ORIGIN", "https://production.example")
    seen = []

    def denied(request):
        seen.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})

    for key, origin in (
        (TEST_KEY, TEST_ORIGIN),
        (LIVE_KEY, "https://production.example"),
    ):
        with (
            LeanWarpCloud(key, transport=httpx.MockTransport(denied)) as client,
            pytest.raises(LeanWarpCloudError),
        ):
            client.versions()
        assert seen[-1] == (origin + "/v1/leanwarp/versions", f"Bearer {key}")
    assert len(seen) == 2


@pytest.mark.parametrize("interface", ["python", "cli", "mcp"])
def test_requests_name_the_sdk_release_and_interface(interface):
    seen = []

    def record(request):
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={"versions": []})

    with LeanWarpCloud(
        TEST_KEY, transport=httpx.MockTransport(record), interface=interface
    ) as client:
        client.versions()
    assert seen == [f"leanwarp-sdk/{__version__} ({interface})"]


def test_unknown_interface_is_rejected_before_any_request():
    with pytest.raises(ValueError, match="interface"):
        LeanWarpCloud(TEST_KEY, interface="browser")  # type: ignore[arg-type]


def test_entry_points_name_their_interface(monkeypatch):
    from contextlib import nullcontext

    from leanwarp_cloud import cli, mcp_server

    loaded = []

    class Client(nullcontext):
        def close(self):
            pass

    def load(*, interface):
        loaded.append(interface)
        return Client()

    monkeypatch.setattr(cli, "load_client", load)
    monkeypatch.setattr(cli, "ProjectSession", lambda *_: type("S", (), {"recover": dict})())
    cli.main(["recover"])

    class Server:
        def __init__(self, connect, _root):
            self.connect = connect

        def run(self, *, transport):
            assert transport == "stdio"
            self.connect()

    monkeypatch.setattr(mcp_server, "load_client", load)
    monkeypatch.setattr(mcp_server, "create_server", Server)
    mcp_server.serve("/tmp/project")  # noqa: S108 - never touched; the server is replaced
    assert loaded == ["cli", "mcp"]


def test_environment_key_client_names_its_interface(monkeypatch):
    monkeypatch.setenv("LEANWARP_API_KEY", TEST_KEY)
    with config.load_client(interface="mcp") as client:
        assert client._client.headers["User-Agent"] == f"leanwarp-sdk/{__version__} (mcp)"


def test_released_keys_select_their_fixed_services():
    assert endpoints.api_origin(LIVE_KEY) == "https://api.isagoge.in"
    assert endpoints.api_origin(TEST_KEY) == TEST_ORIGIN
    assert endpoints.api_origin(LEGACY_KEY) == endpoints.api_origin(TEST_KEY)


@pytest.mark.parametrize("origin", ["https://api.isagoge.in", "https://arbitrary.example"])
def test_the_test_origin_never_redirects_a_live_key(monkeypatch, origin):
    monkeypatch.setenv(endpoints.TEST_ORIGIN_VARIABLE, origin)
    assert endpoints.api_origin(LIVE_KEY) == "https://api.isagoge.in"
    assert endpoints.public_origin() == "https://api.isagoge.in"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://localhost:8000/"])
def test_test_keys_may_reach_a_local_service(monkeypatch, origin):
    monkeypatch.setenv(endpoints.TEST_ORIGIN_VARIABLE, origin)
    assert endpoints.api_origin(TEST_KEY) == origin.rstrip("/")


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "http://test-api.example",
        "https://test-api.example/v1",
        "https://user:secret@test-api.example",
        "https://test-api.example?next=1",
        "test-api.example",
        "https://test-api.example:abc",
        "https://test-api.example:99999",
        "https://[::1",
    ],
)
def test_test_keys_need_a_named_https_test_service(monkeypatch, origin):
    if origin is None:
        monkeypatch.delenv(endpoints.TEST_ORIGIN_VARIABLE)
    else:
        monkeypatch.setenv(endpoints.TEST_ORIGIN_VARIABLE, origin)
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("must reject before HTTP"))
    for key in (TEST_KEY, LEGACY_KEY):
        with pytest.raises(ConfigurationError, match=endpoints.TEST_ORIGIN_VARIABLE) as error:
            LeanWarpCloud(key)
        assert key not in str(error.value)
        assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "key", [LIVE_KEY, "lw_unknown_secret", "", "https://arbitrary.example/key"]
)
def test_unconfigured_or_unknown_key_never_constructs_http_client(monkeypatch, key):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unconfigured key must fail before a client is constructed")

    monkeypatch.setattr(endpoints, "_LIVE_ORIGIN", None)
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(ConfigurationError) as error:
        LeanWarpCloud(key)
    if key:
        assert key not in str(error.value)


def test_environment_authentication_needs_only_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LEANWARP_API_KEY", TEST_KEY)
    monkeypatch.setenv("LEANWARP_BASE_URL", "https://arbitrary.example")
    with config.load_client() as client:
        assert client.base_url == TEST_ORIGIN
    assert not (tmp_path / "leanwarp").exists()


def test_login_saves_only_the_key_after_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    seen = []

    def authenticated(request):
        seen.append(request)
        return httpx.Response(200, json={"versions": []})

    monkeypatch.setattr(
        config,
        "LeanWarpCloud",
        lambda key, *, interface: LeanWarpCloud(
            key, transport=httpx.MockTransport(authenticated), interface=interface
        ),
    )
    config.save_credentials(TEST_KEY)
    path = config.credentials_path()
    assert json.loads(path.read_text()) == {"api_key": TEST_KEY}
    assert path.stat().st_mode & 0o077 == 0
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert seen[0].headers["User-Agent"] == f"leanwarp-sdk/{__version__} (cli)"
    with config.load_client() as client:
        assert client.base_url == TEST_ORIGIN
    assert parser().parse_args(["auth", "login"]).auth_command == "login"


def test_failed_login_preserves_previous_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.credentials_path()
    path.parent.mkdir()
    before = json.dumps({"api_key": TEST_KEY})
    path.write_text(before)
    path.chmod(0o600)
    monkeypatch.setattr(
        config,
        "LeanWarpCloud",
        lambda key, *, interface: LeanWarpCloud(
            key,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, json={"error": {"code": "unauthorized"}})
            ),
            interface=interface,
        ),
    )
    with pytest.raises(LeanWarpCloudError):
        config.save_credentials(TEST_KEY)
    assert path.read_text() == before


@pytest.mark.parametrize("origin", [None, TEST_ORIGIN])
def test_legacy_staging_credentials_remain_usable_without_url_input(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    path = config.credentials_path()
    path.parent.mkdir()
    data = {"api_key": LEGACY_KEY}
    if origin is not None:
        data["base_url"] = origin
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with config.load_client() as client:
        assert client.base_url == endpoints.api_origin(TEST_KEY)


def test_legacy_saved_origin_cannot_redirect_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LEANWARP_API_KEY", raising=False)
    path = config.credentials_path()
    path.parent.mkdir()
    path.write_text(json.dumps({"api_key": LEGACY_KEY, "base_url": "https://arbitrary.example"}))
    path.chmod(0o600)
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("must reject before HTTP"))
    with pytest.raises(config.SessionError, match="auth login"):
        config.load_client()

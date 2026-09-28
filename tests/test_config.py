from __future__ import annotations

import json

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpCloudError, config, endpoints
from leanwarp_cloud.cli import parser
from leanwarp_cloud.endpoints import ConfigurationError

TEST_KEY = f"lw_test_{'a' * 32}.{'s' * 43}"
LIVE_KEY = TEST_KEY.replace("lw_test_", "lw_live_")
LEGACY_KEY = TEST_KEY.replace("lw_test_", "lw_")


def test_environment_key_chooses_exact_origin_without_fallback(monkeypatch):
    monkeypatch.setitem(endpoints._API_ORIGINS, "live", "https://production.example")
    seen = []

    def denied(request):
        seen.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})

    for key, origin in (
        (TEST_KEY, "https://control-api-staging-3b57.up.railway.app"),
        (LIVE_KEY, "https://production.example"),
    ):
        with (
            LeanWarpCloud(key, transport=httpx.MockTransport(denied)) as client,
            pytest.raises(LeanWarpCloudError),
        ):
            client.versions()
        assert seen[-1] == (origin + "/v1/leanwarp/versions", f"Bearer {key}")
    assert len(seen) == 2


@pytest.mark.parametrize(
    "key", [LIVE_KEY, "lw_unknown_secret", "", "https://arbitrary.example/key"]
)
def test_unconfigured_or_unknown_key_never_constructs_http_client(monkeypatch, key):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unconfigured key must fail before a client is constructed")

    monkeypatch.setitem(endpoints._API_ORIGINS, "live", None)
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
        assert client.base_url == "https://control-api-staging-3b57.up.railway.app"
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
        lambda key: LeanWarpCloud(key, transport=httpx.MockTransport(authenticated)),
    )
    config.save_credentials(TEST_KEY)
    path = config.credentials_path()
    assert json.loads(path.read_text()) == {"api_key": TEST_KEY}
    assert path.stat().st_mode & 0o077 == 0
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Bearer {TEST_KEY}"
    with config.load_client() as client:
        assert client.base_url == "https://control-api-staging-3b57.up.railway.app"
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
        lambda key: LeanWarpCloud(
            key,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, json={"error": {"code": "unauthorized"}})
            ),
        ),
    )
    with pytest.raises(LeanWarpCloudError):
        config.save_credentials(TEST_KEY)
    assert path.read_text() == before


@pytest.mark.parametrize("origin", [None, "https://control-api-staging-3b57.up.railway.app"])
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

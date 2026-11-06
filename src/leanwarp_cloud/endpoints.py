"""Trusted service origins. Keys select an environment, never an arbitrary URL."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

#: Names the test service's origin. Only test keys read it; live keys never do.
TEST_ORIGIN_VARIABLE = "LEANWARP_TEST_API_ORIGIN"

# Each key prefix names exactly one service. Live keys always reach production,
# whatever the environment says. A missing origin disables that environment;
# never substitute another environment for an unavailable service.
_LIVE_ORIGIN: str | None = "https://api.isagoge.in"


class ConfigurationError(ValueError):
    """An actionable configuration failure containing no credential material."""


def api_origin(api_key: str) -> str:
    match = re.fullmatch(r"lw_(?:(test|live)_)?([a-f0-9]{32})\.([A-Za-z0-9_-]{43})", api_key)
    if match is None:
        raise ConfigurationError(
            "this is not a LeanWarp API key; copy the whole key from the LeanWarp dashboard "
            "(it starts with lw_live_)"
        )
    # Before production existed, only staging issued untagged keys. This legacy
    # format always means test; production rejects it. Never probe other origins.
    if (match[1] or "test") == "test":
        return _test_origin()
    if _LIVE_ORIGIN is None:
        raise ConfigurationError("this SDK release cannot use the LeanWarp service for this key")
    return _LIVE_ORIGIN


def public_origin() -> str:
    """The production service, whose environment catalog needs no key."""
    if _LIVE_ORIGIN is None:
        raise ConfigurationError("this SDK release has no public LeanWarp service")
    return _LIVE_ORIGIN


def _test_origin() -> str:
    """The test service is LeanWarp's own; customers' keys are live keys."""
    value = os.environ.get(TEST_ORIGIN_VARIABLE, "")
    if not value:
        raise ConfigurationError(
            f"this is a LeanWarp test key; set {TEST_ORIGIN_VARIABLE} to the test service's "
            "origin, or use a live key (lw_live_) from the LeanWarp dashboard"
        )
    try:
        parsed = urlparse(value)
        parsed.port  # noqa: B018 - raises on a malformed port, which httpx would reject later
    except ValueError:
        parsed = None
    local = parsed is not None and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed is None
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{TEST_ORIGIN_VARIABLE} must be an HTTPS origin without a path, credentials, "
            "query or fragment (HTTP is supported only on localhost)"
        )
    return value.rstrip("/")

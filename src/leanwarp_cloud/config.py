"""Credential input for local tools. Tokens never become command-line arguments."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .client import LeanWarpCloud
from .session import SessionError


def credentials_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "leanwarp"
        / "credentials.json"
    )


def load_client() -> LeanWarpCloud:
    url, key = os.environ.get("LEANWARP_BASE_URL"), os.environ.get("LEANWARP_API_KEY")
    if url is not None or key is not None:
        if not url or not key:
            raise SessionError("set both LEANWARP_BASE_URL and LEANWARP_API_KEY")
        return LeanWarpCloud(url, key)
    path = credentials_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise SessionError(
            "run leanwarp auth login or configure the two environment variables"
        ) from error
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SessionError("credentials must be a regular file readable only by its owner")
        raw = stream.read(8193)
    try:
        data = json.loads(raw) if len(raw) <= 8192 else None
        if not isinstance(data, dict) or set(data) != {"base_url", "api_key"}:
            raise ValueError
        return LeanWarpCloud(data["base_url"], data["api_key"])
    except (ValueError, TypeError, AttributeError) as error:
        raise SessionError("invalid credential file; use auth login to replace it") from error


def save_credentials(base_url: str, api_key: str) -> None:
    # Validate and authenticate before replacing a working configuration.
    with LeanWarpCloud(base_url, api_key) as cloud:
        cloud.versions()
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise SessionError("credential location must not be a symlink")
    fd, name = tempfile.mkstemp(prefix="credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"base_url": base_url.rstrip("/"), "api_key": api_key}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

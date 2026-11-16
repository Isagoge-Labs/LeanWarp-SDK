"""Whether a newer SDK is available, as the service reports on every response,
and how this installation updates itself."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from ._version import __version__

UPDATE_COMMAND = "leanwarp update"
_RELEASE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}")
# A date in the form the service sends: printable, short, and free of header syntax.
_SUNSET = re.compile(r"[0-9A-Za-z][0-9A-Za-z :,+-]{0,39}")
# The newest release any response in this process named.
_latest: list[str] = []
# When the service stops accepting this release, once a response has named a date.
_sunset: list[str] = []
_sunset_lock = threading.Lock()


def release(version: str) -> tuple[tuple[int, ...], bool] | None:
    """An ordering key, or None for a version that is not a release."""
    match = _RELEASE.match(version)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group().split("."))
    return numbers + (0,) * (4 - len(numbers)), match.end() == len(version)


def record(headers: Mapping[str, str], *, warn: bool = False) -> None:
    """Remember what a response says about SDK releases.

    With ``warn`` (the Python library), the first sunset date this process sees
    raises one DeprecationWarning; the CLI and MCP report it in `update_available`.
    """
    latest = headers.get("LeanWarp-SDK-Latest", "")
    if release(latest) is not None and len(latest) <= 32:
        _latest[:] = [latest]
    sunset = headers.get("LeanWarp-SDK-Sunset", "")
    if _SUNSET.fullmatch(sunset) is None:
        return
    with _sunset_lock:
        first = not _sunset
        _sunset[:] = [sunset]
    if first and warn:
        warnings.warn(
            f"LeanWarp stops accepting SDK {__version__} on {sunset}; upgrade the leanwarp package",
            DeprecationWarning,
            # record -> _request -> public client method -> the caller.
            stacklevel=4,
        )


def notice() -> dict[str, Any] | None:
    """What to tell the user when this release is outdated or being retired, else None."""
    current = release(__version__)
    latest = release(_latest[0]) if _latest else None
    newer = current is not None and latest is not None and latest > current
    if not newer and not _sunset:
        return None
    return {
        "current": __version__,
        **({"latest": _latest[0]} if _latest else {}),
        "command": UPDATE_COMMAND,
        **({"sunset": _sunset[0]} if _sunset else {}),
    }


def noted(result: dict[str, Any]) -> dict[str, Any]:
    """The result, with `update_available` added when there is a newer release."""
    update = notice()
    return {**result, "update_available": update} if update is not None else result


def upgrade_command(
    prefix: Path, executable: str, direct_url: str | None, *, has_pip: bool
) -> list[str] | None:
    """How this installation upgrades itself, or None for a development checkout."""
    try:
        editable = bool(json.loads(direct_url or "{}").get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError):
        editable = False
    if editable:
        return None
    if (prefix / "uv-receipt.toml").exists():
        return ["uv", "tool", "upgrade", "leanwarp"]
    if (prefix / "pipx_metadata.json").exists():
        return ["pipx", "upgrade", "leanwarp"]
    if has_pip:
        return [executable, "-m", "pip", "install", "--upgrade", "leanwarp"]
    return ["uv", "pip", "install", "--python", executable, "--upgrade", "leanwarp"]


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        list(command), capture_output=True, text=True, check=False, timeout=600
    )


def update(*, run: Run = _run) -> dict[str, Any]:
    """Upgrade this installation, then refresh the skills `leanwarp setup` installed."""
    try:
        direct_url = distribution("leanwarp").read_text("direct_url.json")
    except PackageNotFoundError:
        direct_url = None
    command = upgrade_command(
        Path(sys.prefix),
        sys.executable,
        direct_url,
        has_pip=importlib.util.find_spec("pip") is not None,
    )
    if command is None:
        return {
            "status": "development_install",
            "message": "this leanwarp runs from a source checkout; update the checkout instead",
        }
    try:
        upgraded = run(command)
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "command": " ".join(command),
            "message": "the upgrade timed out; run the command yourself to finish it",
        }
    except (OSError, subprocess.SubprocessError):
        upgraded = None
    if upgraded is None or upgraded.returncode != 0:
        return {
            "status": "failed",
            "command": " ".join(command),
            "message": "the upgrade did not run; run the command yourself to see why",
        }
    # The new release's own command refreshes the skills, so they match it.
    leanwarp = str(Path(sys.executable).with_name("leanwarp"))
    version, skills, left = "", None, []
    with suppress(OSError, subprocess.SubprocessError):
        version = run([leanwarp, "--version"]).stdout.strip().removeprefix("leanwarp ")
    with suppress(OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        refreshed = run([leanwarp, "setup", "--refresh"])
        if refreshed.returncode == 0:
            entries = json.loads(refreshed.stdout)["agents"]
            skills = [entry["skill"] for entry in entries]
            left = [item for entry in entries for item in entry.get("skill_files_left", [])]
    report: dict[str, Any] = {
        "previous": __version__,
        "version": version or None,
        "command": " ".join(command),
    }
    # Only a confirmed version and refreshed skills count as done.
    if not version or skills is None:
        return {
            **report,
            "status": "skills_not_refreshed",
            "message": "leanwarp was upgraded, but its installed skills were not refreshed; "
            "run `leanwarp setup --refresh`, then restart your agent",
        }
    if left:
        # Setup never writes over a file that is not its own, so these keep old text.
        return {
            **report,
            "status": "skills_partly_refreshed",
            "skills_refreshed": skills,
            "skill_files_left": left,
            "message": "leanwarp was upgraded, but some installed skill files were left as "
            "they are because they are no longer the ones setup wrote; replace or remove "
            "them, run `leanwarp setup --refresh`, then restart your agent",
        }
    return {
        **report,
        "status": "updated" if version != __version__ else "current",
        "skills_refreshed": skills,
        "next": "restart your agent or MCP client so it runs the new version",
    }

"""Short MCP tool results: what an agent acts on first, and a count of what was left out.

A full operation can be a megabyte of diagnostics and per-entry results, more than
an agent should read to learn whether a check passed. The compact form keeps the
verdict, the operation's identity and its errors first, shortens long lists and
text, and records under ``omitted`` each path it shortened with how many entries
or characters it left out. Every path in ``omitted`` is in the full result.

It never changes a status: a result's ``status``, its batch counts and its
``failure`` survive every shortening, so a non-verdict never reads as a verdict.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: The fields that say which operation this is and how it ended.
VERDICT_FIELDS = (
    "success",
    "decided",
    "operation_id",
    "kind",
    "state",
    "revision",
    "error_code",
    "error_message",
)
# Successively tighter (characters per text, entries per list) until it fits.
_LIMITS = ((2000, 20), (600, 8), (200, 3))
_BUDGET_BYTES = 16 * 1024


def summary(operation: Mapping[str, Any]) -> dict[str, Any]:
    """The operation's verdict and identity, without its result."""
    return {field: operation[field] for field in VERDICT_FIELDS if field in operation}


def compact(operation: Mapping[str, Any]) -> dict[str, Any]:
    """The operation's verdict, identity and a shortened result, under 16 KiB."""
    envelope = operation.get("result")
    result = envelope.get("result") if isinstance(envelope, Mapping) else None
    shown = summary(operation)
    if not isinstance(result, Mapping):
        return shown
    counts = _severities(result.get("diagnostics"))
    if counts:
        shown["counts"] = counts
    for text, entries in _LIMITS:
        omitted: dict[str, int] = {}
        shortened = _shorten(result, "result", text, entries, omitted)
        candidate = {**shown, "result": shortened, **({"omitted": omitted} if omitted else {})}
        if len(json.dumps(candidate, ensure_ascii=False)) <= _BUDGET_BYTES:
            return candidate
    # Still too large: keep only the result's own scalar fields, such as its status
    # and counts, and its failure.
    scalars = {
        key: value
        for key, value in result.items()
        if _scalar(value) or (key == "failure" and isinstance(value, Mapping))
    }
    left_out = {
        f"result.{key}": _size(value) for key, value in result.items() if key not in scalars
    }
    return {**shown, "result": scalars, "omitted": left_out}


def _scalar(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, bool | int | float)
        or (isinstance(value, str) and len(value) <= 200)
    )


def _size(value: Any) -> int:
    return len(value) if isinstance(value, str | list | dict) else 1


def _rank(entry: Any) -> int:
    """Lower first: errors, trials that closed the goal, entries that failed or are unfinished."""
    if not isinstance(entry, Mapping):
        return 1
    if "severity" in entry:
        return 0 if entry["severity"] == "error" else 1
    if "outcome" in entry:
        return {"closed_proof": 0, "open_proof_state": 1}.get(str(entry["outcome"]), 2)
    if entry.get("unfinished") is True:
        return 0
    status = entry.get("status")
    return 0 if isinstance(status, str) and status != "ok" else 1


def _severities(diagnostics: Any) -> dict[str, int]:
    if not isinstance(diagnostics, list):
        return {}
    counts: dict[str, int] = {}
    for item in diagnostics:
        severity = item.get("severity") if isinstance(item, Mapping) else None
        if isinstance(severity, str):
            counts[severity] = counts.get(severity, 0) + 1
    return counts


def _shorten(value: Any, path: str, text: int, entries: int, omitted: dict[str, int]) -> Any:
    if isinstance(value, str):
        if len(value) <= text:
            return value
        omitted[path] = omitted.get(path, 0) + len(value) - text
        return value[:text] + "…"
    if isinstance(value, Mapping):
        return {
            key: _shorten(item, f"{path}.{key}", text, entries, omitted)
            for key, item in value.items()
        }
    if isinstance(value, list):
        # Diagnostics always lead with errors. Other lists keep their order unless
        # shortened, when what an agent acts on comes first.
        if path.endswith("diagnostics") or len(value) > entries:
            value = sorted(value, key=_rank)
        if len(value) > entries:
            omitted[path] = omitted.get(path, 0) + len(value) - entries
        nested = max(1, entries // 4)
        return [_shorten(item, f"{path}[]", text, nested, omitted) for item in value[:entries]]
    return value

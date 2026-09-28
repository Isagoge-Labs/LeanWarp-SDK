"""Agent-friendly JSON CLI; stdin/stdout credentials never pass through the model."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx

from .client import LeanWarpCloudError, OperationTimeout
from .config import credentials_path, load_client, save_credentials
from .outcome import OperationOutcome
from .project import project_environment
from .session import ProjectSession, SessionError


def _dollars(value: str) -> int:
    try:
        amount = Decimal(value) * 1_000_000
        if (
            not amount.is_finite()
            or amount != amount.to_integral_value()
            or not 0 <= amount <= 10**12
        ):
            raise ValueError
        return int(amount)
    except (ValueError, InvalidOperation) as error:
        raise argparse.ArgumentTypeError(
            "use a nonnegative USD amount with at most six decimals"
        ) from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="leanwarp",
        description="Hosted LeanWarp project tools. Outputs JSON; credentials stay local.",
    )
    root.add_argument(
        "--project", default=".", help="local project root (default: current directory)"
    )
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="configure or remove local credentials")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", help="read the API key from a hidden prompt")
    login.add_argument("--base-url", required=True, help="API origin; no /api or /v1 suffix")
    auth_commands.add_parser(
        "logout", help="remove locally saved credentials (does not revoke the key)"
    )
    for name in (
        "doctor",
        "account",
        "versions",
        "resources",
        "status",
        "sync",
        "recover",
        "stop",
        "cancel",
        "disconnect",
        "skill",
        "mcp",
    ):
        commands.add_parser(name)
    connect = commands.add_parser("connect", help="connect once; does not allocate compute")
    connect.add_argument(
        "--max-spend", type=_dollars, required=True, help="approved workspace ceiling in USD"
    )
    connect.add_argument("--profile", default="standard")
    connect.add_argument("--max-profile", default="standard")
    connect.add_argument("--bundle")
    wait = commands.add_parser("wait", help="poll the saved operation; timeout does not cancel it")
    wait.add_argument("--timeout", type=float, default=300)
    check = commands.add_parser("check", help="sync and strictly check a source file")
    check.add_argument("file")
    inspect = commands.add_parser("inspect", help="inspect one-based source coordinates")
    inspect.add_argument("file")
    inspect.add_argument("--line", type=int, required=True)
    inspect.add_argument("--column", type=int, required=True)
    tactics = commands.add_parser(
        "try-tactics", help="try candidate tactics without editing the local file"
    )
    tactics.add_argument("file")
    tactics.add_argument("--line", type=int, required=True)
    tactics.add_argument("--column", type=int, required=True)
    tactics.add_argument("--tactic", action="append", required=True)
    verify = commands.add_parser("verify", help="verify against an explicit fixed target")
    verify.add_argument("file")
    verify.add_argument("--declaration", required=True)
    verify.add_argument(
        "--target", required=True, help="fixed Lean proposition approved by the user"
    )
    verify.add_argument(
        "--context-file", type=Path, help="UTF-8 Lean imports/definitions for the fixed target"
    )
    verify.add_argument("--fresh", action="store_true")
    for operation in (check, inspect, tactics, verify):
        operation.add_argument("--profile")
        operation.add_argument("--execution-timeout", type=int)
    return root


def _emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "skill":
            sys.stdout.write(
                files("leanwarp_cloud").joinpath("skills/leanwarp/SKILL.md").read_text()
            )
            return 0
        if args.command == "mcp":
            from .mcp_server import serve

            serve(args.project)
            return 0
        if args.command == "auth":
            if args.auth_command == "logout":
                credentials_path().unlink(missing_ok=True)
                _emit({"status": "local_credentials_removed", "key_revoked": False})
            else:
                if not sys.stdin.isatty():
                    raise SessionError(
                        "auth login requires a terminal; use environment variables for automation"
                    )
                save_credentials(args.base_url, getpass.getpass("LeanWarp API key (hidden): "))
                _emit({"status": "authenticated", "base_url": args.base_url})
            return 0
        with load_client() as cloud:
            project = ProjectSession(cloud, args.project)
            if args.command in {"account", "versions", "resources"}:
                result = getattr(cloud, args.command)()
            elif args.command == "doctor":
                toolchain, manifest = project_environment(args.project)
                catalog = cloud.versions()["versions"]
                matches = [
                    b
                    for b in catalog
                    if b["lean_toolchain"] == toolchain and b["lake_manifest_sha256"] == manifest
                ]
                result = {
                    "compatible": bool(matches),
                    "matching_bundles": matches,
                    "resources": cloud.resources()["resources"],
                    "base_url": cloud.base_url,
                }
                _emit(result)
                return 0 if matches else 1
            elif args.command == "connect":
                result = project.connect(
                    max_spend_microusd=args.max_spend,
                    resource_profile=args.profile,
                    max_resource_profile=args.max_profile,
                    bundle_id=args.bundle,
                )
            elif args.command == "wait":
                result = project.wait(timeout=args.timeout)
            elif args.command in {"status", "sync", "recover", "stop", "cancel", "disconnect"}:
                result = getattr(project, args.command)()
            else:
                payload: dict[str, Any] = {"file": args.file}
                kind = args.command.replace("-", "_")
                if kind == "check":
                    payload["strict"] = True
                elif kind in {"inspect", "try_tactics"}:
                    payload.update(line=args.line, column=args.column)
                    if kind == "try_tactics":
                        payload["tactics"] = args.tactic
                else:
                    kind = "verify_target"
                    context = ""
                    if args.context_file:
                        with args.context_file.open("rb") as stream:
                            raw = stream.read(128 * 1024 + 1)
                        if len(raw) > 128 * 1024:
                            raise ValueError("target context is too large")
                        context = raw.decode("utf-8")
                    payload.update(
                        candidate_declaration=args.declaration,
                        target_statement=args.target,
                        target_context=context,
                        execution_mode="fresh" if args.fresh else "reusable",
                    )
                result = project.submit(
                    kind,
                    payload,
                    resource_profile=args.profile,
                    timeout_seconds=args.execution_timeout,
                )
            _emit(result)
            if "operation_id" in result and OperationOutcome(result).terminal:
                return 0 if OperationOutcome(result).successful else 1
            return 0
    except OperationTimeout as error:
        _emit({"error": "poll_timeout", "operation_id": error.operation_id, "running": True})
        return 3
    except LeanWarpCloudError as error:
        _emit({"error": error.code, "http_status": error.status_code})
        return 2
    except httpx.TransportError:
        _emit(
            {
                "error": "transport_error",
                "action": "recover any pending request; do not resubmit it",
            }
        )
        return 2
    except (SessionError, ValueError, OSError) as error:
        # Do not print HTTP exception representations or credential-file contents.
        _emit(
            {
                "error": "local_error",
                "message": str(error)
                if isinstance(error, SessionError)
                else "invalid local input or filesystem access",
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

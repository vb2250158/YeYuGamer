"""Command-line client for the typed YeYu Gamer Manager API."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
import webbrowser
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent_client import AgentManagerClient, DEFAULT_AGENT_WORKER_ID
from .api_client import ManagerApiClient, ManagerApiError
from .config import PlatformConfig
from .process_control import LocalManagerController
from .tray_ipc import send_tray_command


def _windows_known_folder(csidl: int) -> Path:
    """Read a Windows known folder without trusting mutable environment paths."""

    if os.name != "nt":
        raise RuntimeError("the installed desktop entry is only available on Windows")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError(f"Windows known-folder lookup failed for CSIDL {csidl:#x}")
    return Path(buffer.value)


def _request_desktop_host_open_webgui(
    config: PlatformConfig, *, manager_healthy: bool | None = None
) -> dict[str, object]:
    """Open the WebGUI without ever spawning a second Manager.

    When a Manager already answers ``/health`` (whatever entry point started
    it) the browser is simply pointed at the running page.  Only an absent
    Manager launches the same Start Menu entry that a user double-clicks.
    Explorer is the persistent Windows broker there: starting the retired
    pythonw tray chain from this short-lived CLI would let a caller job reclaim
    the Manager as soon as the CLI exits.
    """

    if manager_healthy is None:
        manager_healthy = LocalManagerController(config, actor="cli").is_healthy()
    if manager_healthy:
        webbrowser.open(config.web_url)
        return {"requested": True, "via": "browser", "managerHealthy": True}

    desktop_host = config.install_root / "app" / "desktop-host" / "YeYuGamer.exe"
    if not desktop_host.is_file():
        raise RuntimeError("installed YeYuGamer.exe is unavailable; repair YeYu Gamer")
    shortcut = (
        _windows_known_folder(0x0002)  # CSIDL_PROGRAMS
        / "YeYu Gamer"
        / "YeYu Gamer.lnk"
    )
    if not shortcut.is_file():
        raise RuntimeError("the YeYu Gamer Start Menu shortcut is missing; repair YeYu Gamer")
    explorer = _windows_known_folder(0x0024) / "explorer.exe"  # CSIDL_WINDOWS
    if not explorer.is_file():
        raise RuntimeError("Windows Explorer is unavailable")
    subprocess.Popen(
        [str(explorer), str(shortcut)],
        cwd=str(config.install_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"requested": True, "via": "desktop-host-shortcut", "managerHealthy": False}


def _request_tray_exit(config: PlatformConfig) -> dict[str, object]:
    """Ask the authenticated primary tray to leave its Qt event loop."""

    return send_tray_command(
        config,
        "exit",
        timeout_seconds=min(
            30.0,
            max(5.0, float(config.startup_timeout_seconds) + 5.0),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yeyu-gamer",
        description="Typed local client for YeYu Gamer Manager",
    )
    parser.add_argument("--config", help="Path to platform.json")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("health", "meta", "snapshot", "games", "work-items", "capabilities"):
        subparsers.add_parser(name)

    start_daily = subparsers.add_parser("start-daily")
    _mutation_options(start_daily)

    run_game = subparsers.add_parser("run-game")
    run_game.add_argument("game_id")
    _mutation_options(run_game)

    cancel_batch = subparsers.add_parser("cancel-batch")
    cancel_batch.add_argument("batch_id")
    _mutation_options(cancel_batch)

    resume_batch = subparsers.add_parser("resume-batch")
    resume_batch.add_argument("batch_id")
    resume_batch.add_argument("--reason", default="operator_request")
    _mutation_options(resume_batch)

    invoke = subparsers.add_parser("invoke-capability")
    invoke.add_argument("capability_ref")
    invoke.add_argument("--input-json", default="{}")
    _mutation_options(invoke)

    agent_decision = subparsers.add_parser(
        "agent-submit-decision",
        help="Submit a typed claim decision from JSON without exposing the fencing token in process arguments",
    )
    agent_decision.add_argument("--worker-id", default=DEFAULT_AGENT_WORKER_ID)
    agent_decision.add_argument(
        "--request-file",
        required=True,
        help="UTF-8 JSON file, or '-' to read the decision from stdin",
    )
    _mutation_options(agent_decision)

    command_status = subparsers.add_parser("command-status")
    command_status.add_argument("command_id")

    manager_start = subparsers.add_parser("manager-start")
    manager_start.add_argument("--no-wait", action="store_true")

    manager_stop = subparsers.add_parser("manager-stop")
    _mutation_options(manager_stop)

    manager_restart = subparsers.add_parser("manager-restart")
    _mutation_options(manager_restart)

    subparsers.add_parser("open-webgui")
    subparsers.add_parser("open-logs")
    subparsers.add_parser("tray-exit")

    legacy = subparsers.add_parser(
        "legacy",
        help="Strict migration bridge used only by legacy wrapper templates",
    )
    legacy.add_argument("legacy_args", nargs=argparse.REMAINDER)
    return parser


def _mutation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key")
    parser.add_argument("--expected-state-version", type=int)


def _idempotency_key(value: str | None, action: str) -> str:
    return value or f"cli-{action}-{uuid.uuid4()}"


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _configure_utf8_stdio() -> None:
    """Keep CLI text and JSON machine-readable under Windows legacy code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            # Some embedded or test streams cannot be reconfigured.  They
            # already own their encoding policy, so leave them untouched.
            continue


def _write_result(value: Any, *, json_output: bool) -> None:
    value = _serialize(value)
    if json_output or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


def _open_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    webbrowser.open(path.resolve().as_uri())


def _agent_decision_payload(request_file: str) -> Mapping[str, Any]:
    raw = sys.stdin.read() if request_file == "-" else Path(request_file).read_text(
        encoding="utf-8"
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Agent decision JSON must decode to an object")
    allowed = {
        "claimId",
        "fencingToken",
        "decision",
        "reason",
        "evidenceIds",
        "todoDiagnoses",
        "todoDiagnosis",
        "completionReview",
    }
    required = {"claimId", "fencingToken", "decision", "reason"}
    if set(parsed) - allowed or not required.issubset(parsed):
        raise ValueError("Agent decision JSON has an invalid typed shape")
    if not all(isinstance(parsed[key], str) for key in required):
        raise ValueError(
            "Agent decision claimId, fencingToken, decision, and reason must be strings"
        )
    if "todoDiagnoses" in parsed and "todoDiagnosis" in parsed:
        raise ValueError(
            "Agent decision JSON cannot contain both todoDiagnoses and todoDiagnosis"
        )
    evidence_ids = parsed.get("evidenceIds", [])
    if not isinstance(evidence_ids, list) or not all(
        isinstance(value, str) for value in evidence_ids
    ):
        raise ValueError("Agent decision evidenceIds must be an array of opaque IDs")
    diagnoses = parsed.get("todoDiagnoses")
    if diagnoses is not None and (
        not isinstance(diagnoses, list)
        or not all(isinstance(value, dict) for value in diagnoses)
    ):
        raise ValueError("Agent decision todoDiagnoses must be an array of objects")
    legacy_diagnosis = parsed.get("todoDiagnosis")
    if legacy_diagnosis is not None and not isinstance(legacy_diagnosis, dict):
        raise ValueError("Agent decision todoDiagnosis must be an object")
    completion_review = parsed.get("completionReview")
    if completion_review is not None and not isinstance(completion_review, dict):
        raise ValueError("Agent decision completionReview must be an object")
    return parsed


def _manager_todo_scope_id(
    snapshot: object, *, game_id: str | None = None
) -> str:
    """Use the Manager-frozen Todo period as the legacy bridge idempotency scope."""

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("todo"), dict):
        raise RuntimeError("Manager snapshot lacks todo scope")
    todo = snapshot["todo"]
    if game_id is None:
        scope = todo.get("scopeFingerprint")
    else:
        games = todo.get("games")
        game = games.get(game_id) if isinstance(games, dict) else None
        scope = game.get("scopeFingerprint") if isinstance(game, dict) else None
    if not isinstance(scope, str) or len(scope) != 64:
        raise RuntimeError("Manager snapshot lacks a frozen Todo scope fingerprint")
    try:
        bytes.fromhex(scope)
    except ValueError as error:
        raise RuntimeError("Manager Todo scope fingerprint is invalid") from error
    return scope[:32]


def _legacy_request(client: ManagerApiClient, arguments: Sequence[str]) -> Any:
    execute = False
    only_game: str | None = None
    index = 0
    while index < len(arguments):
        token = arguments[index]
        lowered = token.lower()
        if lowered == "-execute":
            execute = True
            index += 1
            continue
        if lowered == "-onlygame":
            if index + 1 >= len(arguments):
                raise ValueError("-OnlyGame requires a GameId")
            only_game = arguments[index + 1]
            index += 2
            continue
        raise ValueError(
            f"Unsupported legacy argument {token!r}; use the typed CLI instead"
        )

    if not execute:
        return client.snapshot()
    snapshot = client.snapshot()
    scope_id = _manager_todo_scope_id(snapshot, game_id=only_game)
    if only_game:
        return client.create_game_run(
            only_game,
            idempotency_key=f"legacy-daily-{scope_id}-{only_game.lower()}",
        )
    return client.create_daily_batch(
        idempotency_key=f"legacy-daily-{scope_id}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = _parser().parse_args(argv)
    try:
        config = PlatformConfig.load(args.config)
        if args.command == "agent-submit-decision":
            request = _agent_decision_payload(args.request_file)
            agent_client = AgentManagerClient(config, worker_id=args.worker_id)
            result = agent_client.submit_claim_decision(
                str(request["claimId"]),
                str(request["fencingToken"]),
                str(request["decision"]),
                str(request["reason"]),
                idempotency_key=_idempotency_key(
                    args.idempotency_key, "agent-submit-decision"
                ),
                evidence_ids=tuple(request.get("evidenceIds", [])),
                todo_diagnoses=request.get("todoDiagnoses"),
                todo_diagnosis=request.get("todoDiagnosis"),
                completion_review=request.get("completionReview"),
                expected_state_version=args.expected_state_version,
            )
            _write_result(result, json_output=args.json)
            return 0

        client = ManagerApiClient(config, actor="cli")

        if args.command == "health":
            result = client.health()
        elif args.command == "meta":
            result = client.meta()
        elif args.command == "snapshot":
            result = client.snapshot()
        elif args.command == "games":
            result = client.games()
        elif args.command == "work-items":
            result = client.list_work_items()
        elif args.command == "capabilities":
            result = client.list_capabilities()
        elif args.command == "start-daily":
            result = client.create_daily_batch(
                idempotency_key=_idempotency_key(
                    args.idempotency_key, "start-daily"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "run-game":
            result = client.create_game_run(
                args.game_id,
                idempotency_key=_idempotency_key(
                    args.idempotency_key, f"run-{args.game_id}"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "cancel-batch":
            result = client.cancel_batch(
                args.batch_id,
                idempotency_key=_idempotency_key(
                    args.idempotency_key, f"cancel-{args.batch_id}"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "resume-batch":
            result = client.resume_batch(
                args.batch_id,
                reason=args.reason,
                idempotency_key=_idempotency_key(
                    args.idempotency_key, f"resume-{args.batch_id}"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "invoke-capability":
            capability_input = json.loads(args.input_json)
            if not isinstance(capability_input, dict):
                raise ValueError("--input-json must decode to an object")
            result = client.invoke_capability(
                args.capability_ref,
                capability_input,
                idempotency_key=_idempotency_key(
                    args.idempotency_key, "capability"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "command-status":
            result = client.command(args.command_id)
        elif args.command == "manager-start":
            result = LocalManagerController(config, actor="cli").start(
                wait=not args.no_wait
            )
        elif args.command == "manager-stop":
            result = client.request_safe_stop(
                idempotency_key=_idempotency_key(
                    args.idempotency_key, "manager-stop"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "manager-restart":
            result = client.request_restart(
                idempotency_key=_idempotency_key(
                    args.idempotency_key, "manager-restart"
                ),
                expected_state_version=args.expected_state_version,
            )
        elif args.command == "open-webgui":
            result = _request_desktop_host_open_webgui(config)
        elif args.command == "open-logs":
            _open_directory(config.log_directory)
            result = {"opened": True, "path": str(config.log_directory)}
        elif args.command == "tray-exit":
            result = _request_tray_exit(config)
        elif args.command == "legacy":
            result = _legacy_request(client, args.legacy_args)
        else:  # pragma: no cover - argparse guarantees a known command
            raise AssertionError(args.command)

        _write_result(result, json_output=args.json)
        if args.command == "health" and not result.ok:
            return 2
        if args.command == "manager-start" and not result.healthy:
            return 2
        return 0
    except json.JSONDecodeError as error:
        print(f"YeYu Gamer: invalid JSON input: {error}", file=sys.stderr)
        return 2
    except (ManagerApiError, ValueError, FileNotFoundError, RuntimeError) as error:
        print(f"YeYu Gamer: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Authenticated current-user IPC for the lightweight tray.

The endpoint is loopback-only.  Its 256-bit authentication key lives only in
an endpoint record below the current user's local application tree; on Windows
that record receives a protected DACL for the current user and LocalSystem.
Wire messages are size-bounded JSON and every command uses a fresh HMAC
challenge, so this channel never deserializes pickle or trusts a bare port.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PlatformConfig


IPC_VERSION = 2
IPC_HOST = "127.0.0.1"
IPC_COMMANDS = frozenset({"ensure_manager", "open_webgui", "exit", "ping"})
MAX_WIRE_BYTES = 4_096
MAX_ENDPOINT_BYTES = 4_096
DEFAULT_CLIENT_TIMEOUT_SECONDS = 3.0


class TrayIpcError(RuntimeError):
    """A bounded tray IPC failure with a stable command-line exit code."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _current_user_identity() -> str:
    if os.name != "nt":
        return f"uid:{os.getuid()}"

    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        if required.value <= 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            sid_pointer, ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _set_current_user_acl(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return

    from ctypes import wintypes

    sid = _current_user_identity()
    inheritance = "OICI" if directory else ""
    sddl = (
        f"D:P(A;{inheritance};FA;;;SY)"
        f"(A;{inheritance};FA;;;{sid})"
    )
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    security_descriptor = ctypes.c_void_p()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(security_descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi32.SetFileSecurityW(
            str(path),
            dacl_security_information | protected_dacl_security_information,
            security_descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(security_descriptor)


def _windows_dacl_sddl(path: Path) -> str:
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    required = wintypes.DWORD()
    advapi32.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetFileSecurityW(
        str(path), dacl_security_information, None, 0, ctypes.byref(required)
    )
    if required.value <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(required.value)
    if not advapi32.GetFileSecurityW(
        str(path),
        dacl_security_information,
        descriptor,
        required,
        ctypes.byref(required),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor,
        1,
        dacl_security_information,
        ctypes.byref(text),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(text)


def _assert_current_user_acl(path: Path) -> None:
    if os.name != "nt":
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise TrayIpcError(
                "tray IPC endpoint permissions are not current-user-only",
                exit_code=4,
            )
        return
    sddl = _windows_dacl_sddl(path)
    owner_sid = _current_user_identity()
    # We create exactly two allow ACEs: LocalSystem and the current user.  A
    # protected DACL prevents inherited Everyone/Users entries from reappearing.
    if not sddl.startswith("D:P") or sddl.count("(A;") != 2:
        raise TrayIpcError(
            "tray IPC endpoint ACL is not the protected product ACL",
            exit_code=4,
        )
    trustees = []
    for ace in sddl.split("(")[1:]:
        if ";;;" not in ace or ")" not in ace:
            raise TrayIpcError("tray IPC endpoint ACL is malformed", exit_code=4)
        trustees.append(ace.split(";;;", 1)[1].split(")", 1)[0])
    if set(trustees) != {"SY", owner_sid}:
        raise TrayIpcError(
            "tray IPC endpoint grants an unexpected principal", exit_code=4
        )


def endpoint_record_path(config: PlatformConfig) -> Path:
    # install_root is fixed below the current user's LocalAppData in production.
    # Keeping mutable IPC state beside (not inside) the swappable installation
    # prevents ProgramData users from reading the channel credential.
    return config.install_root.parent / ".yeyu-gamer-user-state" / "tray-ipc.json"


def _is_reparse(path: Path, result: os.stat_result | None = None) -> bool:
    value = result or os.lstat(path)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & 0x0400
    )


def _assert_endpoint_path(path: Path, *, must_exist: bool) -> None:
    ancestor = path.parent
    while True:
        try:
            ancestor_result = os.lstat(ancestor)
        except FileNotFoundError:
            pass
        else:
            if _is_reparse(ancestor, ancestor_result):
                raise TrayIpcError(
                    "tray IPC endpoint has a reparse-point ancestor", exit_code=4
                )
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    candidates = (path.parent, path) if must_exist else (path.parent,)
    for candidate in candidates:
        try:
            result = os.lstat(candidate)
        except FileNotFoundError:
            if must_exist or candidate == path.parent:
                raise
            continue
        if _is_reparse(candidate, result):
            raise TrayIpcError("tray IPC endpoint uses a reparse point", exit_code=4)
    if must_exist:
        result = os.lstat(path)
        if not stat.S_ISREG(result.st_mode):
            raise TrayIpcError("tray IPC endpoint is not a regular file", exit_code=4)


def _write_endpoint_record(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_endpoint_path(path, must_exist=False)
    if path.exists() or path.is_symlink():
        _assert_endpoint_path(path, must_exist=True)
    _set_current_user_acl(path.parent, directory=True)
    temporary = path.with_name(f".{path.name}.{record['instanceId']}.tmp")
    payload = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(payload) > MAX_ENDPOINT_BYTES:
        raise ValueError("tray IPC endpoint record is too large")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        _set_current_user_acl(temporary, directory=False)
        os.replace(temporary, path)
        _set_current_user_acl(path, directory=False)
        _assert_current_user_acl(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_endpoint_record(path: Path) -> dict[str, Any]:
    _assert_endpoint_path(path, must_exist=True)
    _assert_current_user_acl(path)
    before = os.lstat(path)
    if before.st_size <= 0 or before.st_size > MAX_ENDPOINT_BYTES:
        raise TrayIpcError("tray IPC endpoint has an invalid size", exit_code=4)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise TrayIpcError("tray IPC endpoint changed while opening", exit_code=4)
        payload = os.read(descriptor, MAX_ENDPOINT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_ENDPOINT_BYTES:
        raise TrayIpcError("tray IPC endpoint exceeds its size bound", exit_code=4)
    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrayIpcError("tray IPC endpoint is invalid", exit_code=4) from error
    required = {
        "schemaVersion",
        "instanceId",
        "pid",
        "ownerIdentity",
        "host",
        "port",
        "authKey",
        "createdAt",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise TrayIpcError("tray IPC endpoint schema is invalid", exit_code=4)
    try:
        uuid.UUID(str(record["instanceId"]))
        pid = int(record["pid"])
        port = int(record["port"])
        schema_version = int(record["schemaVersion"])
        auth_key = base64.b64decode(str(record["authKey"]), validate=True)
    except (ValueError, TypeError) as error:
        raise TrayIpcError("tray IPC endpoint values are invalid", exit_code=4) from error
    if (
        schema_version != IPC_VERSION
        or pid <= 0
        or record["ownerIdentity"] != _current_user_identity()
        or record["host"] != IPC_HOST
        or not 1 <= port <= 65535
        or len(auth_key) != 32
    ):
        raise TrayIpcError("tray IPC endpoint identity is invalid", exit_code=4)
    record["pid"] = pid
    record["port"] = port
    record["authKeyBytes"] = auth_key
    return record


def _canonical(parts: Sequence[str]) -> bytes:
    return "\x1f".join(parts).encode("utf-8")


def _mac(key: bytes, parts: Sequence[str]) -> str:
    return hmac.new(key, _canonical(parts), hashlib.sha256).hexdigest()


def _send_json_line(stream: socket.socket, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    ) + b"\n"
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError("tray IPC message exceeds its size bound")
    stream.sendall(payload)


def _receive_json_line(stream: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= MAX_WIRE_BYTES:
        block = stream.recv(min(1024, MAX_WIRE_BYTES + 1 - len(chunks)))
        if not block:
            raise ValueError("tray IPC peer closed before a complete message")
        chunks.extend(block)
        newline = chunks.find(b"\n")
        if newline >= 0:
            if newline != len(chunks) - 1:
                raise ValueError("tray IPC message contains trailing bytes")
            try:
                value = json.loads(bytes(chunks[:newline]).decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("tray IPC message is invalid JSON") from error
            if not isinstance(value, dict):
                raise ValueError("tray IPC message root must be an object")
            return value
    raise ValueError("tray IPC message exceeds its size bound")


class TrayIpcServer:
    def __init__(
        self,
        config: PlatformConfig,
        command_handler: Callable[[str], Mapping[str, str] | None],
        *,
        socket_timeout_seconds: float = 1.0,
    ) -> None:
        self.config = config
        self.command_handler = command_handler
        self.socket_timeout_seconds = socket_timeout_seconds
        self.instance_id = str(uuid.uuid4())
        self.auth_key = secrets.token_bytes(32)
        self.record_path = endpoint_record_path(config)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("tray IPC server is already started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind((IPC_HOST, 0))
            listener.listen(8)
            listener.settimeout(0.25)
            port = int(listener.getsockname()[1])
            record = {
                "schemaVersion": IPC_VERSION,
                "instanceId": self.instance_id,
                "pid": os.getpid(),
                "ownerIdentity": _current_user_identity(),
                "host": IPC_HOST,
                "port": port,
                "authKey": base64.b64encode(self.auth_key).decode("ascii"),
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            _write_endpoint_record(self.record_path, record)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve, name="yeyu-gamer-tray-ipc", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                continue
            with connection:
                connection.settimeout(self.socket_timeout_seconds)
                try:
                    self._handle_connection(connection)
                except (OSError, ValueError):
                    # Authentication/protocol failures are deliberately silent:
                    # neither the endpoint secret nor attacker data reaches logs.
                    continue

    def _handle_connection(self, connection: socket.socket) -> None:
        challenge = secrets.token_urlsafe(24)
        _send_json_line(
            connection,
            {
                "version": IPC_VERSION,
                "instanceId": self.instance_id,
                "challenge": challenge,
            },
        )
        request = _receive_json_line(connection)
        required = {"version", "requestId", "clientNonce", "command", "mac"}
        if set(request) != required:
            raise ValueError("invalid tray IPC request schema")
        version = int(request["version"])
        request_id = str(request["requestId"])
        client_nonce = str(request["clientNonce"])
        command = str(request["command"])
        supplied_mac = str(request["mac"])
        try:
            uuid.UUID(request_id)
        except ValueError as error:
            raise ValueError("invalid tray IPC request id") from error
        if version != IPC_VERSION or command not in IPC_COMMANDS:
            raise ValueError("invalid tray IPC command")
        expected_mac = _mac(
            self.auth_key,
            (self.instance_id, challenge, request_id, client_nonce, command),
        )
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ValueError("tray IPC authentication failed")
        try:
            command_result = self.command_handler(command)
        except Exception:
            status = "rejected"
            message = "primary tray rejected the command"
        else:
            if command_result is None:
                status = "accepted"
                message = "command accepted by primary tray"
            else:
                if set(command_result) != {"status", "message"}:
                    raise ValueError("invalid tray IPC command result schema")
                status = str(command_result["status"])
                message = str(command_result["message"])
                if status not in {"accepted", "completed", "rejected"}:
                    raise ValueError("invalid tray IPC command result status")
                if not message or len(message) > 240:
                    raise ValueError("invalid tray IPC command result message")
        response_mac = _mac(
            self.auth_key,
            (self.instance_id, request_id, status, message),
        )
        _send_json_line(
            connection,
            {
                "version": IPC_VERSION,
                "requestId": request_id,
                "status": status,
                "message": message,
                "mac": response_mac,
            },
        )

    def stop(self) -> None:
        self._stopping.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        _remove_endpoint_record_if_owned(self.record_path, self.instance_id)


def _remove_endpoint_record_if_owned(path: Path, instance_id: str) -> bool:
    quarantine = path.with_name(f".{path.name}.{instance_id}.cleanup")
    try:
        os.replace(path, quarantine)
    except FileNotFoundError:
        return False
    try:
        try:
            record = json.loads(quarantine.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict) and record.get("instanceId") == instance_id:
            quarantine.unlink(missing_ok=True)
            return True
        if not path.exists():
            os.replace(quarantine, path)
        return False
    finally:
        quarantine.unlink(missing_ok=True)


def send_tray_command(
    config: PlatformConfig,
    command: str,
    *,
    timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    if command not in IPC_COMMANDS:
        raise TrayIpcError("unsupported tray IPC command", exit_code=2)
    deadline = time.monotonic() + timeout_seconds
    endpoint_path = endpoint_record_path(config)
    last_unavailable: Exception | None = None
    while time.monotonic() < deadline:
        try:
            record = _read_endpoint_record(endpoint_path)
            remaining = max(0.1, deadline - time.monotonic())
            with socket.create_connection(
                (record["host"], record["port"]), timeout=min(1.0, remaining)
            ) as connection:
                connection.settimeout(min(1.0, remaining))
                challenge = _receive_json_line(connection)
                if set(challenge) != {"version", "instanceId", "challenge"}:
                    raise TrayIpcError("invalid tray IPC challenge", exit_code=4)
                if (
                    int(challenge["version"]) != IPC_VERSION
                    or challenge["instanceId"] != record["instanceId"]
                ):
                    raise TrayIpcError("tray IPC instance changed", exit_code=4)
                request_id = str(uuid.uuid4())
                client_nonce = secrets.token_urlsafe(18)
                request_mac = _mac(
                    record["authKeyBytes"],
                    (
                        record["instanceId"],
                        str(challenge["challenge"]),
                        request_id,
                        client_nonce,
                        command,
                    ),
                )
                _send_json_line(
                    connection,
                    {
                        "version": IPC_VERSION,
                        "requestId": request_id,
                        "clientNonce": client_nonce,
                        "command": command,
                        "mac": request_mac,
                    },
                )
                # Completion-confirmed lifecycle commands may legitimately run
                # for the Manager startup timeout.  The connect/challenge phase
                # stays short, while only the authenticated final response uses
                # the caller's remaining bounded deadline.
                connection.settimeout(max(0.1, deadline - time.monotonic()))
                response = _receive_json_line(connection)
            required = {"version", "requestId", "status", "message", "mac"}
            if set(response) != required or response["requestId"] != request_id:
                raise TrayIpcError("invalid tray IPC response", exit_code=4)
            status = str(response["status"])
            message = str(response["message"])
            expected = _mac(
                record["authKeyBytes"],
                (record["instanceId"], request_id, status, message),
            )
            if not hmac.compare_digest(str(response["mac"]), expected):
                raise TrayIpcError("tray IPC response authentication failed", exit_code=4)
            if status == "accepted" and command != "exit":
                raise TrayIpcError(
                    f"primary tray accepted {command} without reporting completion",
                    exit_code=4,
                )
            if status not in {"accepted", "completed"}:
                raise TrayIpcError(message, exit_code=4)
            return response
        except FileNotFoundError as error:
            last_unavailable = error
        except (ConnectionError, TimeoutError, socket.timeout, OSError) as error:
            last_unavailable = error
        except ValueError as error:
            raise TrayIpcError(
                "tray IPC authentication or wire protocol failed", exit_code=4
            ) from error
        time.sleep(0.05)
    raise TrayIpcError(
        "primary tray IPC is unavailable or timed out", exit_code=3
    ) from last_unavailable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yeyu-gamer-tray-ipc")
    parser.add_argument("--config", help="Path to platform.json")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_CLIENT_TIMEOUT_SECONDS,
        help="Bounded wait for a completion-confirmed tray command",
    )
    parser.add_argument("command", choices=sorted(IPC_COMMANDS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        config = PlatformConfig.load(arguments.config)
        if not 0.1 <= arguments.timeout_seconds <= 300.0:
            raise ValueError("timeout-seconds must be between 0.1 and 300")
        response = send_tray_command(
            config,
            arguments.command,
            timeout_seconds=arguments.timeout_seconds,
        )
    except TrayIpcError as error:
        print(f"YeYu Gamer tray IPC: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, RuntimeError, ValueError) as error:
        print(f"YeYu Gamer tray IPC: {error}", file=sys.stderr)
        return 2
    print(str(response["message"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

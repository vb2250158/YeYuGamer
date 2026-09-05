"""Fixed-root notification secret providers.

Production credentials are a single DPAPI CurrentUser blob.  The provider
returns an in-memory typed profile and never exposes it through Manager models,
SQLite rows, events, receipts, or logs.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class NotificationSecretError(RuntimeError):
    """Safe, classified secret-provider failure without credential detail."""

    code = "secret_invalid"


class NotificationSecretMissing(NotificationSecretError):
    code = "secret_missing"


@dataclass(frozen=True, slots=True)
class SmtpProfile:
    binding_id: str
    host: str = field(repr=False)
    port: int = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    sender_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    security: str = field(default="starttls", repr=False)
    timeout_seconds: int = field(default=20, repr=False)


class NotificationSecretProvider(Protocol):
    def state(self, binding_id: str) -> str: ...

    def load(self, binding_id: str) -> SmtpProfile: ...


class FakeNotificationSecretProvider:
    """Test-only provider; profiles live only in the test process."""

    def __init__(self, profiles: dict[str, SmtpProfile] | None = None) -> None:
        self._profiles = dict(profiles or {})

    def state(self, binding_id: str) -> str:
        return "configured" if binding_id in self._profiles else "missing"

    def load(self, binding_id: str) -> SmtpProfile:
        try:
            return self._profiles[binding_id]
        except KeyError as error:
            raise NotificationSecretMissing("notification binding is not configured") from error


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_unprotect(ciphertext: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise NotificationSecretError("DPAPI notification secrets require Windows")
    encrypted, encrypted_buffer = _blob(ciphertext)
    optional_entropy, entropy_buffer = _blob(entropy)
    decrypted = _DataBlob()
    # Keep buffers alive for the native call.
    _ = encrypted_buffer, entropy_buffer
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_bool
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    succeeded = crypt32.CryptUnprotectData(
        ctypes.byref(encrypted),
        None,
        ctypes.byref(optional_entropy),
        None,
        None,
        0,
        ctypes.byref(decrypted),
    )
    if not succeeded:
        raise NotificationSecretError("notification DPAPI blob cannot be decrypted")
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(decrypted.pbData, ctypes.c_void_p))


class DpapiNotificationSecretProvider:
    PROFILE_FILE = "profile.dpapi"
    ENTROPY = b"YeYuGamer.NotificationSecrets.v1"
    MAX_BLOB_BYTES = 64 * 1024

    def __init__(self, fixed_root: Path) -> None:
        self.fixed_root = fixed_root.resolve()

    @property
    def profile_path(self) -> Path:
        return self.fixed_root / self.PROFILE_FILE

    def state(self, binding_id: str) -> str:
        path = self.profile_path
        if not path.is_file() or path.is_symlink():
            return "missing"
        try:
            self.load(binding_id)
        except NotificationSecretMissing:
            return "missing"
        except NotificationSecretError:
            return "invalid"
        return "configured"

    def load(self, binding_id: str) -> SmtpProfile:
        path = self.profile_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.fixed_root)
        except (FileNotFoundError, ValueError) as error:
            raise NotificationSecretMissing("notification binding is not configured") from error
        if path.is_symlink() or not resolved.is_file():
            raise NotificationSecretError("notification secret path is not a regular file")
        try:
            blob = resolved.read_bytes()
        except OSError as error:
            raise NotificationSecretError(
                "notification secret blob cannot be read"
            ) from error
        if not blob:
            raise NotificationSecretMissing("notification binding is not configured")
        if len(blob) > self.MAX_BLOB_BYTES:
            raise NotificationSecretError("notification secret blob exceeds the fixed limit")
        try:
            payload = json.loads(_dpapi_unprotect(blob, self.ENTROPY).decode("utf-8"))
        except NotificationSecretError:
            raise
        except Exception as error:
            raise NotificationSecretError("notification secret payload is invalid") from error
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            raise NotificationSecretError("notification secret schema is invalid")
        if payload.get("bindingId") != binding_id:
            raise NotificationSecretMissing("notification recipient binding is not configured")
        required = {
            "smtpHost",
            "smtpPort",
            "smtpUsername",
            "smtpPassword",
            "senderAddress",
            "recipientAddress",
        }
        if not required.issubset(payload):
            raise NotificationSecretError("notification secret fields are incomplete")
        security = str(payload.get("security", "starttls"))
        if security not in {"starttls", "tls"}:
            raise NotificationSecretError("notification transport security is invalid")
        port = payload.get("smtpPort")
        timeout = payload.get("timeoutSeconds", 20)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise NotificationSecretError("notification SMTP port is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 5 <= timeout <= 60:
            raise NotificationSecretError("notification timeout is invalid")
        values = {key: payload[key] for key in required if key != "smtpPort"}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise NotificationSecretError("notification secret text fields are invalid")
        return SmtpProfile(
            binding_id=binding_id,
            host=str(payload["smtpHost"]),
            port=port,
            username=str(payload["smtpUsername"]),
            password=str(payload["smtpPassword"]),
            sender_address=str(payload["senderAddress"]),
            recipient_address=str(payload["recipientAddress"]),
            security=security,
            timeout_seconds=timeout,
        )

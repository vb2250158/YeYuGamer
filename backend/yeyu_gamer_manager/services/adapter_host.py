from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..domain.models import GameSummary
from ..store.sqlite_store import RecordNotFound
from .adapter_protocol import (
    MAX_EVENT_BYTES,
    MAX_STDERR_BYTES,
    PROTOCOL_VERSION as ADAPTER_PROTOCOL_VERSION,
    AdapterEvent,
    AdapterEventStream,
    AdapterExecutionPlan,
    AdapterProtocolError,
    AdapterRunResult,
    ExecutionPackageManifest,
    NoExecutableTodos,
    serialize_cancel_control,
    serialize_durable_cancel_control,
    serialize_execute_request,
    validate_plan_against_manifest,
    verify_execution_candidate,
    verify_execution_package,
)
from .legacy_adapter import LegacyAdapter
from .private_runtime_acl import (
    PrivateRuntimeAclError,
    assert_private_runtime_acl,
    set_private_runtime_acl,
)
from ..logging_setup import AttemptLogSession, bind_log_context, get_logger

_log = get_logger("adapter_host")


class ExecutionPackageUnavailable(RuntimeError):
    pass


class AdapterExecutionRejected(ExecutionPackageUnavailable):
    """An execute request was sealed before a Host process could start."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdapterPromotionRejected(RuntimeError):
    """A fixed installed candidate failed a Manager-owned promotion gate."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdapterHostProtocolError(RuntimeError):
    """A fixed Adapter Host package or response failed closed validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ManagerAdapterHost:
    """Manager-owned, fixed-path Adapter Host boundary.

    ``probe`` is a pure filesystem projection. GET endpoints validate the
    installed manifest, containment, size and SHA-256, but never start a
    process. ``canary`` is the only diagnostic operation that launches the
    separately installed Host executable. Its typed result must prove that
    neither an execution Adapter nor a game process was started.

    The Host package and the per-game execution package are different layers:
    installing a healthy Host never makes legacy execution ready.
    """

    HOST_ID = "manager-adapter-host"
    PROTOCOL_VERSION = ADAPTER_PROTOCOL_VERSION
    SUPPORTED_OPERATIONS = ("probe", "canary", "execute")
    HOST_ENTRYPOINT = "host.exe"
    HOST_MANIFEST = "install-manifest.json"
    HOST_PACKAGE_ID = "manager-adapter-host"
    MAX_MANIFEST_BYTES = 64 * 1024
    MAX_STDOUT_BYTES = 64 * 1024
    MAX_STDERR_BYTES = 8 * 1024
    CANARY_TIMEOUT_SECONDS = 10
    CANCEL_GRACE_SECONDS = 5
    CONTROL_AUTHORITY_DIRECTORY = "adapter-control-authorities"
    DURABLE_CONTROL_DIRECTORY = "adapter-controls"
    MAX_CONTROL_FILE_BYTES = MAX_EVENT_BYTES
    MAX_CONTROL_AUTHORITY_BYTES = 4096
    HOST_ENV_ALLOWLIST = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    )

    def __init__(
        self,
        compatibility_adapter: LegacyAdapter,
        *,
        promotion_receipt_resolver: (
            Callable[[str], Mapping[str, Any]] | None
        ) = None,
    ) -> None:
        self.compatibility_adapter = compatibility_adapter
        self.runtime_dir = compatibility_adapter.runtime_dir
        self.host_root = self.runtime_dir / "adapters" / self.HOST_PACKAGE_ID
        self._execution_lock = threading.RLock()
        self._active_process: subprocess.Popen[bytes] | None = None
        self._active_plan: AdapterExecutionPlan | None = None
        self._cancel_sent = False
        self._promotion_receipt_resolver = promotion_receipt_resolver

    @staticmethod
    def adapter_id(game_id: str) -> str:
        return f"legacy-{game_id.lower()}"

    def _game_for_adapter(
        self, adapter_id: str, games: Iterable[GameSummary]
    ) -> GameSummary:
        matches = [
            game for game in games if self.adapter_id(game.game_id) == adapter_id
        ]
        if len(matches) != 1:
            raise RecordNotFound(adapter_id)
        game = matches[0]
        self.compatibility_adapter.validate_game_id(game.game_id)
        return game

    def validate_adapter_id(
        self, adapter_id: str, games: Iterable[GameSummary]
    ) -> GameSummary:
        return self._game_for_adapter(adapter_id, games)

    def _fixed_host_file(self, name: str) -> Path:
        nominal = self.host_root / name
        candidate = nominal.resolve()
        if (
            candidate.parent != self.host_root
            or candidate.name != name
            or not candidate.is_relative_to(self.runtime_dir)
            or nominal.is_symlink()
        ):
            raise AdapterHostProtocolError(
                "unsafe_host_path", "Adapter Host path containment failed"
            )
        return candidate

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _read_verified_host_manifest(self) -> tuple[dict[str, Any], str]:
        if not self.host_root.is_dir():
            raise AdapterHostProtocolError(
                "host_package_missing", "Adapter Host package is not installed"
            )
        if self._is_reparse_point(self.host_root):
            raise AdapterHostProtocolError(
                "unsafe_host_path", "Adapter Host root is a reparse point"
            )
        try:
            entries = list(self.host_root.iterdir())
        except OSError as error:
            raise AdapterHostProtocolError(
                "host_package_unreadable", "Adapter Host package cannot be read"
            ) from error
        expected_names = {self.HOST_ENTRYPOINT, self.HOST_MANIFEST}
        if {entry.name for entry in entries} != expected_names:
            raise AdapterHostProtocolError(
                "host_package_extras",
                "Adapter Host package contains missing or unexpected entries",
            )
        if any(
            not entry.is_file() or self._is_reparse_point(entry)
            for entry in entries
        ):
            raise AdapterHostProtocolError(
                "unsafe_host_path",
                "Adapter Host package entries must be regular non-reparse files",
            )
        entrypoint = self._fixed_host_file(self.HOST_ENTRYPOINT)
        manifest_path = self._fixed_host_file(self.HOST_MANIFEST)
        if not entrypoint.is_file() or not manifest_path.is_file():
            raise AdapterHostProtocolError(
                "host_package_missing", "Adapter Host package is not installed"
            )
        if entrypoint.is_symlink() or manifest_path.is_symlink():
            raise AdapterHostProtocolError(
                "unsafe_host_path", "Adapter Host package contains a link"
            )
        manifest_size = manifest_path.stat().st_size
        if manifest_size <= 0 or manifest_size > self.MAX_MANIFEST_BYTES:
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host manifest size is invalid"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host manifest is not valid JSON"
            ) from error
        if not isinstance(manifest, dict):
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host manifest must be an object"
            )

        required = {
            "schemaVersion",
            "packageId",
            "hostVersion",
            "protocolVersion",
            "entryPoint",
            "sha256",
            "sizeBytes",
            "hostReady",
            "executionReady",
            "supportedGameIds",
            "installedAt",
        }
        if not required.issubset(manifest):
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host manifest fields are incomplete"
            )
        supported = manifest.get("supportedGameIds")
        if (
            manifest.get("schemaVersion") != 1
            or manifest.get("packageId") != self.HOST_PACKAGE_ID
            or manifest.get("protocolVersion") != self.PROTOCOL_VERSION
            or manifest.get("entryPoint") != self.HOST_ENTRYPOINT
            or not isinstance(manifest.get("hostVersion"), str)
            or not manifest.get("hostVersion")
            or manifest.get("hostReady") is not True
            or manifest.get("executionReady") is not False
            or not isinstance(supported, list)
            or not supported
            or any(not isinstance(item, str) or not item for item in supported)
            or len(set(supported)) != len(supported)
        ):
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host manifest contract is invalid"
            )
        expected_hash = manifest.get("sha256")
        expected_size = manifest.get("sizeBytes")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
        ):
            raise AdapterHostProtocolError(
                "invalid_host_manifest", "Adapter Host digest metadata is invalid"
            )
        if entrypoint.stat().st_size != expected_size:
            raise AdapterHostProtocolError(
                "host_size_mismatch", "Adapter Host size differs from its manifest"
            )
        actual_hash = self._sha256(entrypoint)
        if actual_hash != expected_hash:
            raise AdapterHostProtocolError(
                "host_hash_mismatch", "Adapter Host hash differs from its manifest"
            )
        return manifest, actual_hash

    def _host_environment(
        self,
        *,
        staging_dir: Path | None = None,
        installation_binding_path: Path | None = None,
        execution_package_root: Path | None = None,
    ) -> dict[str, str]:
        """Build the complete, minimal environment for Host and runner.

        In particular, no inherited ``YEYU_*`` value, tray bootstrap secret,
        actor credential, SMTP setting, token, or unrelated service credential
        crosses the process boundary. The one Adapter variable is derived from
        Manager-owned local runtime state rather than an API request.
        """
        environment = {
            name: value
            for name in self.HOST_ENV_ALLOWLIST
            if (value := os.environ.get(name)) is not None
        }
        if staging_dir is not None:
            environment["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(staging_dir)
        if installation_binding_path is not None:
            environment["YEYU_GAMER_INSTALLATION_BINDING_PATH"] = str(
                installation_binding_path
            )
        if execution_package_root is not None:
            environment["YEYU_GAMER_EXECUTION_PACKAGE_ROOT"] = str(
                execution_package_root
            )
        return environment

    @staticmethod
    def _write_installation_binding(
        staging_dir: Path,
        *,
        game_id: str,
        installation_binding: Mapping[str, Any],
    ) -> Path:
        """Expose only Manager-owned, typed local paths to a fixed runner.

        This is deliberately an ephemeral, per-attempt document beneath the
        already-contained artifact staging directory.  API callers never pass
        it to the Host: the Manager projects it from its persisted Config
        record after GameId validation.
        """
        allowed = {"gamePath", "toolPath", "dailyTaskProfile", "emulatorBinding"}
        if set(installation_binding) - allowed:
            raise AdapterExecutionRejected(
                "installation_binding_invalid", "Installation binding contains unsupported fields"
            )
        daily_task_profile = installation_binding.get("dailyTaskProfile")
        emulator_binding = installation_binding.get("emulatorBinding")
        if emulator_binding is not None:
            required_emulator_fields = {
                "provider",
                "consolePath",
                "adbPath",
                "instanceIndex",
                "instanceName",
                "adbSerial",
            }
            if (
                game_id not in {"FGO", "BD2", "CZN"}
                or not isinstance(emulator_binding, Mapping)
                or set(emulator_binding) != required_emulator_fields
                or emulator_binding.get("provider") != "ldplayer"
            ):
                raise AdapterExecutionRejected(
                    "installation_binding_invalid",
                    "Android emulator binding is not a complete Manager-owned projection",
                )
        if daily_task_profile is not None:
            required_profile_fields = (
                {
                    "whichToFarm",
                    "tacetSuppressionNumber",
                    "forgeryChallengeNumber",
                    "materialSelection",
                    "farmNightmareNestForDailyEcho",
                }
                if game_id == "WW"
                else {
                    "anomalyTaskType",
                    "expRewardTarget",
                    "materialIndex",
                    "staminaTarget",
                    "autoCycleSubTask",
                    "coffeeMode",
                }
                if game_id == "NTE"
                else {
                    "staminaCategory",
                    "staminaTarget",
                    "battleEfficiency",
                    "untilExhausted",
                }
                if game_id == "CZN"
                else set()
            )
            if (
                not required_profile_fields
                or not isinstance(daily_task_profile, Mapping)
                or set(daily_task_profile) != required_profile_fields
            ):
                raise AdapterExecutionRejected(
                    "installation_binding_invalid",
                    "daily profile is not a complete Manager-owned projection",
                )
        document = {
            "schemaVersion": (
                3
                if emulator_binding is not None
                else 2 if daily_task_profile is not None else 1
            ),
            "gameId": game_id,
            "gamePath": installation_binding.get("gamePath"),
            "toolPath": installation_binding.get("toolPath"),
        }
        if daily_task_profile is not None:
            document["dailyTaskProfile"] = dict(daily_task_profile)
        if emulator_binding is not None:
            document["emulatorBinding"] = dict(emulator_binding)
        target = staging_dir / "installation-binding.json"
        target.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if target.is_symlink() or target.stat().st_size > 4096:
            raise AdapterExecutionRejected(
                "installation_binding_invalid", "Installation binding could not be staged safely"
            )
        return target

    def _artifact_staging_dir(self, plan: AdapterExecutionPlan) -> Path:
        staging_root = self.runtime_dir / "artifact-inbox"
        staging_root.mkdir(parents=True, exist_ok=True)
        if self._is_reparse_point(staging_root):
            raise AdapterHostProtocolError(
                "unsafe_artifact_staging", "Artifact staging root is a reparse point"
            )
        root = staging_root.resolve()
        nominal = staging_root / plan.run_attempt_id
        nominal.mkdir(exist_ok=True)
        candidate = nominal.resolve()
        if (
            candidate.parent != root
            or candidate.name != plan.run_attempt_id
            or self._is_reparse_point(nominal)
        ):
            raise AdapterHostProtocolError(
                "unsafe_artifact_staging", "Artifact staging path containment failed"
            )
        return candidate

    def _private_runtime_file(self, directory: str, leaf: str) -> Path:
        """Resolve one fixed, non-reparse file below the local runtime root."""

        runtime = self.runtime_dir.resolve()
        nominal_root = self.runtime_dir / directory
        nominal_root.mkdir(parents=True, exist_ok=True)
        try:
            set_private_runtime_acl(nominal_root, directory=True)
            assert_private_runtime_acl(nominal_root)
        except (OSError, PrivateRuntimeAclError) as error:
            raise AdapterHostProtocolError(
                "unsafe_control_acl",
                "Adapter control directory ACL is not private",
            ) from error
        root = nominal_root.resolve()
        if (
            root.parent != runtime
            or self._is_reparse_point(nominal_root)
            or not root.is_relative_to(runtime)
        ):
            raise AdapterHostProtocolError(
                "unsafe_control_path", "Adapter control directory containment failed"
            )
        nominal = nominal_root / leaf
        candidate = nominal.resolve()
        if (
            candidate.parent != root
            or candidate.name != leaf
            or not candidate.is_relative_to(runtime)
            or (nominal.exists() and self._is_reparse_point(nominal))
        ):
            raise AdapterHostProtocolError(
                "unsafe_control_path", "Adapter control path containment failed"
            )
        return candidate

    @staticmethod
    def _atomic_write_private_file(target: Path, payload: bytes) -> None:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            try:
                set_private_runtime_acl(target, directory=False)
                assert_private_runtime_acl(target)
            except (OSError, PrivateRuntimeAclError) as error:
                target.unlink(missing_ok=True)
                raise AdapterHostProtocolError(
                    "unsafe_control_acl",
                    "Adapter control file ACL is not private",
                ) from error
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _cancel_authority_hash(authority: str) -> str:
        return "sha256:" + hashlib.sha256(authority.encode("utf-8")).hexdigest()

    def _control_authority_path(self, run_attempt_id: str) -> Path:
        try:
            parsed = uuid.UUID(run_attempt_id)
        except (ValueError, AttributeError) as error:
            raise AdapterHostProtocolError(
                "invalid_control_scope", "Run attempt scope is invalid"
            ) from error
        if str(parsed) != run_attempt_id:
            raise AdapterHostProtocolError(
                "invalid_control_scope", "Run attempt scope is not canonical"
            )
        return self._private_runtime_file(
            self.CONTROL_AUTHORITY_DIRECTORY,
            f"{run_attempt_id}.json",
        )

    def _write_control_authority(self, plan: AdapterExecutionPlan) -> Path:
        target = self._control_authority_path(plan.run_attempt_id)
        document = {
            "schemaVersion": 1,
            "runId": plan.run_id,
            "runAttemptId": plan.run_attempt_id,
            "cancelAuthority": plan.cancel_authority,
            "cancelAuthorityHash": self._cancel_authority_hash(
                plan.cancel_authority
            ),
            "issuedAt": plan.issued_at,
            "expiresAt": plan.expires_at,
            "timeoutSeconds": plan.timeout_seconds,
        }
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.MAX_CONTROL_AUTHORITY_BYTES:
            raise AdapterHostProtocolError(
                "control_authority_too_large", "Cancellation authority is too large"
            )
        self._atomic_write_private_file(target, payload)
        return target

    def _load_control_authority(
        self,
        *,
        run_id: str,
        run_attempt_id: str,
        expected_hash: str,
    ) -> str | None:
        path = self._control_authority_path(run_attempt_id)
        try:
            if (
                not path.is_file()
                or self._is_reparse_point(path)
                or path.stat().st_size < 2
                or path.stat().st_size > self.MAX_CONTROL_AUTHORITY_BYTES
            ):
                return None
            assert_private_runtime_acl(path)
            document = json.loads(path.read_text(encoding="utf-8"))
        except (
            OSError,
            PrivateRuntimeAclError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return None
        if not isinstance(document, dict) or set(document) != {
            "schemaVersion",
            "runId",
            "runAttemptId",
            "cancelAuthority",
            "cancelAuthorityHash",
            "issuedAt",
            "expiresAt",
            "timeoutSeconds",
        }:
            return None
        authority = document.get("cancelAuthority")
        if (
            document.get("schemaVersion") != 1
            or document.get("runId") != run_id
            or document.get("runAttemptId") != run_attempt_id
            or not isinstance(authority, str)
            or not isinstance(expected_hash, str)
            or document.get("cancelAuthorityHash") != expected_hash
            or not hmac.compare_digest(
                self._cancel_authority_hash(authority), expected_hash
            )
        ):
            return None
        try:
            issued = datetime.fromisoformat(
                str(document["issuedAt"]).replace("Z", "+00:00")
            )
            expires = datetime.fromisoformat(
                str(document["expiresAt"]).replace("Z", "+00:00")
            )
            timeout_seconds = int(document["timeoutSeconds"])
            deadline = min(
                expires,
                issued + timedelta(seconds=timeout_seconds),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            issued.tzinfo is None
            or expires.tzinfo is None
            or isinstance(document["timeoutSeconds"], bool)
            or not 1 <= timeout_seconds <= 24 * 60 * 60
            or datetime.now(timezone.utc) > deadline
        ):
            return None
        return authority

    def _durable_control_path(
        self, run_attempt_id: str, *, delivered: bool = False
    ) -> Path:
        suffix = "delivered" if delivered else "cancel"
        return self._private_runtime_file(
            self.DURABLE_CONTROL_DIRECTORY,
            f"{run_attempt_id}.{suffix}.json",
        )

    def _durable_control_is_authentic(
        self,
        path: Path,
        *,
        run_id: str,
        run_attempt_id: str,
        cancel_authority: str,
        maximum_bytes: int,
    ) -> bool:
        try:
            if not path.is_file() or self._is_reparse_point(path):
                return False
            assert_private_runtime_acl(path)
            raw = path.read_bytes()
            if len(raw) < 2 or len(raw) > maximum_bytes:
                return False
            value = json.loads(raw.decode("utf-8"))
        except (
            OSError,
            PrivateRuntimeAclError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return False
        if not isinstance(value, dict) or set(value) != {
            "schemaVersion",
            "protocolVersion",
            "controlType",
            "runId",
            "runAttemptId",
            "at",
            "reasonCode",
            "nonce",
            "authorityMac",
        }:
            return False
        if (
            value.get("schemaVersion") != 1
            or value.get("protocolVersion") != ADAPTER_PROTOCOL_VERSION
            or value.get("controlType") != "cancel"
            or value.get("runId") != run_id
            or value.get("runAttemptId") != run_attempt_id
            or not all(
                isinstance(value.get(field), str) and value.get(field)
                for field in ("at", "reasonCode", "nonce", "authorityMac")
            )
        ):
            return False
        canonical = "\n".join(
            (
                ADAPTER_PROTOCOL_VERSION,
                "cancel",
                run_id,
                run_attempt_id,
                str(value["at"]),
                str(value["reasonCode"]),
                str(value["nonce"]),
            )
        )
        expected = "hmac-sha256:" + hmac.new(
            cancel_authority.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(str(value["authorityMac"]), expected)

    def _write_durable_cancel(
        self,
        *,
        run_id: str,
        run_attempt_id: str,
        cancel_authority: str,
        reason_code: str,
    ) -> bool:
        delivered = self._durable_control_path(run_attempt_id, delivered=True)
        if self._durable_control_is_authentic(
            delivered,
            run_id=run_id,
            run_attempt_id=run_attempt_id,
            cancel_authority=cancel_authority,
            maximum_bytes=self.MAX_CONTROL_FILE_BYTES,
        ):
            return True
        pending = self._durable_control_path(run_attempt_id)
        payload = serialize_durable_cancel_control(
            run_id=run_id,
            run_attempt_id=run_attempt_id,
            cancel_authority=cancel_authority,
            at=datetime.now(timezone.utc).isoformat(),
            reason_code=reason_code,
        )
        self._atomic_write_private_file(pending, payload)
        return True

    def _execution_package_probe(self, game_id: str | None = None) -> dict[str, Any]:
        package_root = (
            self.compatibility_adapter.execution_package_root(game_id)
            if game_id is not None
            else self.compatibility_adapter.adapter_root
        )
        try:
            manifest = self._verify_promoted_package(package_root)
        except AdapterProtocolError as error:
            candidate: ExecutionPackageManifest | None = None
            try:
                candidate = verify_execution_candidate(package_root)
            except AdapterProtocolError:
                pass
            status_by_code = {
                "execution_package_missing": "missing",
                "execution_manifest_missing": "installed-unpromoted",
                "execution_package_unpromoted": "installed-unpromoted",
                "promotion_receipt_missing": "installed-unpromoted",
                "promotion_receipt_mismatch": "installed-unpromoted",
            }
            return {
                "packageId": "legacy-night-rain-gamer",
                "packageVersion": (
                    candidate.package_version if candidate is not None else None
                ),
                "buildId": candidate.build_id if candidate is not None else None,
                "entryPoint": "runner.exe",
                "status": (
                    "installed-unpromoted"
                    if candidate is not None
                    else status_by_code.get(error.code, error.code)
                ),
                "sha256": None,
                "packageDigest": (
                    candidate.package_digest if candidate is not None else None
                ),
                "payloadDigest": (
                    candidate.payload_digest if candidate is not None else None
                ),
                "manifestSchemaVersion": 2 if candidate is not None else None,
                "protocolVersions": (
                    [self.PROTOCOL_VERSION] if candidate is not None else []
                ),
                "supportedGameIds": (
                    list(candidate.supported_game_ids) if candidate is not None else []
                ),
                "operationBindings": (
                    {
                        candidate_game_id: sorted(bindings)
                        for candidate_game_id, bindings in candidate.operation_bindings.items()
                    }
                    if candidate is not None
                    else {}
                ),
            }
        if game_id is not None and game_id not in manifest.supported_game_ids:
            return {
                "packageId": "legacy-night-rain-gamer",
                "packageVersion": None,
                "entryPoint": "runner.exe",
                "status": "missing",
                "sha256": None,
                "manifestSchemaVersion": None,
                "protocolVersions": [],
                "supportedGameIds": [],
                "operationBindings": {},
            }
        return {
            "packageId": "legacy-night-rain-gamer",
            "packageVersion": manifest.package_version,
            "buildId": manifest.build_id,
            "entryPoint": "runner.exe",
            "status": "promoted",
            "sha256": self._sha256(manifest.entry_point),
            "packageDigest": manifest.package_digest,
            "payloadDigest": manifest.payload_digest,
            "promotionReceiptId": manifest.promotion_receipt_id,
            "manifestSchemaVersion": 2,
            "protocolVersions": [self.PROTOCOL_VERSION],
            "supportedGameIds": list(manifest.supported_game_ids),
            "operationBindings": {
                game_id: sorted(bindings)
                for game_id, bindings in manifest.operation_bindings.items()
            },
        }

    def probe(self) -> dict[str, Any]:
        manifest: dict[str, Any] | None = None
        host_hash: str | None = None
        host_status = "ready"
        try:
            manifest, host_hash = self._read_verified_host_manifest()
        except AdapterHostProtocolError as error:
            host_status = error.code

        package = self._execution_package_probe()
        active = self.compatibility_adapter.active_execution()
        host_healthy = manifest is not None
        # The base Host manifest never promotes a runner by itself. Readiness
        # requires an independently verified v2 execution package plus the
        # explicit local execution gate.
        execution_ready = bool(
            host_healthy
            and package["status"] == "promoted"
            and self.compatibility_adapter.execution_enabled
            and not bool(active and not bool(active.get("exited")))
        )
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "supportedOperations": list(self.SUPPORTED_OPERATIONS),
            "hostId": self.HOST_ID,
            "hostVersion": (
                str(manifest["hostVersion"]) if manifest is not None else "unknown"
            ),
            "hostHealthy": host_healthy,
            "hostStatus": host_status,
            "hostManifestVerified": host_healthy,
            "hostEntryPointSha256": host_hash,
            "hostSupportedGameIds": (
                list(manifest["supportedGameIds"])
                if manifest is not None
                else []
            ),
            "managerOwned": True,
            "executionPackage": package,
            "executionGateEnabled": self.compatibility_adapter.execution_enabled,
            "activeExecution": bool(active and not bool(active.get("exited"))),
            "executionReady": execution_ready,
            "arbitraryCommandApi": False,
            "arbitraryPathApi": False,
            "arbitraryClickApi": False,
        }

    def _scoped_probe(self, game_id: str) -> dict[str, Any]:
        """Project an Adapter Host diagnostic for exactly one game module.

        A host-level probe intentionally reports the compatibility package for
        inventory.  A canary is evidence for a concrete Adapter, though, so it
        must use the same fixed module-root resolver as execution.  This keeps
        a successful canary from accidentally authorizing another game's
        package.
        """
        probe = self.probe()
        package = self._execution_package_probe(game_id)
        probe["executionPackage"] = package
        probe["executionReady"] = bool(
            probe["hostHealthy"]
            and probe["executionGateEnabled"]
            and package.get("status") == "promoted"
        )
        return probe

    def probe_game(self, game_id: str) -> dict[str, Any]:
        """Return readiness for one fixed game module, never the shared root."""

        self.compatibility_adapter.validate_game_id(game_id)
        return self._scoped_probe(game_id)

    @staticmethod
    def _decode_json_object(raw: bytes) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
            stripped = text.lstrip()
            value, end = json.JSONDecoder().raw_decode(stripped)
            if stripped[end:].strip():
                raise ValueError("trailing output")
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise AdapterHostProtocolError(
                "invalid_host_response", "Adapter Host returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise AdapterHostProtocolError(
                "invalid_host_response", "Adapter Host response must be an object"
            )
        return value

    def _invoke_host(
        self, operation: str, *, run_id: str, game_id: str
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        probe = self._scoped_probe(game_id)
        if not probe["hostHealthy"]:
            raise AdapterHostProtocolError(
                str(probe["hostStatus"]), "Adapter Host package is not healthy"
            )
        entrypoint = self._fixed_host_file(self.HOST_ENTRYPOINT)
        command = [
            str(entrypoint),
            "--operation",
            operation,
            "--protocol-version",
            self.PROTOCOL_VERSION,
            "--run-id",
            run_id,
            "--game-id",
            game_id,
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=self.host_root,
                env=self._host_environment(
                    execution_package_root=self.compatibility_adapter.execution_package_root(
                        game_id
                    )
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creation_flags,
                timeout=self.CANARY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterHostProtocolError(
                "host_invocation_failed", "Adapter Host invocation failed"
            ) from error
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if len(stdout) > self.MAX_STDOUT_BYTES or len(stderr) > self.MAX_STDERR_BYTES:
            raise AdapterHostProtocolError(
                "host_output_too_large", "Adapter Host output exceeded its limit"
            )
        return completed.returncode, self._decode_json_object(stdout), probe

    @staticmethod
    def _require_exact_bool(result: dict[str, Any], key: str, expected: bool) -> None:
        if result.get(key) is not expected:
            raise AdapterHostProtocolError(
                "invalid_host_response", f"Adapter Host {key} invariant failed"
            )

    def _validate_scoped_response(
        self,
        *,
        result: dict[str, Any],
        probe: dict[str, Any],
        operation: str,
        run_id: str,
        game_id: str,
    ) -> None:
        if (
            result.get("protocolVersion") != self.PROTOCOL_VERSION
            or result.get("hostVersion") != probe["hostVersion"]
            or result.get("operation") != operation
            or result.get("runId") != run_id
            or result.get("gameId") != game_id
            or result.get("entryPointSha256") != probe["hostEntryPointSha256"]
        ):
            raise AdapterHostProtocolError(
                "invalid_host_response", "Adapter Host response scope did not match"
            )
        supported = result.get("supportedGameIds")
        if (
            not isinstance(supported, list)
            or any(not isinstance(item, str) for item in supported)
            or len(set(supported)) != len(supported)
            or set(supported) != set(probe["hostSupportedGameIds"])
            or game_id not in supported
        ):
            raise AdapterHostProtocolError(
                "invalid_host_response", "Adapter Host allowlist did not match"
            )

    def _execute_startup_rejection(
        self,
        raw: bytes,
        *,
        plan: AdapterExecutionPlan,
        probe: dict[str, Any],
    ) -> AdapterProtocolError | None:
        """Recognize only the fixed Host's pre-run rejection response.

        Execute normally emits Adapter JSONL, but the Host's request/package
        gates emit WriteResult before a runner exists. Keep that failure's
        diagnostic without treating it as an Adapter event or completion.
        """
        try:
            result = self._decode_json_object(raw)
        except AdapterHostProtocolError:
            # The Adapter parser owns malformed JSON and ordinary event errors.
            return None
        if "eventType" in result or "operation" not in result:
            return None
        try:
            if set(result) != {
                "protocolVersion", "hostVersion", "operation", "success",
                "code", "message", "runId", "gameId", "hostReady",
                "executionReady", "adapterProcessStarted", "gameProcessStarted",
                "entryPointSha256", "supportedGameIds",
            }:
                raise AdapterHostProtocolError(
                    "invalid_host_response", "Adapter Host rejection schema did not match"
                )
            self._validate_scoped_response(
                result=result, probe=probe, operation="execute",
                run_id=plan.run_id, game_id=plan.game_id,
            )
            self._require_exact_bool(result, "hostReady", True)
            for key in (
                "success", "executionReady", "adapterProcessStarted", "gameProcessStarted"
            ):
                self._require_exact_bool(result, key, False)
            code = result["code"]
            message = result["message"]
            if (
                not isinstance(code, str)
                or code not in {
                    "invalid_request", "missing_execute_request",
                    "execute_request_too_large", "invalid_execute_request",
                    "execution_package_unavailable",
                }
                or not isinstance(message, str)
                or not message.strip()
                or len(message) > 2048
            ):
                raise AdapterHostProtocolError(
                    "invalid_host_response", "Adapter Host rejection diagnostic is invalid"
                )
            return AdapterProtocolError(code, message)
        except AdapterHostProtocolError as error:
            return AdapterProtocolError(error.code, str(error))

    @staticmethod
    def _failed_canary(
        *,
        adapter_id: str,
        game_id: str,
        probe: dict[str, Any],
        error: AdapterHostProtocolError,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            **probe,
            "diagnosticOnly": True,
            "adapterId": adapter_id,
            "gameId": game_id,
            "canaryStatus": "failed",
            "diagnosticCode": error.code,
            "adapterProcessStarted": False,
            "gameProcessStarted": False,
        }

    def canary(
        self, adapter_id: str, games: Iterable[GameSummary]
    ) -> dict[str, Any]:
        game = self._game_for_adapter(adapter_id, games)
        run_id = str(uuid.uuid4())
        probe = self._scoped_probe(game.game_id)
        try:
            exit_code, result, invoked_probe = self._invoke_host(
                "canary", run_id=run_id, game_id=game.game_id
            )
            self._validate_scoped_response(
                result=result,
                probe=invoked_probe,
                operation="canary",
                run_id=run_id,
                game_id=game.game_id,
            )
            if exit_code != 0 or result.get("success") is not True:
                raise AdapterHostProtocolError(
                    "canary_failed", "Adapter Host canary returned failure"
                )
            if result.get("code") != "canary_passed":
                raise AdapterHostProtocolError(
                    "invalid_host_response", "Adapter Host canary code is invalid"
                )
            self._require_exact_bool(result, "hostReady", True)
            self._require_exact_bool(result, "executionReady", False)
            self._require_exact_bool(result, "adapterProcessStarted", False)
            self._require_exact_bool(result, "gameProcessStarted", False)
        except AdapterHostProtocolError as error:
            return self._failed_canary(
                adapter_id=adapter_id,
                game_id=game.game_id,
                probe=probe,
                error=error,
            )

        message = result.get("message")
        return {
            "schemaVersion": 1,
            **invoked_probe,
            "diagnosticOnly": True,
            "adapterId": adapter_id,
            "gameId": game.game_id,
            "canaryRunId": run_id,
            "canaryStatus": "passed",
            "diagnosticCode": "canary_passed",
            "message": message if isinstance(message, str) else "",
            "adapterProcessStarted": False,
            "gameProcessStarted": False,
        }

    def host_diagnostic(self, games: Iterable[GameSummary]) -> dict[str, Any]:
        registered = list(games)
        host = self.probe()
        per_game = {
            game.game_id: self.execution_bindings(game.game_id) for game in registered
        }
        ready_game_ids = [
            game_id
            for game_id, runtime in per_game.items()
            if runtime.get("manifestVerified") is True
            and runtime.get("status") == "promoted"
            and bool(runtime.get("bindings"))
        ]
        unavailable_game_ids = [
            game.game_id
            for game in registered
            if game.game_id not in ready_game_ids
        ]
        return {
            **host,
            "legacyCompatibilityExecutionReady": bool(host["executionReady"]),
            "executionReady": bool(
                host["hostHealthy"]
                and host["executionGateEnabled"]
                and not host["activeExecution"]
                and ready_game_ids
            ),
            "readyGameIds": ready_game_ids,
            "unavailableGameIds": unavailable_game_ids,
            "perGameExecution": per_game,
            "registeredAdapterCount": len(registered),
        }

    def execution_bindings(self, game_id: str) -> dict[str, Any]:
        """Project the installed v2 operation map without starting a process.

        Manager must use this runtime projection when deriving
        ``executableTodoInstanceIds``. Catalog ``automationState`` and the broad
        ``game.daily.run`` capability are not sufficient execution authority.
        """
        self.validate_game_id(game_id)
        try:
            manifest = self._verify_promoted_package(
                self.compatibility_adapter.execution_package_root(game_id)
            )
        except AdapterProtocolError as error:
            return {
                "schemaVersion": 1,
                "protocolVersion": self.PROTOCOL_VERSION,
                "gameId": game_id,
                "manifestVerified": False,
                "status": error.code,
                "packageVersion": None,
                "packageDigest": None,
                "bindings": [],
            }
        game_bindings = manifest.operation_bindings.get(game_id, {})
        return {
            "schemaVersion": 1,
            "protocolVersion": self.PROTOCOL_VERSION,
            "gameId": game_id,
            "manifestVerified": True,
            "status": "promoted" if game_bindings else "game_not_promoted",
            "packageVersion": manifest.package_version,
            "packageDigest": manifest.package_digest,
            "bindings": [
                {
                    "operation": operation,
                    "handlerId": binding.handler_id,
                    "mode": binding.mode,
                    "actionClass": binding.action_class,
                    "risk": binding.risk,
                    "todoDefinitionIds": list(binding.todo_definition_ids),
                    "adapterCapabilityRefs": list(binding.adapter_capability_refs),
                    "supportsResume": binding.supports_resume,
                    "timeoutSeconds": binding.timeout_seconds,
                    "requiredEvidenceKinds": list(binding.required_evidence_kinds),
                }
                for operation, binding in sorted(game_bindings.items())
            ],
        }

    def validate_execution_request(
        self, plan: AdapterExecutionPlan | dict[str, Any]
    ) -> AdapterExecutionPlan:
        """Strict, process-free validation against the current promoted manifest."""
        try:
            parsed = (
                plan
                if isinstance(plan, AdapterExecutionPlan)
                else AdapterExecutionPlan.from_document(plan)
            )
            # Enforce exact JSON/size semantics even for a constructed dataclass.
            serialize_execute_request(parsed)
            expires_at = datetime.fromisoformat(
                parsed.expires_at.replace("Z", "+00:00")
            )
            if expires_at <= datetime.now(timezone.utc):
                raise AdapterProtocolError(
                    "execute_request_expired", "Execute request lease has expired"
                )
            manifest = self._verify_promoted_package(
                self.compatibility_adapter.execution_package_root(parsed.game_id)
            )
            validate_plan_against_manifest(parsed, manifest)
            return parsed
        except AdapterProtocolError as error:
            raise AdapterExecutionRejected(error.code, str(error)) from error

    def validate_game_id(self, game_id: str) -> None:
        self.compatibility_adapter.validate_game_id(game_id)

    def active_execution(self) -> dict[str, object] | None:
        with self._execution_lock:
            if self._active_process is not None:
                plan = self._active_plan
                return {
                    "runId": plan.run_id if plan is not None else None,
                    "runAttemptId": plan.run_attempt_id if plan is not None else None,
                    "gameId": plan.game_id if plan is not None else None,
                    "pid": self._active_process.pid,
                    "exited": self._active_process.poll() is not None,
                    "cancelRequested": self._cancel_sent,
                }
        return self.compatibility_adapter.active_execution()

    def _verified_execution_manifest(self, game_id: str) -> ExecutionPackageManifest:
        try:
            return self._verify_promoted_package(
                self.compatibility_adapter.execution_package_root(game_id)
            )
        except AdapterProtocolError as error:
            raise ExecutionPackageUnavailable(str(error)) from error

    def _verify_promoted_package(self, package_root: Path) -> ExecutionPackageManifest:
        try:
            self._read_verified_host_manifest()
        except AdapterHostProtocolError as error:
            raise AdapterProtocolError(error.code, str(error)) from error
        return verify_execution_package(
            package_root,
            receipt_resolver=self._promotion_receipt_resolver,
        )

    @staticmethod
    def _canonical_resource_bytes(resource: Mapping[str, Any]) -> bytes:
        return json.dumps(
            resource,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _public_resource(resource: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "resourceId": resource["resource_id"],
            "resourceType": resource["resource_type"],
            "state": resource["state"],
            "document": resource["document"],
            "createdAt": resource["created_at"],
            "updatedAt": resource["updated_at"],
        }

    @staticmethod
    def _nonzero_digest(value: object) -> str:
        digest = str(value)
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or digest == "sha256:" + "0" * 64
        ):
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid", "candidate evidence digest is invalid"
            )
        return digest

    def _fixed_game_module_root(self, game_id: str) -> Path:
        self.validate_game_id(game_id)
        modules = self.runtime_dir / "adapters" / "game-modules"
        nominal = modules / game_id.lower()
        try:
            root = nominal.resolve(strict=True)
            modules_root = modules.resolve(strict=True)
        except OSError as error:
            raise AdapterPromotionRejected(
                "candidate_package_missing", "fixed installed candidate module is missing"
            ) from error
        if (
            root.parent != modules_root
            or root.name != game_id.lower()
            or not root.is_relative_to(self.runtime_dir)
            or not root.is_dir()
            or self._is_reparse_point(nominal)
            or self._is_reparse_point(modules)
        ):
            raise AdapterPromotionRejected(
                "unsafe_candidate_path", "fixed installed candidate path is unsafe"
            )
        return nominal

    def _candidate_test_evidence(
        self,
        *,
        game_id: str,
        manifest: ExecutionPackageManifest,
    ) -> tuple[dict[str, Any], str]:
        evidence_root = self.runtime_dir / "adapters" / "promotion-evidence"
        evidence_dir = evidence_root / game_id.lower()
        nominal = evidence_dir / "candidate-test-evidence.json"
        try:
            resolved_root = evidence_root.resolve(strict=True)
            resolved_dir = evidence_dir.resolve(strict=True)
            resolved = nominal.resolve(strict=True)
        except OSError as error:
            raise AdapterPromotionRejected(
                "candidate_evidence_missing", "fixed candidate test evidence is missing"
            ) from error
        if (
            resolved_dir.parent != resolved_root
            or resolved_dir.name != game_id.lower()
            or resolved.parent != resolved_dir
            or resolved.name != "candidate-test-evidence.json"
            or not resolved.is_relative_to(self.runtime_dir)
            or not resolved.is_file()
            or any(
                self._is_reparse_point(item)
                for item in (evidence_root, evidence_dir, nominal)
            )
        ):
            raise AdapterPromotionRejected(
                "unsafe_candidate_evidence", "fixed candidate evidence path is unsafe"
            )
        raw = resolved.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid", "candidate evidence size is invalid"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid", "candidate evidence is not valid JSON"
            ) from error
        required = {
            "schemaVersion",
            "resourceType",
            "status",
            "passed",
            "packageId",
            "packageVersion",
            "buildId",
            "supportedGameIds",
            "payloadDigest",
            "replaySuiteDigest",
            "shadowSuiteDigest",
            "generatedAt",
            "gameStarted",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid", "candidate evidence fields are invalid"
            )
        try:
            generated_at = datetime.fromisoformat(
                str(document["generatedAt"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid", "candidate evidence time is invalid"
            ) from error
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise AdapterPromotionRejected(
                "candidate_evidence_invalid",
                "candidate evidence time requires a timezone",
            )
        if (
            document["schemaVersion"] != 1
            or document["resourceType"] != "adapter-candidate-test-evidence"
            or document["status"] != "passed"
            or document["passed"] is not True
            or document["packageId"] != "legacy-night-rain-gamer"
            or document["packageVersion"] != manifest.package_version
            or document["buildId"] != manifest.build_id
            or document["supportedGameIds"] != [game_id]
            or document["payloadDigest"] != manifest.payload_digest
            or document["replaySuiteDigest"] != manifest.replay_suite_digest
            or document["shadowSuiteDigest"] != manifest.shadow_suite_digest
            or document["gameStarted"] is not False
        ):
            raise AdapterPromotionRejected(
                "candidate_evidence_mismatch",
                "candidate evidence does not bind the fixed installed package",
            )
        for field in ("payloadDigest", "replaySuiteDigest", "shadowSuiteDigest"):
            self._nonzero_digest(document[field])
        return document, "sha256:" + hashlib.sha256(raw).hexdigest()

    def promote_candidate(
        self,
        *,
        adapter_id: str,
        game: GameSummary,
        version_id: str,
        requested_by: str,
        reason: str,
        create_canary_resource: Callable[[dict[str, Any]], Mapping[str, Any]],
        create_receipt_resource: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Promote one fixed installed module from Manager-owned evidence.

        The API caller supplies no path, digest, receipt, or command.  The
        candidate directory, test report and Host are all derived from the
        registered GameId.  The live package changes through one directory
        swap while the same lock that fences execution is held.
        """

        with self._execution_lock:
            if self._active_process is not None and self._active_process.poll() is None:
                raise AdapterPromotionRejected(
                    "adapter_busy", "an Adapter execution is active"
                )
            active = self.compatibility_adapter.active_execution()
            if active and not bool(active.get("exited")):
                raise AdapterPromotionRejected(
                    "adapter_busy", "a compatibility execution is active"
                )
            package_root = self._fixed_game_module_root(game.game_id)
            try:
                manifest = verify_execution_candidate(package_root)
            except AdapterProtocolError as error:
                raise AdapterPromotionRejected(error.code, str(error)) from error
            if manifest.supported_game_ids != (game.game_id,):
                raise AdapterPromotionRejected(
                    "candidate_scope_mismatch",
                    "installed candidate must have exact one-game scope",
                )
            if version_id != manifest.package_version:
                raise AdapterPromotionRejected(
                    "candidate_version_mismatch",
                    "requested version differs from the installed candidate",
                )
            _, candidate_evidence_sha256 = self._candidate_test_evidence(
                game_id=game.game_id,
                manifest=manifest,
            )
            diagnostic = self.canary(adapter_id, [game])
            if (
                diagnostic.get("canaryStatus") != "passed"
                or diagnostic.get("diagnosticOnly") is not True
                or diagnostic.get("hostHealthy") is not True
                or diagnostic.get("hostManifestVerified") is not True
                or diagnostic.get("adapterProcessStarted") is not False
                or diagnostic.get("gameProcessStarted") is not False
                or diagnostic.get("gameId") != game.game_id
                or diagnostic.get("adapterId") != adapter_id
            ):
                raise AdapterPromotionRejected(
                    "manager_canary_failed",
                    "Manager-owned no-process canary did not pass",
                )
            canary_resource = create_canary_resource(
                {
                    **diagnostic,
                    "requestedBy": requested_by,
                    "note": reason,
                }
            )
            public_canary = self._public_resource(canary_resource)
            canary_digest = "sha256:" + hashlib.sha256(
                self._canonical_resource_bytes(public_canary)
            ).hexdigest()
            host_hash = str(diagnostic.get("hostEntryPointSha256", ""))
            if re.fullmatch(r"[0-9a-f]{64}", host_hash) is None:
                raise AdapterPromotionRejected(
                    "manager_canary_invalid", "Manager canary Host digest is invalid"
                )
            receipt_id = str(uuid.uuid4())
            issued_at = datetime.now(timezone.utc).isoformat()
            receipt = {
                "schemaVersion": 1,
                "resourceType": "adapter-promotion-receipt",
                "resourceId": receipt_id,
                "state": "passed",
                "packageId": "legacy-night-rain-gamer",
                "packageVersion": manifest.package_version,
                "buildId": manifest.build_id,
                "supportedGameIds": [game.game_id],
                "payloadDigest": manifest.payload_digest,
                "replaySuiteDigest": manifest.replay_suite_digest,
                "shadowSuiteDigest": manifest.shadow_suite_digest,
                "canarySuiteDigest": canary_digest,
                "candidateTestEvidenceSha256": candidate_evidence_sha256,
                "managerCanaryEvidenceSha256": canary_digest,
                "hostEntryPointSha256": "sha256:" + host_hash,
                "issuedAt": issued_at,
            }
            receipt_resource = create_receipt_resource(receipt)
            if (
                receipt_resource.get("resource_id") != receipt_id
                or receipt_resource.get("resource_type")
                != "adapter-promotion-receipt"
                or receipt_resource.get("state") != "passed"
                or receipt_resource.get("document") != receipt
            ):
                raise AdapterPromotionRejected(
                    "promotion_receipt_persistence_failed",
                    "Manager did not persist the exact promotion receipt",
                )

            modules_root = package_root.parent
            incoming = modules_root / f".{game.game_id.lower()}.promotion-{uuid.uuid4().hex}"
            backup = modules_root / f".{game.game_id.lower()}.pre-promotion-{uuid.uuid4().hex}"
            swapped = False
            try:
                shutil.copytree(package_root, incoming, copy_function=shutil.copy2)
                manifest_path = incoming / "install-manifest.json"
                manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
                receipt_bytes = self._canonical_resource_bytes(receipt)
                receipt_path = incoming / "promotion-receipt.json"
                receipt_path.write_bytes(receipt_bytes)
                receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
                manifest_document["files"].append(
                    {
                        "path": "promotion-receipt.json",
                        "sha256": receipt_hash,
                        "sizeBytes": len(receipt_bytes),
                    }
                )
                manifest_document["promotion"] = {
                    "status": "promoted",
                    "replaySuiteDigest": manifest.replay_suite_digest,
                    "shadowSuiteDigest": manifest.shadow_suite_digest,
                    "canarySuiteDigest": canary_digest,
                    "payloadDigest": manifest.payload_digest,
                    "receiptFile": "promotion-receipt.json",
                    "receiptSha256": "sha256:" + receipt_hash,
                    "receiptResourceId": receipt_id,
                }
                manifest_document["executionReady"] = True
                manifest_bytes = json.dumps(
                    manifest_document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                manifest_path.write_bytes(manifest_bytes)
                verify_execution_package(
                    incoming,
                    receipt_resolver=lambda resource_id: receipt_resource,
                )
                os.replace(package_root, backup)
                try:
                    os.replace(incoming, package_root)
                    swapped = True
                except Exception:
                    os.replace(backup, package_root)
                    raise
                self._verify_promoted_package(package_root)
            except Exception as error:
                if swapped:
                    failed = modules_root / f".{game.game_id.lower()}.failed-{uuid.uuid4().hex}"
                    os.replace(package_root, failed)
                    os.replace(backup, package_root)
                    shutil.rmtree(failed, ignore_errors=True)
                if incoming.exists():
                    shutil.rmtree(incoming, ignore_errors=True)
                if isinstance(error, AdapterPromotionRejected):
                    raise
                raise AdapterPromotionRejected(
                    "promotion_install_failed",
                    "promoted package could not be installed atomically",
                ) from error
            return {
                "adapterId": adapter_id,
                "gameId": game.game_id,
                "packageVersion": manifest.package_version,
                "buildId": manifest.build_id,
                "payloadDigest": manifest.payload_digest,
                "promotionReceipt": self._public_resource(receipt_resource),
                "managerCanary": public_canary,
                "executionReady": True,
                "gameStarted": False,
                "rollbackState": "pre-promotion-package-retained",
            }

    def _send_cancel_locked(
        self,
        *,
        plan: AdapterExecutionPlan,
        reason_code: str,
    ) -> bool:
        process = self._active_process
        if (
            process is None
            or self._active_plan != plan
            or process.poll() is not None
            or process.stdin is None
            or self._cancel_sent
        ):
            return False
        durable = self._write_durable_cancel(
            run_id=plan.run_id,
            run_attempt_id=plan.run_attempt_id,
            cancel_authority=plan.cancel_authority,
            reason_code=reason_code,
        )
        at = datetime.now(timezone.utc).isoformat()
        payload = serialize_cancel_control(plan, at=at, reason_code=reason_code)
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return durable
        self._cancel_sent = True
        return True

    def cancel(
        self,
        *,
        run_attempt_id: str,
        fencing_token: str,
        reason_code: str = "user_cancelled",
    ) -> bool:
        """Request cooperative cancellation for the exact active attempt.

        The Host and runner receive a typed control envelope. This method never
        terminates a game process or an arbitrary child process tree.
        """
        with self._execution_lock:
            plan = self._active_plan
            if (
                plan is None
                or plan.run_attempt_id != run_attempt_id
                or plan.fencing_token != fencing_token
            ):
                return False
            return self._send_cancel_locked(plan=plan, reason_code=reason_code)

    def cancel_run(
        self, *, run_id: str, reason_code: str = "user_cancelled"
    ) -> tuple[str, bool]:
        """Cancel the active attempt for one Manager-validated GameRun.

        The fencing token never crosses the Manager service boundary; this
        method resolves it only from the Host's in-memory active ownership.
        """

        with self._execution_lock:
            plan = self._active_plan
            if plan is None or plan.run_id != run_id:
                return "", False
            return (
                plan.run_attempt_id,
                self._send_cancel_locked(plan=plan, reason_code=reason_code),
            )

    def cancel_persisted_attempt(
        self,
        *,
        run_id: str,
        run_attempt_id: str,
        cancel_authority_hash: str,
        reason_code: str = "user_cancelled",
    ) -> bool:
        """Write a restart-safe stop request for an exact persisted attempt.

        Only the cancellation-only authority file is recovered.  The execution
        fencing token remains unrecoverable and never crosses this boundary.
        """

        authority = self._load_control_authority(
            run_id=run_id,
            run_attempt_id=run_attempt_id,
            expected_hash=cancel_authority_hash,
        )
        if authority is None:
            return False
        return self._write_durable_cancel(
            run_id=run_id,
            run_attempt_id=run_attempt_id,
            cancel_authority=authority,
            reason_code=reason_code,
        )

    def _cleanup_attempt_controls(self, plan: AdapterExecutionPlan) -> None:
        # Cleanup must never create a directory.  In particular, a caller may
        # be tearing down an isolated runtime immediately after its completion
        # callback returns; using _private_runtime_file here would recreate a
        # just-removed directory and leave both teardown and recovery racy.
        runtime = self.runtime_dir.resolve()
        leaves = (
            (self.CONTROL_AUTHORITY_DIRECTORY, f"{plan.run_attempt_id}.json"),
            (self.DURABLE_CONTROL_DIRECTORY, f"{plan.run_attempt_id}.cancel.json"),
            (self.DURABLE_CONTROL_DIRECTORY, f"{plan.run_attempt_id}.delivered.json"),
        )
        for directory, leaf in leaves:
            try:
                root = (self.runtime_dir / directory).resolve()
                if root.parent != runtime or not root.is_relative_to(runtime):
                    continue
                path = (root / leaf).resolve()
                if (
                    path.parent != root
                    or path.name != leaf
                    or not path.is_relative_to(runtime)
                    or (path.exists() and self._is_reparse_point(path))
                ):
                    continue
                path.unlink(missing_ok=True)
            except (OSError, RuntimeError):
                pass

    def _watch_execution(
        self,
        *,
        process: subprocess.Popen[bytes],
        plan: AdapterExecutionPlan,
        manifest: ExecutionPackageManifest,
        host_probe: dict[str, Any],
        on_event: Callable[[AdapterEvent, ExecutionPackageManifest], None] | None,
        on_complete: Callable[[AdapterRunResult], None],
        installation_binding_path: Path | None = None,
        transcript: AttemptLogSession | None = None,
    ) -> None:
        with bind_log_context(
            run=plan.run_id, attempt=plan.run_attempt_id, game=plan.game_id, phase="tool-run"
        ):
            self._watch_execution_bound(
                process=process,
                plan=plan,
                manifest=manifest,
                host_probe=host_probe,
                on_event=on_event,
                on_complete=on_complete,
                installation_binding_path=installation_binding_path,
                transcript=transcript,
            )

    def _watch_execution_bound(
        self,
        *,
        process: subprocess.Popen[bytes],
        plan: AdapterExecutionPlan,
        manifest: ExecutionPackageManifest,
        host_probe: dict[str, Any],
        on_event: Callable[[AdapterEvent, ExecutionPackageManifest], None] | None,
        on_complete: Callable[[AdapterRunResult], None],
        installation_binding_path: Path | None = None,
        transcript: AttemptLogSession | None = None,
    ) -> None:
        _log.info(
            "adapter.watch.begin pid=%s timeoutSeconds=%s package=%s@%s digest=%s",
            process.pid,
            plan.timeout_seconds,
            manifest.package_root.name,
            manifest.package_version,
            manifest.package_digest[:16],
        )
        stream = AdapterEventStream(
            plan,
            allowed_artifact_mime_types=manifest.allowed_mime_types,
            max_artifact_bytes=manifest.max_artifact_bytes,
            max_artifacts_per_todo=manifest.max_artifacts_per_todo,
            expected_package_version=manifest.package_version,
            expected_package_digest=manifest.package_digest,
        )
        failure_lock = threading.Lock()
        failure_event = threading.Event()
        failures: list[AdapterProtocolError] = []
        stderr_buffer = bytearray()

        def record_failure(error: AdapterProtocolError) -> None:
            _log.error("adapter.protocol_failure code=%s message=%s", error.code, error)
            with failure_lock:
                if not failures:
                    failures.append(error)
                    failure_event.set()
            with self._execution_lock:
                self._send_cancel_locked(
                    plan=plan, reason_code="adapter_protocol_failure"
                )

        def read_stdout() -> None:
            output = process.stdout
            if output is None:
                record_failure(
                    AdapterProtocolError(
                        "missing_host_stdout", "Adapter Host stdout is unavailable"
                    )
                )
                return
            while True:
                try:
                    line = output.readline(MAX_EVENT_BYTES + 2)
                except OSError as error:
                    record_failure(
                        AdapterProtocolError(
                            "host_stdout_failed", "Adapter Host stdout could not be read"
                        )
                    )
                    return
                if not line:
                    return
                if transcript is not None:
                    transcript.write_stdout(line)
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b"\n"):
                    record_failure(
                        AdapterProtocolError(
                            "event_too_large", "Adapter Host emitted an invalid JSONL frame"
                        )
                    )
                    return
                try:
                    if not stream.hello_seen:
                        rejection = self._execute_startup_rejection(
                            line[:-1], plan=plan, probe=host_probe
                        )
                        if rejection is not None:
                            raise rejection
                    event = stream.consume_line(line[:-1])
                    if on_event is not None:
                        on_event(event, manifest)
                except AdapterProtocolError as error:
                    record_failure(error)
                    return
                except Exception as error:  # Manager persistence callback failed.
                    record_failure(
                        AdapterProtocolError(
                            "event_callback_failed",
                            f"Adapter event callback failed: {type(error).__name__}",
                        )
                    )
                    return

        def read_stderr() -> None:
            error_stream = process.stderr
            if error_stream is None:
                return
            while True:
                try:
                    chunk = error_stream.read(1024)
                except OSError:
                    return
                if not chunk:
                    return
                if transcript is not None:
                    transcript.write_stderr(chunk)
                remaining = MAX_STDERR_BYTES + 1 - len(stderr_buffer)
                if remaining > 0:
                    stderr_buffer.extend(chunk[:remaining])
                if len(stderr_buffer) > MAX_STDERR_BYTES:
                    record_failure(
                        AdapterProtocolError(
                            "host_stderr_too_large", "Adapter Host stderr exceeded its limit"
                        )
                    )
                    return

        stdout_thread = threading.Thread(
            target=read_stdout,
            name=f"yeyu-adapter-events-{plan.run_attempt_id[:8]}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stderr,
            name=f"yeyu-adapter-stderr-{plan.run_attempt_id[:8]}",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        cancel_requested = False
        result: AdapterRunResult
        try:
            deadline = time.monotonic() + plan.timeout_seconds
            while process.poll() is None:
                with self._execution_lock:
                    cancel_requested = self._cancel_sent
                if cancel_requested:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if failure_event.wait(timeout=min(0.1, remaining)):
                    break
            if process.poll() is None and (
                cancel_requested or timed_out or failure_event.is_set()
            ):
                with self._execution_lock:
                    self._send_cancel_locked(
                        plan=plan,
                        reason_code=(
                            "user_cancelled"
                            if cancel_requested
                            else (
                                "adapter_timeout"
                                if timed_out
                                else "adapter_protocol_failure"
                            )
                        ),
                    )
                try:
                    exit_code = process.wait(timeout=self.CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    # Only the fixed Host process is terminated. The game client
                    # and an arbitrary descendant tree are deliberately preserved.
                    process.terminate()
                    try:
                        exit_code = process.wait(timeout=self.CANCEL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        exit_code = process.wait()
            else:
                exit_code = process.wait()
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            stdout_thread.join(timeout=self.CANCEL_GRACE_SECONDS)
            stderr_thread.join(timeout=self.CANCEL_GRACE_SECONDS)

            with failure_lock:
                protocol_failure = failures[0] if failures else None
            if protocol_failure is not None:
                result = stream.failure_result(
                    code=protocol_failure.code,
                    message=str(protocol_failure),
                    process_exit_code=exit_code,
                )
            elif timed_out:
                result = stream.failure_result(
                    code="adapter_timeout",
                    message="Adapter Host exceeded the Manager timeout",
                    process_exit_code=exit_code,
                    transport_outcome="timeout",
                )
            elif cancel_requested:
                cooperative_result = stream.finish(process_exit_code=exit_code)
                if cooperative_result.protocol_valid:
                    result = cooperative_result
                else:
                    result = stream.failure_result(
                        code="adapter_cancelled_without_terminal",
                        message=(
                            "Adapter Host did not acknowledge cooperative cancellation "
                            "within the bounded grace period"
                        ),
                        process_exit_code=exit_code,
                        transport_outcome="cancelled",
                    )
            else:
                result = stream.finish(process_exit_code=exit_code)
            _log.info(
                "adapter.watch.end exit=%s timedOut=%s cancelRequested=%s protocolFailure=%s "
                "-> status=%s transport=%s code=%s stderrBytes=%s",
                exit_code,
                timed_out,
                cancel_requested,
                protocol_failure.code if protocol_failure is not None else None,
                result.status,
                result.transport_outcome,
                result.code,
                len(stderr_buffer),
            )
            if stderr_buffer:
                _log.info(
                    "adapter.stderr.tail %s",
                    bytes(stderr_buffer[-2048:]).decode("utf-8", errors="replace"),
                )
        except Exception as error:  # pragma: no cover - defensive watcher boundary
            _log.exception("adapter.watch.crashed")
            result = stream.failure_result(
                code="adapter_watcher_failed",
                message=f"Adapter watcher failed: {type(error).__name__}",
                process_exit_code=process.poll() if process.poll() is not None else -1,
            )
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if installation_binding_path is not None:
            try:
                installation_binding_path.unlink(missing_ok=True)
            except OSError:
                # The binding is confined to this run's staging directory;
                # an eventual staging cleanup can safely remove a stale file.
                pass
        try:
            on_complete(result)
        except Exception:
            # Manager owns callback persistence and recovery. Never invoke a
            # completion callback twice after an exception.
            _log.exception("adapter.on_complete.failed")
        else:
            # The authority and durable cancel file are recovery material until
            # Manager has committed the terminal attempt.  A callback failure
            # must leave them available for restart reconciliation.
            self._cleanup_attempt_controls(plan)
        finally:
            with self._execution_lock:
                if self._active_process is process:
                    # Keep ownership until the completion callback returned so a
                    # second execution cannot overlap Manager persistence.
                    self._active_process = None
                    self._active_plan = None
                    self._cancel_sent = False

    def execute(
        self,
        run_id: str,
        game_id: str,
        on_complete: Callable[[AdapterRunResult], None],
        *,
        plan: AdapterExecutionPlan | dict[str, Any] | None = None,
        on_event: Callable[[AdapterEvent, ExecutionPackageManifest], None] | None = None,
        installation_binding: Mapping[str, Any] | None = None,
        transcript: AttemptLogSession | None = None,
    ) -> int:
        """Start one strictly scoped Todo execution attempt.

        ``plan`` must contain the exact non-empty
        ``executableTodoInstanceIds`` selected by Manager. A game with only
        deferred, blocked, review-required, or already completed Todos is sealed
        before any Host process starts.  ``transcript`` receives the raw Host
        stdout/stderr for post-mortem diagnostics.
        """
        self.validate_game_id(game_id)
        if plan is None:
            error = NoExecutableTodos()
            raise AdapterExecutionRejected(error.code, str(error)) from error
        execution_plan = self.validate_execution_request(plan)
        request_payload = serialize_execute_request(execution_plan)
        if execution_plan.run_id != run_id or execution_plan.game_id != game_id:
            raise AdapterExecutionRejected(
                "execution_scope_mismatch", "Plan scope differs from execute arguments"
            )

        probe = self.probe()
        if not probe["hostHealthy"]:
            raise ExecutionPackageUnavailable("Manager Adapter Host is unavailable")
        if not self.compatibility_adapter.execution_enabled:
            raise ExecutionPackageUnavailable("execution gate is disabled")
        package_root = self.compatibility_adapter.execution_package_root(game_id)
        manifest = self._verified_execution_manifest(game_id)

        entrypoint = self._fixed_host_file(self.HOST_ENTRYPOINT)
        staging_dir = self._artifact_staging_dir(execution_plan)
        if installation_binding is None:
            raise AdapterExecutionRejected(
                "installation_binding_missing", "This game has no Manager-owned local installation binding"
            )
        binding_path: Path | None = None
        command = [
            str(entrypoint),
            "--operation",
            "execute",
            "--protocol-version",
            self.PROTOCOL_VERSION,
            "--run-id",
            run_id,
            "--game-id",
            game_id,
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self._execution_lock:
            active = self._active_process
            if active is not None and active.poll() is None:
                raise AdapterExecutionRejected(
                    "adapter_busy", "Another Adapter execution is already active"
                )
            compatibility_active = self.compatibility_adapter.active_execution()
            if compatibility_active and not bool(compatibility_active.get("exited")):
                raise AdapterExecutionRejected(
                    "adapter_busy", "A compatibility execution is already active"
                )
            binding_path = self._write_installation_binding(
                staging_dir,
                game_id=game_id,
                installation_binding=installation_binding,
            )
            authority_path = self._write_control_authority(execution_plan)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.host_root,
                    env=self._host_environment(
                        staging_dir=staging_dir,
                        installation_binding_path=binding_path,
                        execution_package_root=package_root,
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    creationflags=creation_flags,
                    bufsize=0,
                )
            except OSError as error:
                binding_path.unlink(missing_ok=True)
                authority_path.unlink(missing_ok=True)
                raise AdapterHostProtocolError(
                    "host_invocation_failed", "Adapter Host could not be started"
                ) from error
            self._active_process = process
            self._active_plan = execution_plan
            self._cancel_sent = False
            try:
                if process.stdin is None:
                    raise OSError("Adapter Host stdin is unavailable")
                process.stdin.write(request_payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                process.terminate()
                process.wait(timeout=self.CANCEL_GRACE_SECONDS)
                self._active_process = None
                self._active_plan = None
                binding_path.unlink(missing_ok=True)
                authority_path.unlink(missing_ok=True)
                raise AdapterHostProtocolError(
                    "host_stdin_failed", "Adapter Host request could not be written"
                ) from error

        threading.Thread(
            target=self._watch_execution,
            kwargs={
                "process": process,
                "plan": execution_plan,
                "manifest": manifest,
                "host_probe": probe,
                "on_event": on_event,
                "on_complete": on_complete,
                "installation_binding_path": binding_path,
                "transcript": transcript,
            },
            name=f"yeyu-adapter-watch-{execution_plan.run_attempt_id[:8]}",
            daemon=True,
        ).start()
        return process.pid

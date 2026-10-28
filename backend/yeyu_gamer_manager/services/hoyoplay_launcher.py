"""Official check/repair UIA driver under the existing Manager preflight."""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import time
import uuid

from .game_launcher import GameLaunchCancelled
from .hoyoplay_native_execution import NativeHoYoPlaySession
from .hoyoplay_native_observation import FIXED_GAME_EXECUTABLE
from .hoyoplay_ui_contract import interpret, fail
from .hoyoplay_owner_family import node_owner_key

SETTLING_SECONDS = 8.0
SETTLING_POLL_SECONDS = .2


@dataclass(frozen=True, slots=True)
class LauncherSnapshot:
    state: str
    observed_at: float
    process_id: int
    created_at: int
    game_path: str
    actions: frozenset[str]
    progress: tuple[str, int] | None = None
    completion_receipt: str | None = None


class HoYoPlayLauncher:
    def __init__(self, service: object, game_executable: Path, notify: Callable,
                 cancel_requested: Callable[[], bool], *, context=None):
        self._cancel_requested = cancel_requested
        self.game, self.context, self.notify = game_executable, context, notify
        self.session = self._snapshot = self._view = self._pending = self._receipt = None
        self._counts = {"check": 0, "verify": 0, "update": 0}
        self._update_scope = None
        self._poisoned = False
        self._buffered_view = None
        self._view_at = None

    def _guard(self):
        if self._cancel_requested():
            raise GameLaunchCancelled("绝区零启动器前置检查已取消。", reason_code="zzz_launcher_preflight_cancelled")
        if self._poisoned:
            fail("unknown_action_outcome")

    def prepare(self):
        self._guard()
        if self.context is None:
            fail("native_context_missing")
        if PureWindowsPath(str(self.game)) != PureWindowsPath(str(FIXED_GAME_EXECUTABLE)):
            fail("game_binding_changed")
        if self.session is not None:
            fail("session_already_prepared")
        self.session = NativeHoYoPlaySession(self.context, self._cancel_requested)
        self.session.start()
        self._guard()

    def _read(self):
        self._guard()
        if self.session is None:
            fail("session_not_prepared")
        response = self.session.request("observe")
        self._guard()
        if response.get("view") == {"waiting": True}:
            self._view = None
            return None
        self._view = interpret(response.get("view"),
            update_scope=self._update_scope if self._pending == "update" else None)
        self._view_at = time.monotonic()
        return self._view

    def _dispatch(self, action, view, node):
        self._guard()
        try:
            result = self.session.request("invoke", action=action, nonce=view.packet["nonce"], index=node["index"])
            if result.get("invoked") is not True or result.get("action") != action:
                fail("action_receipt_missing")
            self._guard()
            return result  # Native transport has durably recorded its return.
        except BaseException:
            self._poisoned = True
            raise

    def _wait(self, predicate):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            view = self._read()
            if view is None:
                fail("process_identity_changed_during_action")
            if predicate(view):
                return view
            if view.state not in {"ready", "waiting"}:
                fail("unexpected_action_transition")
            self._guard()
            time.sleep(.2)
        fail("action_transition_timeout")

    def _dialog(self, view, name):
        return view.unique([n for n in view.nodes if n["name"] == name and view.below(n, view.document)
                            and n.get("offscreen") is False])

    @staticmethod
    def _surface_identity(view):
        p = view.packet["process"]
        return (p["pid"], p["createdAt"], view.packet["hwnd"],
                tuple(view.document["uia"]["runtimeId"]),
                view.packet["installationAssociation"]["configSha256"], node_owner_key(view.document["uia"]))

    def _settle_dialog(self, initial, receipt, *, action, old_state, dialog_name, terminal_states):
        # This is continuation of one acknowledged action, never permission to
        # click again. No arbitrary old dialog or unrelated state is tolerated.
        if receipt.get("invoked") is not True or receipt.get("action") != action:
            fail("settling_receipt_missing")
        identity = self._surface_identity(initial)
        dialog_id = tuple(self._dialog(initial, dialog_name)["uia"]["runtimeId"])
        dialog_owner = node_owner_key(self._dialog(initial, dialog_name)["uia"])
        deadline = time.monotonic() + SETTLING_SECONDS
        while True:
            self._guard()
            if time.monotonic() >= deadline:
                fail("dialog_settling_timeout")
            view = self._read()
            if view is None or self._surface_identity(view) != identity:
                fail("dialog_settling_identity_changed")
            if time.monotonic() >= deadline:
                fail("dialog_settling_timeout")
            if view.state in terminal_states:
                return view, self._view_at
            if (view.state != old_state or tuple(self._dialog(view, dialog_name)["uia"]["runtimeId"]) != dialog_id
                    or node_owner_key(self._dialog(view, dialog_name)["uia"]) != dialog_owner):
                fail("dialog_settling_unowned_state")
            time.sleep(SETTLING_POLL_SECONDS)

    def observe(self):
        self._guard()
        if self._buffered_view is not None:
            view, observed_at = self._buffered_view
            self._buffered_view = None
            if not 0 <= time.monotonic() - observed_at <= 2:
                fail("stale_observation")
        else:
            view = self._read()
            observed_at = self._view_at
        if view is None:
            if self._pending is not None:
                fail("process_identity_changed_during_action")
            self._snapshot = LauncherSnapshot("waiting", time.monotonic(), 0, 0, str(self.game), frozenset())
            return self._snapshot
        state, receipt = view.state, None
        if state == "check-complete":
            if self._pending != "check":
                fail("unowned_completion_dialog")
            dialog = self._dialog(view, "提示")
            acknowledged = self._dispatch("ack-check", view, view.target("知道了", "Button", dialog))
            view, observed_at = self._settle_dialog(view, acknowledged, action="ack-check",
                old_state="check-complete", dialog_name="提示", terminal_states={"ready"})
            receipt, state = self._receipt, "up-to-date"
            self._pending = None
        elif state == "repair-confirm":
            fail("unowned_repair_dialog")
        elif state == "verifying" and self._pending not in {"verify", "update"}:
            fail("unowned_repair_progress")
        elif state == "checking" and self._pending != "check":
            fail("unowned_check_progress")
        p = view.packet["process"]
        actions = frozenset({"check", "verify"}) if state in {"ready", "up-to-date"} else frozenset()
        if state == "update-available":
            actions = frozenset({"update"})
        self._snapshot = LauncherSnapshot(state, observed_at, p["pid"], p["createdAt"],
                                         str(self.game), actions, view.progress, receipt)
        return self._snapshot

    def invoke(self, action, snapshot):
        self._guard()
        if action not in {"check", "update", "verify"}:
            fail("action_forbidden")
        if not isinstance(snapshot, LauncherSnapshot) or snapshot is not self._snapshot or not 0 <= time.monotonic() - snapshot.observed_at <= 2:
            fail("stale_observation")
        maximum = {"check": 2, "verify": 1 + self._counts["update"], "update": 1}[action]
        if action not in snapshot.actions or self._counts[action] >= maximum:
            fail("action_repeated_or_unavailable")
        self._counts[action] += 1
        self._receipt = str(uuid.uuid4())
        self._pending = action
        try:
            view = self._read()
            if action == "update":
                if view is None or view.state != "update-available":
                    fail("action_state_changed")
                target, self._update_scope = view.update_target()
                self._dispatch("update", view, target)
                return self._receipt
            if view is None or view.state != "ready":
                fail("action_state_changed")
            if not view.find("Menu"):
                self._dispatch("expand-menu", view, view.opener())
                view = self._wait(lambda v: bool(v.find("Menu")))
            menu = view.menu()
            name = "检查更新" if action == "check" else "一键修复"
            self._dispatch(action, view, view.target(name, "MenuItem", menu))
            if action == "verify":
                view = self._wait(lambda v: v.state == "repair-confirm")
                dialog = self._dialog(view, "一键修复提醒")
                acknowledged = self._dispatch("confirm-repair", view, view.target("开始修复", "Button", dialog))
                self._buffered_view = self._settle_dialog(view, acknowledged, action="confirm-repair",
                    old_state="repair-confirm", dialog_name="一键修复提醒", terminal_states={"waiting", "verifying", "ready"})
            return self._receipt
        except BaseException:
            self._poisoned = True
            raise

    def close(self):
        if self.session is not None:
            self.session.close()

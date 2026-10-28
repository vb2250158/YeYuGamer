"""Bounded Manager-owned ZZZ launcher preflight, before OneDragon handoff.

Only official launcher states can satisfy this gate. Version-string equality and
process I/O never prove a check, repair, or update completed. The production UI
driver remains unqualified until its real controls and identity chain are known.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from .game_launcher import GameLaunchCancelled, GameLaunchError, GameLaunchHumanRequired, GameLaunchService
from .hoyoplay_launcher import HoYoPlayLauncher, LauncherSnapshot

UPDATE_HARD_CAP_SECONDS = GameLaunchService.LAUNCHER_UPDATE_HARD_CAP_SECONDS
UI_READY_SECONDS = 90.0
NO_PROGRESS_SECONDS = 300.0
SNAPSHOT_TTL_SECONDS = 2.0
POLL_SECONDS = 0.5
MAX_CAPTURES = 16
PROGRESS_OBSERVATION_SECONDS = 30.0
MAX_PROGRESS_PHASES = 8
STATES = frozenset({
    "waiting", "checking", "downloading", "installing", "updating", "verifying", "ready",
    "up-to-date", "verification-complete", "update-available",
    "credentials", "captcha", "unknown", "failed",
})


class ZzzLauncherPreflight:
    """One synchronous preflight under the existing attempt's authority.

    The injected driver is an internal test seam, never an HTTP/config source.
    Deadline and authority checks also feed the existing cancellable probe as a
    pure boolean; exceptions must not leave that helper alive.
    """

    def __init__(self, driver, game_path: Path, *, cancel_requested, authority_valid,
                 deadline_at: datetime, notify, clock=time.monotonic,
                 utc_now=lambda: datetime.now(timezone.utc), sleep=time.sleep):
        self.driver = driver
        self.game_path = str(game_path.resolve()).casefold()
        self.cancel_requested = cancel_requested
        self.authority_valid = authority_valid
        self.deadline_at = deadline_at
        self.notify = notify
        self.clock, self.utc_now, self.sleep = clock, utc_now, sleep
        self.started_at = clock()
        self.last_progress = self.started_at
        self.progress = {}
        self.pids = frozenset()
        self.observations = 0
        self.captures = 0
        self.last_emit_at = self.started_at
        self.last_observed = None
        self.interruption = None

    def interruption_reason(self):
        # A broken authority source cannot silently keep a child action alive.
        try:
            if self.cancel_requested is None or self.cancel_requested():
                return "manager_cancel_requested"
            if (not isinstance(self.deadline_at, datetime)
                    or self.deadline_at.utcoffset() is None):
                return "zzz_client_update_deadline_missing"
            if self.utc_now() >= self.deadline_at:
                return "zzz_client_update_deadline_expired"
            if self.clock() - self.started_at >= UPDATE_HARD_CAP_SECONDS:
                return "zzz_client_update_hard_timeout"
            if self.authority_valid is None or not self.authority_valid():
                return "zzz_client_update_authority_lost"
        except Exception:
            return "zzz_client_update_authority_unavailable"
        return None

    def interrupted(self):
        self.interruption = self.interruption_reason()
        return self.interruption is not None

    def guard(self):
        reason = self.interruption_reason()
        if reason == "manager_cancel_requested":
            raise GameLaunchCancelled(
                "Manager Stop or generation fence interrupted ZZZ launcher preflight.",
                reason_code=reason, detail={"phase": "client-update"},
                process_ids=self.pids)
        if reason:
            self.fail(reason)

    def fail(self, reason):
        raise GameLaunchError(
            "ZZZ official launcher preflight did not establish a current verified client.",
            reason_code=reason, detail={"phase": "client-update"},
            process_ids=self.pids)

    def emit(self, state, *, terminal=False, **detail):
        # Lightweight progress stays visible during long downloads. Only frames
        # are capped; at most one progress event per 30 seconds plus transitions.
        key = (state, tuple(sorted(detail.items())))
        transition = self.last_observed is None or self.last_observed[0] != state
        if not terminal:
            if key == self.last_observed:
                return
            if not transition and self.clock() - self.last_emit_at < PROGRESS_OBSERVATION_SECONDS:
                return
        capture = (transition or terminal) and self.captures < (MAX_CAPTURES if terminal else MAX_CAPTURES - 1)
        if capture:
            self.captures += 1
        self.last_observed = key
        self.last_emit_at = self.clock()
        self.observations += 1
        self.notify(state, {**detail, "captureRequested": capture}, self.pids)

    def validate(self, snapshot):
        if not isinstance(snapshot, LauncherSnapshot) or snapshot.state not in STATES:
            self.fail("zzz_launcher_invalid_observation")
        age = self.clock() - snapshot.observed_at
        if not 0 <= age <= SNAPSHOT_TTL_SECONDS:
            self.fail("zzz_launcher_stale_observation")
        if str(Path(snapshot.game_path).resolve()).casefold() != self.game_path:
            self.fail("zzz_launcher_game_binding_changed")
        if snapshot.state != "waiting":
            if (type(snapshot.process_id) is not int or snapshot.process_id < 1
                    or type(snapshot.created_at) is not int or snapshot.created_at < 1):
                self.fail("zzz_launcher_process_identity_missing")
            self.pids = frozenset({snapshot.process_id})
        else:
            # Never carry the old HWND/PID scope into self-update transition.
            self.pids = frozenset()
        if not snapshot.actions <= frozenset({"check", "update", "verify"}):
            self.fail("zzz_launcher_action_outside_scope")

    def observe_progress(self, snapshot):
        # Only genuine launcher phase/progress changes count, not process writes,
        # loop heartbeats or PID replacement. Repeated phase cycles do not reset.
        if snapshot.state not in {"checking", "downloading", "installing", "updating", "verifying"}:
            return
        progress = snapshot.progress
        if progress is None:
            key, value = snapshot.state, 0
        elif (isinstance(progress, tuple) and len(progress) == 2
              and isinstance(progress[0], str) and type(progress[1]) is int
              and progress[1] >= 0):
            key, value = snapshot.state + ":" + progress[0], progress[1]
        else:
            self.fail("zzz_launcher_invalid_progress")
        previous = self.progress.get(key)
        if previous is None and len(self.progress) >= MAX_PROGRESS_PHASES:
            self.fail("zzz_launcher_progress_phase_limit")
        if previous is not None and value < previous:
            self.fail("zzz_launcher_progress_regressed")
        if previous is None or value > previous:
            self.progress[key] = value
            self.last_progress = self.clock()
            # Progress values are launcher observations, not daily completion.
            self.emit(snapshot.state, progressKind=key, progressValue=value)

    def invoke(self, action, snapshot):
        self.guard()
        self.validate(snapshot)
        if action not in snapshot.actions:
            self.fail("zzz_launcher_required_action_unavailable")
        receipt = self.driver.invoke(action, snapshot)
        self.guard()
        # Only an actual invocation starts a new progress epoch. A new phase or
        # PID alone cannot refill a budget or erase the high-water mark.
        self.progress.clear()
        self.last_progress = self.clock()
        self.emit("action", action=action)
        return receipt

    def run(self):
        self.guard()
        self.driver.prepare()
        self.guard()
        checked = verified = False
        pending = None
        saw_phase = False
        checks = updates = verifies = 0
        ever_ready = False
        pending_receipt = None
        while True:
            self.guard()
            try:
                snapshot = self.driver.observe()
            except GameLaunchCancelled:
                self.guard()  # Translate a probe deadline into its own reason.
                raise
            self.guard()
            self.validate(snapshot)
            state = snapshot.state
            if state in {"credentials", "captcha"}:
                raise GameLaunchHumanRequired(
                    "zzz_launcher_" + state + "_required",
                    "Official launcher requires the user's authentication.",
                    detail={"phase": "client-update"}, process_ids=self.pids)
            if state in {"unknown", "failed"}:
                self.fail("zzz_launcher_" + state)
            if state == "waiting":
                if not ever_ready and self.clock() - self.started_at >= UI_READY_SECONDS:
                    self.fail("zzz_launcher_ui_timeout")
            else:
                ever_ready = True
            self.observe_progress(snapshot)
            if pending == "check" and state == "checking":
                saw_phase = True
            elif pending == "verify" and state == "verifying":
                saw_phase = True
            elif pending == "update" and state in {"downloading", "installing", "updating", "verifying"}:
                saw_phase = True
            # A cached completion label cannot prove that our action ran. Until
            # the real driver supplies a correlated completion receipt, require
            # a fresh busy phase after this invocation even for explicit labels.
            correlated_check = (pending_receipt is not None and snapshot.completion_receipt == pending_receipt
                                and state == "up-to-date")
            if pending == "check" and (saw_phase or correlated_check) and state in {"up-to-date", "ready"}:
                checked, pending = True, None
            elif pending == "verify" and saw_phase and state in {"verification-complete", "ready"}:
                verified, pending = True, None
            elif pending == "update" and state == "ready" and saw_phase:
                checked, verified, pending = False, False, None
            if state == "update-available" and pending != "update":
                if updates >= 1:
                    self.fail("zzz_launcher_update_repeated")
                pending_receipt = self.invoke("update", snapshot)
                updates += 1
                pending, saw_phase = "update", False
            elif state in {"ready", "up-to-date", "verification-complete"} and pending is None:
                if not checked:
                    if checks >= 2:
                        self.fail("zzz_launcher_check_repeated")
                    pending_receipt = self.invoke("check", snapshot)
                    checks += 1
                    pending, saw_phase = "check", False
                elif not verified:
                    if verifies >= 1 + updates:
                        self.fail("zzz_launcher_verify_repeated")
                    pending_receipt = self.invoke("verify", snapshot)
                    verifies += 1
                    pending, saw_phase = "verify", False
                elif state == "ready":
                    self.guard()
                    self.emit("ready", terminal=True, officialCheck=True, officialIntegrity=True)
                    return {"state": "launcher-verified", "updates": updates,
                            "checks": checks, "verifications": verifies}
            if self.clock() - self.last_progress >= NO_PROGRESS_SECONDS:
                self.fail("zzz_launcher_progress_stalled")
            self.sleep(POLL_SECONDS)


def ensure_zzz_launcher(service, game_path, observer, started_at, *,
                        cancel_requested, deadline_at, authority_valid, native_context=None):
    """Called only by the existing ZZZ official-tool handoff branch."""
    def notify(state, detail, pids):
        service._notify(
            observer, "ZZZ", "client-update", started_at, frozenset({"hyp.exe", "launcher.exe"}),
            {"state": state, "phase": "client-update", "purpose": "zzz-official-launcher-preflight",
             "parentPid": __import__("os").getpid(), **detail}, process_ids=pids)

    gate = None
    def interrupted():
        return gate is None or gate.interrupted()

    driver = HoYoPlayLauncher(service, Path(game_path), notify, interrupted, context=native_context)
    gate = ZzzLauncherPreflight(
        driver, Path(game_path), cancel_requested=cancel_requested,
        authority_valid=authority_valid, deadline_at=deadline_at, notify=notify)
    try:
        result = gate.run()
        gate.guard()
        return result
    except GameLaunchCancelled:
        gate.guard()
        gate.emit("cancelled", terminal=True)
        raise
    except GameLaunchError as error:
        gate.emit("blocked", terminal=True, reasonCode=error.reason_code or "zzz_launcher_failed")
        raise
    finally:
        driver.close()

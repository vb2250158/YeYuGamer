"""Observe upstream OK-WW steps and hand off its native saved-login window.

YeYu does not select accounts, mutate done_set, or call login helpers.
Official MultiAccountDailyTask owns switching. This module records the
official current account so Adapter can attribute already-executed
DailyTask stages to the matching YeYu GameRun, and it activates the
window the official task already selected before that task sends input.

The official account-login step also needs one handoff: the game's saved-login
control is owned by the game process, and the official ``PostMessage`` click on
it is accepted by the SDK but never advances the page (verified on 2026-09-12:
12 minutes of posted clicks with the game and the dialog in the foreground left
the dialog unchanged, while one real click on the same officially selected
control logged in immediately). Two official selections are handed off: the
classic native ``#32770`` dialog ``Button``, and the in-client page, which the
official reaches by targeting the game window itself with client coordinates.
Only the already-selected control is eligible below, and only one real click is
issued per official click.

The account list lives on that same in-client page, so the official
``MultiAccountDailyTask`` switch needs the same handoff: on 2026-09-12 the
posted clicks on the account row left the displayed account unchanged and the
official task raised ``Failed to switch account``. ``_observe_account_selection``
scopes that handoff to the official ``_click_account_in_list`` call only.

On 2026-09-13 the scoped handoff was measured inert: the official switch again
posted five account-row clicks and raised, and no handoff was issued because
the official point resolved to an hwnd the saved-login predicate does not
accept (``game-ui-account-unconfirmed-before-raw``). The click is therefore
also recorded as a diagnostic and resolved against the hwnd the official point
is actually relative to (see ``_game_window_account_selection_target``).
"""
from __future__ import annotations

from datetime import datetime
from contextvars import ContextVar
from functools import wraps
import json
import os
import sys
from OfficialStepCapture import install_recognition_observers, recognition_scope


_current_account = {"label": None, "first_label": None}
_captured_operations = set()
_ACCOUNT_CURRENT = object()
_in_official_login = ContextVar("yeyu_ww_official_login", default=False)
_in_official_account_selection = ContextVar("yeyu_ww_official_account_selection", default=False)


def _observe_login_input(task_type, interaction_type, describe_target, prepare_target=None) -> None:
    """Record upstream-selected input targets without sending any extra input."""
    original_login = getattr(task_type, "wait_login", None)
    original_position = getattr(interaction_type, "update_mouse_pos", None)
    if not callable(original_login) or not callable(original_position):
        return
    if getattr(original_login, "_yeyu_input_observer", False):
        return

    @wraps(original_login)
    def login(self, *args, **kwargs):
        token = _in_official_login.set(True)
        try:
            return original_login(self, *args, **kwargs)
        finally:
            _in_official_login.reset(token)

    @wraps(original_position)
    def position(self, *args, **kwargs):
        result = original_position(self, *args, **kwargs)
        if _in_official_login.get():
            if prepare_target is not None:
                try:
                    activation = prepare_target(self)
                    if activation is not None:
                        _emit("login-window-activation", "observed", activation)
                except Exception as error:
                    _emit("login-window-activation", "failed", type(error).__name__ + ": " + str(error))
            try:
                _emit("official-login-input", "observed", describe_target(self, result))
            except Exception:
                pass
        return result

    login._yeyu_input_observer = True
    task_type.wait_login = login
    interaction_type.update_mouse_pos = position


def _prepare_native_login_input(interaction):
    """Activate the official target before its existing native login click.

    This runs on the upstream task thread after target resolution and before
    PostMessage.click sends input. It neither clicks nor decides login success;
    when this activation cannot help, ``_observe_native_login_click`` delivers
    that same official click as real input instead.
    """
    import win32gui
    import win32process
    hwnd = interaction.hwnd
    window = interaction.hwnd_window
    if win32gui.GetClassName(hwnd) != "Button":
        return None
    # The SDK owner-draws its label; the actual login control is named Button2.
    # Selection belongs to official wait_login, not GetWindowText recognition.
    parent = win32gui.GetParent(hwnd)
    if not parent or win32gui.GetClassName(parent) != "#32770":
        return None
    pid = win32process.GetWindowThreadProcessId(window.hwnd)[1]
    current, seen = hwnd, set()
    while current and current != window.hwnd and current not in seen:
        seen.add(current)
        if win32process.GetWindowThreadProcessId(current)[1] != pid:
            return None
        current = win32gui.GetParent(current)
    if current != window.hwnd or window.is_foreground():
        return None
    requested = window.bring_to_front()
    return json.dumps({"targetHwnd": hwnd, "dialogHwnd": parent, "targetPid": pid,
                       "officialActivationReturned": bool(requested),
                       "foregroundConfirmed": bool(window.is_foreground()),
                       "trigger": "Official wait_login selected a native dialog Button while game is not foreground",
                       "handoff": "Original PostMessage click; official wait_login confirms subsequent state"})


def _observe_native_login_click(interaction_type) -> None:
    """Issue the official native-login click as real input exactly once.

    The official task still decides which control to click and when this
    wrapper only changes how that single click reaches it, and only when the
    control the official call resolved is the game's bound saved-login
    dialog. Every other official click keeps the original implementation and
    the original return value untouched.
    """
    original_click = getattr(interaction_type, "click", None)
    if not callable(original_click) or getattr(original_click, "_yeyu_native_login_click", False):
        return
    # Resolve the official target without re-running the diagnostics wrapper
    # that _observe_login_input installed over the same method.
    resolved_position = getattr(interaction_type, "update_mouse_pos", None)
    resolve_target = getattr(resolved_position, "__wrapped__", resolved_position)

    @wraps(original_click)
    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
        # A same-process native Button is not necessarily a saved-login
        # control. Only the official login/account-selection call owns this
        # handoff; ordinary clicks keep their original target resolution too.
        if not (_in_official_login.get() or _in_official_account_selection.get()):
            return original_click(self, x, y, move_back=move_back, name=name,
                                  down_time=down_time, move=move, key=key)
        target = None
        packed_position = None
        if key == "left" and callable(resolve_target):
            try:
                packed_position = resolve_target(self, x, y)
                target = _native_login_click_target(self, packed_position)
            except Exception as error:
                _emit("ww-login-window-click", "failed",
                      "handoff_target_error:{}:{}".format(type(error).__name__, str(error)[:1000]))
        if target is None and _in_official_account_selection.get():
            # The account list has no working official PostMessage path, so a
            # click the strict saved-login predicate does not accept is still
            # recorded, then resolved against the window the official point is
            # actually relative to. Observation first: without this record a
            # rejected handoff and a missing handoff look identical.
            _emit("ww-login-window-click", "diagnostic",
                  _describe_account_selection_click(self, x, y, packed_position))
            target = _game_window_account_selection_target(self, packed_position)
        if target is not None:
            try:
                _issue_physical_click(target)
            except Exception as error:
                _emit("ww-login-window-click", "failed",
                      "handoff_input_error:{}:{}".format(type(error).__name__, str(error)[:1000]))
                if not getattr(error, "input_started", False):
                    # Keep the official delivery path when the handoff refused
                    # input. Once a button event may have been sent, falling
                    # back would deliver the same official click twice.
                    return original_click(self, x, y, move_back=move_back, name=name,
                                          down_time=down_time, move=move, key=key)
            else:
                _emit("ww-login-window-click", "issued",
                      "official {flow} selected {kind} control hwnd={hwnd} at ({x},{y}); "
                      "one real click issued".format(
                          flow="login" if _in_official_login.get() else "account-selection", **target))
            return None
        return original_click(self, x, y, move_back=move_back, name=name, down_time=down_time, move=move, key=key)

    click._yeyu_native_login_click = True
    interaction_type.click = click


def _native_login_click_target(interaction, packed_position=None):
    """Return the game's own saved-login control the official click targeted.

    Two official selections are eligible, both owned by the game process:

    * a native ``Button`` inside a ``#32770`` dialog whose parent chain ends
      at the bound game window (the classic saved-login dialog), and
    * the bound game window itself, which is how the SDK's in-client login
      page resolves. Observed 2026-09-12 on the shared WW run:
      ``official-login-input`` reported ``targetClass=UnrealWindow`` with
      client coordinates, and the official ``PostMessage`` click on that
      window never advanced the page.

    Any other target is not eligible, and the in-game case is additionally
    restricted to the official ``wait_login`` flow so ordinary in-game clicks
    keep the original implementation untouched.
    """
    import win32gui
    import win32process
    hwnd = interaction.hwnd
    window = interaction.hwnd_window
    if not hwnd or not window:
        return None
    if hwnd == window.hwnd:
        return _game_window_login_click_target(interaction, packed_position)
    if win32gui.GetClassName(hwnd) != "Button":
        return None
    dialog = win32gui.GetParent(hwnd)
    if not dialog or win32gui.GetClassName(dialog) != "#32770":
        return None
    pid = win32process.GetWindowThreadProcessId(window.hwnd)[1]
    current, seen = hwnd, set()
    while current and current != window.hwnd and current not in seen:
        seen.add(current)
        if win32process.GetWindowThreadProcessId(current)[1] != pid:
            return None
        current = win32gui.GetParent(current)
    if current != window.hwnd:
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return {"hwnd": hwnd, "dialog": dialog, "pid": pid, "kind": "native-dialog",
            "x": (left + right) // 2, "y": (top + bottom) // 2}


def _unpack_client_point(packed_position):
    """Decode the official packed client point into signed x/y."""
    low = packed_position & 0xffff
    high = (packed_position >> 16) & 0xffff
    x = low - 0x10000 if low & 0x8000 else low
    y = high - 0x10000 if high & 0x8000 else high
    return x, y


def _game_window_login_click_target(interaction, packed_position):
    """Resolve the in-client login control the official selected.

    The SDK renders its saved-login page inside the game window when no native
    dialog is used, and reports the control as client coordinates on that
    window. The official ``PostMessage`` click does not advance such a page, so
    the same officially selected point is delivered as one real click instead.
    The same page carries the account list, so the official account-selection
    click is eligible too (see ``_observe_account_selection``).
    """
    if not (_in_official_login.get() or _in_official_account_selection.get()) or packed_position is None:
        return None
    import win32gui
    import win32process
    window = interaction.hwnd_window
    client_x, client_y = _unpack_client_point(packed_position)
    try:
        screen_x, screen_y = win32gui.ClientToScreen(window.hwnd, (client_x, client_y))
    except Exception:
        return None
    return {"hwnd": interaction.hwnd, "dialog": window.hwnd,
            "pid": win32process.GetWindowThreadProcessId(window.hwnd)[1],
            "kind": "game-window", "x": screen_x, "y": screen_y,
            "clientPoint": (client_x, client_y)}


def _game_window_account_selection_target(interaction, packed_position):
    """Resolve the official account-list click against the game's own windows.

    The strict saved-login predicate only accepts the bound window itself or a
    native ``Button`` inside a ``#32770`` dialog. The in-client account page is
    reached by targeting an Unreal render/child hwnd instead, which that
    predicate rejects, so the official ``_click_account_in_list`` click fell
    through to ``PostMessage`` and never advanced the page.

    ``update_mouse_pos`` reports the official point relative to the hwnd it
    resolved (``_dynamic_target_hwnd``), so the same hwnd owns the screen
    point. Only the already-selected official click changes delivery; the
    official task still decides which account row, how often, and whether the
    displayed account matched. Scoped to ``_click_account_in_list``, where the
    official PostMessage click is proven not to advance the page, so this
    cannot regress a working official click.
    """
    if not _in_official_account_selection.get() or packed_position is None:
        return None
    import win32gui
    import win32process
    window = interaction.hwnd_window
    base = getattr(window, "hwnd", None)
    if not base:
        return None
    try:
        base_pid = win32process.GetWindowThreadProcessId(base)[1]
    except Exception:
        return None
    candidates = []
    for hwnd in (getattr(interaction, "_dynamic_target_hwnd", None),
                 getattr(window, "top_hwnd", None), base):
        if hwnd and hwnd not in candidates:
            candidates.append(hwnd)
    client_x, client_y = _unpack_client_point(packed_position)
    for hwnd in candidates:
        try:
            if not win32gui.IsWindow(hwnd):
                continue
            if win32process.GetWindowThreadProcessId(hwnd)[1] != base_pid:
                continue
            screen_x, screen_y = win32gui.ClientToScreen(hwnd, (client_x, client_y))
        except Exception:
            continue
        return {"hwnd": hwnd, "dialog": hwnd, "pid": base_pid,
                "kind": "game-window-fallback", "x": screen_x, "y": screen_y,
                "clientPoint": (client_x, client_y)}
    return None


def _describe_account_selection_click(interaction, x, y, packed_position):
    """Record why an official account-list click was not handed off."""
    parts = ["interactionClass=" + type(interaction).__name__,
             "officialX={!r}".format(x), "officialY={!r}".format(y)]
    try:
        import win32gui
        window = interaction.hwnd_window
        for label, hwnd in (("interactionHwnd", getattr(interaction, "hwnd", None)),
                            ("windowHwnd", getattr(window, "hwnd", None)),
                            ("windowTopHwnd", getattr(window, "top_hwnd", None)),
                            ("dynamicTargetHwnd", getattr(interaction, "_dynamic_target_hwnd", None))):
            if not hwnd:
                parts.append("%s=None" % label)
                continue
            try:
                parts.append("%s=%s(%s)" % (label, hwnd, win32gui.GetClassName(hwnd)))
            except Exception:
                parts.append("%s=%s(<class error>)" % (label, hwnd))
        if packed_position is not None:
            client_x, client_y = _unpack_client_point(packed_position)
            parts.append("client=(%s,%s)" % (client_x, client_y))
    except Exception as error:
        parts.append("describe_error=" + type(error).__name__)
    return "diagnostic: targeted=false; " + "; ".join(parts)


class _PhysicalClickError(RuntimeError):
    def __init__(self, detail, *, input_started):
        super().__init__(detail)
        self.input_started = input_started


def _issue_physical_click(target) -> None:
    """Send one real click at the resolved control centre.

    Validate window ownership before activation, then resolve and validate the
    official point after activation and again just before the button event. A
    covered game must first be raised; a stale or moved target is refused. The
    ALT nudge and window raise repeat the launcher handoff sequence, which is
    what defeats a stuck foreground lock on this host.
    """
    import ctypes
    import time
    import win32api
    import win32con
    import win32gui
    import win32process
    user32 = ctypes.windll.user32

    def validate_windows():
        for hwnd in (target["hwnd"], target["dialog"]):
            if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != target["pid"]:
                raise RuntimeError("native_login_window_changed hwnd={}".format(hwnd))

    def resolve_point():
        if target.get("kind") == "native-dialog":
            left, top, right, bottom = win32gui.GetWindowRect(target["hwnd"])
            return (left + right) // 2, (top + bottom) // 2
        if "clientPoint" in target:
            return win32gui.ClientToScreen(target["hwnd"], target["clientPoint"])
        return int(target["x"]), int(target["y"])

    def validate_point(point):
        foreground = win32gui.GetForegroundWindow()
        foreground_pid = win32process.GetWindowThreadProcessId(foreground)[1] if foreground else None
        if foreground_pid != target["pid"]:
            raise RuntimeError("native_login_foreground_foreign expectedPid={} hwnd={} pid={}".format(
                target["pid"], foreground, foreground_pid))
        hit = win32gui.WindowFromPoint(point)
        hit_pid = win32process.GetWindowThreadProcessId(hit)[1] if hit else None
        if hit_pid != target["pid"]:
            raise RuntimeError("native_login_point_foreign expectedHwnd={} expectedPid={} point={} hitHwnd={} hitPid={}".format(
                target["hwnd"], target["pid"], point, hit, hit_pid))

    validate_windows()
    # A popup (notably ComboLBox) can own foreground while its game owner is
    # minimized. Raising only that popup leaves the official point covered by
    # another app. Restore the same-process root owner before raising the
    # selected dialog; the task still selects the control and confirms login.
    root = win32gui.GetAncestor(target["dialog"], 3)  # GA_ROOTOWNER
    if not root or not win32gui.IsWindow(root) or win32process.GetWindowThreadProcessId(root)[1] != target["pid"]:
        raise RuntimeError("native_login_root_owner_foreign")
    if win32gui.IsIconic(root):
        win32gui.ShowWindow(root, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    if root != target["dialog"]:
        win32gui.BringWindowToTop(root)
        win32gui.SetForegroundWindow(root)
    win32gui.BringWindowToTop(target["dialog"])
    win32gui.SetForegroundWindow(target["dialog"])
    time.sleep(0.1)
    validate_windows()
    point = resolve_point()
    validate_point(point)
    win32api.SetCursorPos(point)
    time.sleep(0.15)
    validate_windows()
    if resolve_point() != point:
        raise RuntimeError("native_login_point_changed")
    validate_point(point)
    target["x"], target["y"] = point
    try:
        # Treat even a failing down API call as possibly delivered. Never fall
        # back to another click after this point, and always request release.
        try:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
        finally:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as error:
        raise _PhysicalClickError("native_login_input_error:" + str(error)[:500], input_started=True) from error


def _describe_login_input(interaction, packed_position):
    import win32gui
    import win32process
    hwnd = interaction.hwnd
    foreground = win32gui.GetForegroundWindow()
    return json.dumps({
        "targetHwnd": hwnd,
        "targetPid": win32process.GetWindowThreadProcessId(hwnd)[1],
        "targetClass": win32gui.GetClassName(hwnd),
        "targetRect": win32gui.GetWindowRect(hwnd),
        "targetEnabled": bool(win32gui.IsWindowEnabled(hwnd)),
        "foregroundHwnd": foreground,
        "baseHwnd": interaction.hwnd_window.hwnd,
        "topHwnd": interaction.hwnd_window.top_hwnd,
        "clientX": (packed_position & 0xffff) - (0x10000 if packed_position & 0x8000 else 0),
        "clientY": ((packed_position >> 16) & 0xffff) - (0x10000 if packed_position & 0x80000000 else 0),
    }, ensure_ascii=False)


def _normalize_account(account):
    if not account:
        return account
    return str(account).lower().replace("0", "o").replace(".con", ".com")


def _install_upstream_logging() -> None:
    # Called from an upstream task method, after the tool has initialized its
    # logger. A missing candidate path or an unwritable log is non-authoritative.
    try:
        from upstream_logging import install as install_logging
        install_logging(os.environ.get("YEYU_GAMER_UPSTREAM_LOG_FILE", ""))
    except Exception:
        pass


def _emit(operation: str, state: str, detail: str, observed_account=_ACCOUNT_CURRENT) -> None:
    try:
        path = os.environ.get("YEYU_GAMER_STAGE_FILE", "")
        if not path:
            return
        record = {
            "operation": operation,
            "state": state,
            "detail": detail,
            "at": datetime.now().astimezone().isoformat(),
        }
        account = _current_account.get("label") if observed_account is _ACCOUNT_CURRENT else observed_account
        if account:
            record["account"] = account
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _set_current_account(account) -> None:
    normalized = _normalize_account(account)
    _current_account["label"] = normalized or None
    if normalized and _current_account["first_label"] is None:
        # The official task confirms its initial account only after DailyTask.
        # Continue charging that account's later prerequisites to startup;
        # confirmation must not create a second allowance for the same run.
        _current_account["first_label"] = normalized
    if normalized:
        _emit("official-account", "detected", "account=" + normalized)


class _DailyConditionCapture:
    """Read the upstream DailyTask frame after it has made its own decision."""

    _locals_to_operations = {
        "need_nightmare": "farm-nightmare-daily-echo",
        "need_stamina": "spend-waveplates",
    }

    def __init__(self, wrapped_run) -> None:
        self.conditions: dict[str, bool] = {}
        self._codes = set()
        current = wrapped_run
        while callable(current):
            code = getattr(current, "__code__", None)
            if code is not None:
                self._codes.add(code)
            current = getattr(current, "__wrapped__", None)
        self._callback = self._observe_return
        self._installed = False

    def _observe_return(self, frame, event, _arg) -> None:
        if event != "return" or frame.f_code not in self._codes:
            return
        try:
            for local_name in self._locals_to_operations:
                value = frame.f_locals.get(local_name)
                if type(value) is bool:
                    self.conditions[local_name] = value
        except Exception:
            # Observation can never alter an upstream task return or exception.
            pass

    def start(self) -> None:
        try:
            # Do not replace another profiler: it could belong to the upstream
            # tool or a debugger. Missing condition evidence simply stays absent.
            if sys.getprofile() is not None:
                return
            sys.setprofile(self._callback)
            self._installed = True
        except Exception:
            self._installed = False

    def stop(self) -> None:
        if not self._installed:
            return
        try:
            # Only undo our own current-thread hook. An upstream routine that
            # installed another hook while running remains authoritative.
            if sys.getprofile() is self._callback:
                sys.setprofile(None)
        except Exception:
            pass
        finally:
            self._installed = False

    def emit_skips(self) -> None:
        for local_name, operation in self._locals_to_operations.items():
            if self.conditions.get(local_name) is False:
                detail = "upstream:DailyTask.{}=false; official task body not entered".format(local_name)
                _emit(operation, "skipped", detail)


def _observe_daily_task(task_type) -> None:
    """Observe DailyTask's own conditional decisions without reimplementing them."""
    original = getattr(task_type, "run", None)
    if not callable(original) or getattr(original, "_yeyu_passive_observer", False):
        return

    @wraps(original)
    def observed(self, *args, **kwargs):
        _install_upstream_logging()
        _captured_operations.clear()
        _emit("upstream-daily-task", "started", "upstream:{}:started".format(original.__name__))
        capture = _DailyConditionCapture(original)
        capture.start()
        try:
            result = original(self, *args, **kwargs)
        except Exception as error:
            _emit("upstream-daily-task", "failed", "upstream:{}: {}".format(type(error).__name__, error))
            raise
        finally:
            capture.stop()
        if result is not False:
            capture.emit_skips()
        _emit("upstream-daily-task", "completed" if result is not False else "observed_false",
              "upstream:{}:returned:{!r}".format(original.__name__, result))
        return result

    observed._yeyu_passive_observer = True
    setattr(task_type, "run", observed)


def _observe(task_type, method_name: str, operation: str) -> None:
    original = getattr(task_type, method_name, None)
    if not callable(original) or getattr(original, "_yeyu_passive_observer", False):
        return

    @wraps(original)
    def observed(self, *args, **kwargs):
        if method_name == "open_daily" and (getattr(self, "config", {}).get("Daily Progress Inspection Only", False)
                                            or getattr(self, "config", {}).get("Daily Reward Status Inspection Only", False)):
            return original(self, *args, **kwargs)
        _install_upstream_logging()
        _emit(operation, "started", "upstream:{}:started".format(original.__name__))
        # Freeze identity at entry: a later successful login must never relabel
        # a screenshot taken before the official account was established.
        account = _current_account.get("label")
        capture_context = operation != "attach-world" or operation not in _captured_operations
        _captured_operations.add(operation)
        def emit(_path, op, state, detail):
            if state == "step_artifact" and account is None:
                detail += "; accountAttribution=unconfirmed"
            _emit(op, state, detail, observed_account=account)
        try:
            with recognition_scope(self, os.environ.get("YEYU_GAMER_STAGE_FILE", "") if capture_context else None,
                                   operation, emit, "ww", identity=account,
                                   max_images=8 if method_name == "inspect_battle_pass_status" else 2, first_distinct=0,
                                   verification_kind=("game-ui-key-step-verification-raw" if method_name == "inspect_daily_progress"
                                                      else "game-ui-reward-verification-raw" if method_name == "inspect_daily_reward_status" else None),
                                   byte_budget_identity=account if account != _current_account["first_label"] else None):
                result = original(self, *args, **kwargs)
        except Exception as error:
            _emit(operation, "failed", "upstream:{}: {}".format(type(error).__name__, error))
            raise
        _emit(operation, "completed" if result is not False else "observed_false",
              "upstream:{}:returned:{!r}".format(original.__name__, result))
        return result

    observed._yeyu_passive_observer = True
    setattr(task_type, method_name, observed)


def _observe_battle_pass_page(task_type):
    """Retain each named official OCR input without adding task terminals or IO."""
    original = getattr(task_type, "inspect_battle_pass_page", None)
    if not callable(original) or getattr(original, "_yeyu_passive_observer", False):
        return

    @wraps(original)
    def observed(self, page, *args, **kwargs):
        account = _current_account.get("label")
        enabled = getattr(self, "config", {}).get("Battle Pass Status Inspection Only", False)
        known_page = isinstance(page, str) and page in {"tasks", "rewards"}
        path = os.environ.get("YEYU_GAMER_STAGE_FILE", "") if enabled and known_page else None
        def emit(_path, operation, state, detail):
            if state == "step_artifact" and account is None:
                detail += "; accountAttribution=unconfirmed"
            _emit(operation, state, detail, observed_account=account)
        with recognition_scope(self, path, "claim-battle-pass", emit, "ww", identity=account,
                               max_images=8, first_distinct=0,
                               verification_kind="game-ui-reward-verification-raw", verification_page=page if known_page else None,
                               byte_budget_identity=account if account != _current_account["first_label"] else None):
            return original(self, page, *args, **kwargs)

    observed._yeyu_passive_observer = True
    setattr(task_type, "inspect_battle_pass_page", observed)


def _observe_account_identity(task_type, method_name: str) -> None:
    original = getattr(task_type, method_name, None)
    if not callable(original) or getattr(original, "_yeyu_passive_observer", False):
        return

    @wraps(original)
    def observed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        try:
            _set_current_account(result)
        except Exception:
            pass
        return result

    observed._yeyu_passive_observer = True
    setattr(task_type, method_name, observed)


def _observe_account_selection(task_type) -> None:
    """Deliver the official account-list click to real input.

    ``MultiAccountDailyTask`` switches accounts by clicking the login page's
    account list, which lives on the same page the official ``PostMessage``
    click never advances (see module docstring). On 2026-09-12 the official
    switch therefore logged six ``left_click`` attempts whose displayed
    account never changed, then raised ``Failed to switch account``.

    Only ``_click_account_in_list`` is scoped: the official task still decides
    which account to select, how often to retry, and whether the displayed
    account matches. This wrapper changes only how that single official click
    reaches the control, exactly like the existing saved-login handoff.
    """
    original = getattr(task_type, "_click_account_in_list", None)
    if not callable(original) or getattr(original, "_yeyu_account_selection_scope", False):
        return

    @wraps(original)
    def clicking(self, *args, **kwargs):
        token = _in_official_account_selection.set(True)
        try:
            return original(self, *args, **kwargs)
        finally:
            _in_official_account_selection.reset(token)

    clicking._yeyu_account_selection_scope = True
    task_type._click_account_in_list = clicking


def _observe_multi_account(task_type) -> None:
    original = getattr(task_type, "run", None)
    if callable(original) and not getattr(original, "_yeyu_passive_observer", False):
        @wraps(original)
        def observed(self, *args, **kwargs):
            _current_account["label"] = None
            _current_account["first_label"] = None
            _emit("official-account", "started", "upstream:MultiAccountDailyTask.run")
            _emit("upstream-multi-account-task", "started", "upstream:MultiAccountDailyTask.run")
            try:
                result = original(self, *args, **kwargs)
            except Exception as error:
                _emit("upstream-multi-account-task", "failed",
                      f"upstream:MultiAccountDailyTask.run:{type(error).__name__}: {error}")
                raise
            else:
                _emit("upstream-multi-account-task", "failed" if result is False else "completed",
                      f"upstream:MultiAccountDailyTask.run:returned:{result}")
                return result
            finally:
                _current_account["label"] = None
                _current_account["first_label"] = None
        observed._yeyu_passive_observer = True
        setattr(task_type, "run", observed)
    _observe_account_selection(task_type)
    _observe_account_identity(task_type, "_detect_current_account_from_login")
    _observe_account_identity(task_type, "_select_and_login_account")


def install() -> None:
    """Wrap existing methods only; do not create an upstream task instance."""
    from src.task.DailyTask import DailyTask
    from src.task.BaseWWTask import BaseWWTask
    from src.task.NightmareNestTask import NightmareNestTask
    from src.task.TacetTask import TacetTask
    from src.task.ForgeryTask import ForgeryTask
    from src.task.SimulationTask import SimulationTask

    if getattr(DailyTask, "_yeyu_passive_observer_v3", False):
        return
    install_recognition_observers()
    try:
        from ok.device.interaction_methods.post_message import PostMessageInteraction
        _observe_login_input(BaseWWTask, PostMessageInteraction, _describe_login_input, _prepare_native_login_input)
        _observe_native_login_click(PostMessageInteraction)
    except Exception:
        pass
    _observe_daily_task(DailyTask)
    _observe(BaseWWTask, "ensure_main", "attach-world")
    _observe(DailyTask, "open_daily", "inspect-daily-progress")
    _observe(DailyTask, "inspect_daily_progress", "inspect-daily-progress")
    _observe(DailyTask, "inspect_daily_reward_status", "claim-daily-reward")
    _observe(DailyTask, "inspect_battle_pass_status", "claim-battle-pass")
    _observe_battle_pass_page(DailyTask)
    # DailyTask uses run() for the official "Auto Farm all Nightmare Nest"
    # additional-task branch, while its daily-echo branch calls
    # run_capture_mode().  Both are upstream entry points for the same selected
    # operation, and each wrapper only observes its original call.
    _observe(NightmareNestTask, "run", "farm-nightmare-daily-echo")
    _observe(NightmareNestTask, "run_capture_mode", "farm-nightmare-daily-echo")
    _observe(TacetTask, "farm_tacet", "spend-waveplates")
    _observe(ForgeryTask, "farm_forgery", "spend-waveplates")
    _observe(SimulationTask, "farm_simulation", "spend-waveplates")
    _observe(DailyTask, "claim_daily", "claim-daily-reward")
    _observe(DailyTask, "claim_mail", "claim-mail")
    _observe(DailyTask, "claim_battle_pass", "claim-battle-pass")
    try:
        from src.task.MultiAccountDailyTask import MultiAccountDailyTask
        _observe_multi_account(MultiAccountDailyTask)
        from ok.core.events import communicate
        from OfficialTaskExit import install_failure_exit
        install_failure_exit(MultiAccountDailyTask, communicate, "WWAdapter")
    except Exception:
        pass
    DailyTask._yeyu_passive_observer_v3 = True

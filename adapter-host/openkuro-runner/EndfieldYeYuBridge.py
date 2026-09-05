"""Run-scoped YeYu Gamer bridge for OK-EF's formal GUI DailyTask.

The PyAppify updater may replace the whole ``working`` tree.  The Adapter build
therefore persists this module before hashing the selected tool version, and
the runtime revalidates the hook immediately before the formal GUI starts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

import cv2


def _load_run_bridge() -> dict | None:
    path = Path(__file__).resolve().parents[2] / "configs" / "YeYuGamerRun.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(document.get("createdAtUnix", 0))
        stage_file = Path(str(document["stageFile"])).resolve()
        selected = document["selectedOperations"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        document.get("schemaVersion") != 1
        or document.get("active") is not True
        or not -300 <= age <= 3600
        or not stage_file.is_absolute()
        or not isinstance(selected, list)
        or not selected
        or not all(isinstance(item, str) for item in selected)
    ):
        return None
    return {"stageFile": stage_file, "selected": set(selected)}


class _StageReporter:
    def __init__(self, bridge: dict):
        self.path: Path = bridge["stageFile"]
        self.selected: set[str] = bridge["selected"]

    def selected_for_run(self, operation: str) -> bool:
        return operation in self.selected

    def write(self, operation: str, state: str, detail: object = "") -> None:
        if not self.selected_for_run(operation):
            return
        record = {
            "operation": operation,
            "state": state,
            "detail": str(detail)[:1000],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def wrap(self, operation: str, callback):
        def run():
            self.write(operation, "started")
            try:
                result = callback()
            except Exception as error:
                self.write(operation, "failed", error)
                raise
            if result is False:
                self.write(operation, "failed", "upstream callback returned False")
            else:
                self.write(operation, "completed")
            return result

        return run


def install_yeyu_gamer_bridge() -> None:
    from src.tasks.daily.daily_task_runner import DailyTaskRunner
    from src.tasks.onetime.DailyTask import DailyTask
    from ok.task.TaskExecutor import TaskExecutor as FrameworkTaskExecutor

    if getattr(DailyTask, "_yeyu_gamer_bridge_v2", False):
        return

    if not getattr(FrameworkTaskExecutor, "_yeyu_gamer_titlebar_ratio_v1", False):
        original_resolution_check = FrameworkTaskExecutor.check_frame_and_resolution

        def manager_resolution_check(self, supported_ratio, min_size, time_out=8.0):
            supported, resolution = original_resolution_check(
                self, supported_ratio, min_size, time_out
            )
            if supported or supported_ratio != "16:9" or min_size is None:
                return supported, resolution
            try:
                width_text, height_text = resolution.lower().split("x", 1)
                width, height = int(width_text), int(height_text)
                expected_height = round(width * 9 / 16)
                titlebar_deficit = expected_height - height
            except (AttributeError, TypeError, ValueError):
                return supported, resolution
            if (
                width >= int(min_size[0])
                and expected_height >= int(min_size[1])
                and 0 <= titlebar_deficit <= 64
            ):
                # A maximized Win32 game window captures only its client area:
                # 1920x1057 or 2560x1417 instead of the outer 16:9 size.  The
                # framework's existing out-of-ratio coordinate path centers a
                # 16:9 working region correctly, so accept only this bounded
                # title-bar deficit and keep every other ratio guard intact.
                self.logger.info(
                    "YeYu Gamer run: accepted maximized 16:9 client area "
                    f"{width}x{height} (title-bar deficit {titlebar_deficit}px)"
                )
                return True, resolution
            return supported, resolution

        FrameworkTaskExecutor.check_frame_and_resolution = manager_resolution_check
        FrameworkTaskExecutor._yeyu_gamer_titlebar_ratio_v1 = True
    original_run = DailyTask.run
    original_finally = DailyTask.run_daily_finally

    def manager_run(self):
        bridge = _load_run_bridge()
        if bridge is None:
            return original_run(self)
        reporter = _StageReporter(bridge)
        # Upstream activation unconditionally calls SW_RESTORE, which turns a
        # Manager-prepared maximized 2560x1417 game window back into the saved
        # 1024x768 window. Keep its foreground/input behavior but never restore
        # an already visible, non-minimized window.
        mouse_module = importlib.import_module("src.interaction.Mouse")
        runtime_module = importlib.import_module("src.core.base_mixin.runtime_mixin")

        def manager_active_and_send_mouse_delta(
            hwnd,
            dx=1,
            dy=1,
            activate=True,
            only_activate=False,
            delay=0.005,
            steps=5,
        ):
            if only_activate:
                activate = True
            if activate:
                try:
                    current_hwnd = mouse_module.win32gui.GetForegroundWindow()
                    if current_hwnd != hwnd:
                        if not mouse_module.win32gui.IsWindow(hwnd):
                            return None
                        if mouse_module.win32gui.IsIconic(hwnd):
                            mouse_module.win32gui.ShowWindow(hwnd, 9)
                            time.sleep(0.15)
                        elif not mouse_module.win32gui.IsWindowVisible(hwnd):
                            mouse_module.win32gui.ShowWindow(hwnd, 5)
                            time.sleep(0.15)
                        user32 = mouse_module.user32
                        kernel32 = interaction_module.ctypes.windll.kernel32
                        foreground_pid = interaction_module.ctypes.c_ulong(0)
                        user32.GetWindowThreadProcessId(
                            current_hwnd,
                            interaction_module.ctypes.byref(foreground_pid),
                        )
                        if foreground_pid.value == os.getpid():
                            # The visible formal GUI is allowed to yield focus
                            # only to its verified game. Minimizing our own
                            # window avoids Windows' foreground-lock denial;
                            # the GUI was still launched through the updater
                            # and remains available on the taskbar.
                            user32.ShowWindowAsync(current_hwnd, 6)
                            time.sleep(0.2)
                            current_hwnd = mouse_module.win32gui.GetForegroundWindow()
                        current_thread = kernel32.GetCurrentThreadId()
                        foreground_thread = user32.GetWindowThreadProcessId(current_hwnd, None)
                        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
                        attached_threads = []
                        try:
                            for thread_id in (foreground_thread, target_thread):
                                if (
                                    thread_id
                                    and thread_id != current_thread
                                    and thread_id not in attached_threads
                                    and user32.AttachThreadInput(current_thread, thread_id, True)
                                ):
                                    attached_threads.append(thread_id)
                            user32.AllowSetForegroundWindow(-1)
                            user32.BringWindowToTop(hwnd)
                            # Call user32 directly here: pywin32 raises when
                            # Windows reports a transient focus denial, which
                            # used to skip every remaining activation fallback.
                            user32.SetForegroundWindow(hwnd)
                            user32.SetFocus(hwnd)
                        finally:
                            for thread_id in reversed(attached_threads):
                                user32.AttachThreadInput(current_thread, thread_id, False)
                        if mouse_module.win32gui.GetForegroundWindow() != hwnd:
                            user32.SwitchToThisWindow(hwnd, True)
                            time.sleep(0.05)
                        if mouse_module.win32gui.GetForegroundWindow() != hwnd:
                            interaction_module.win32api.keybd_event(
                                interaction_module.win32con.VK_MENU, 0, 0, 0
                            )
                            try:
                                time.sleep(0.01)
                                mouse_module.win32gui.SetForegroundWindow(hwnd)
                            finally:
                                interaction_module.win32api.keybd_event(
                                    interaction_module.win32con.VK_MENU,
                                    0,
                                    interaction_module.win32con.KEYEVENTF_KEYUP,
                                    0,
                                )
                        time.sleep(delay)
                except Exception as error:
                    self.log_warning(f"YeYu Gamer run: window activation warning ({error})")
            if only_activate:
                return None
            count = max(1, steps)
            base_dx, remain_dx = divmod(dx, count)
            base_dy, remain_dy = divmod(dy, count)
            for _ in range(count):
                move_dx = base_dx + (1 if remain_dx > 0 else 0)
                move_dy = base_dy + (1 if remain_dy > 0 else 0)
                remain_dx = max(0, remain_dx - 1)
                remain_dy = max(0, remain_dy - 1)
                mouse_module.user32.mouse_event(
                    mouse_module.MOUSEEVENTF_MOVE, move_dx, move_dy, 0, 0
                )
                if delay > 0:
                    time.sleep(delay)
            return None

        interaction_module = importlib.import_module("src.interaction.EfInteraction")
        mouse_module.active_and_send_mouse_delta = manager_active_and_send_mouse_delta
        runtime_module.send_mouse_delta = manager_active_and_send_mouse_delta
        interaction_module.active_and_send_mouse_delta = manager_active_and_send_mouse_delta
        # A transient Win32 cursor move failure must not abort the formal
        # tool's otherwise valid PostMessage click. Retry briefly, then let the
        # fixed click path continue without moving/restoring the physical
        # cursor. This patch exists only inside the Manager-owned tool process.
        original_set_cursor_pos = interaction_module.win32api.SetCursorPos

        def manager_set_cursor_pos(position):
            last_error = None
            for _ in range(3):
                try:
                    return original_set_cursor_pos(position)
                except Exception as error:
                    last_error = error
                    time.sleep(0.05)
            if not getattr(self, "_yeyu_gamer_logged_cursor_fallback", False):
                self.log_warning(
                    f"YeYu Gamer run: SetCursorPos unavailable; continuing with window message click ({last_error})"
                )
                self._yeyu_gamer_logged_cursor_fallback = True
            return False

        interaction_module.win32api.SetCursorPos = manager_set_cursor_pos
        interaction_module.SetCursorPos = manager_set_cursor_pos
        # On this Windows/GameInput build, the protected Unity client installs
        # a hidden, system-owned GameInputServiceWindow as the reported
        # foreground hwnd while the game is the real input target.  The
        # upstream equality check treats that as focus loss and drops every
        # foreground-only key. Accept only that exact signed-system proxy,
        # with the verified game hwnd still visible, then retain pynput's
        # original physical-key path.
        original_wait_foreground = interaction_module.EfInteraction._wait_foreground
        psutil_module = importlib.import_module("psutil")
        win32process_module = importlib.import_module("win32process")

        def manager_gameinput_proxy_active(interaction, hwnd):
            foreground = interaction_module.win32gui.GetForegroundWindow()
            if foreground == hwnd:
                return True
            if not (
                interaction_module.win32gui.IsWindow(hwnd)
                and interaction_module.win32gui.IsWindowVisible(hwnd)
            ):
                return False
            try:
                if interaction_module.win32gui.GetClassName(foreground) != "GameInputServiceWindow":
                    return False
                _, process_id = win32process_module.GetWindowThreadProcessId(foreground)
                executable = Path(psutil_module.Process(process_id).exe()).resolve()
                expected = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "GameInputSvc.exe"
                accepted = executable == expected.resolve()
                if accepted and not getattr(self, "_yeyu_gamer_logged_gameinput_proxy", False):
                    self.log_info(
                        f"YeYu Gamer run: verified GameInput foreground proxy hwnd={foreground} game={hwnd}"
                    )
                    self._yeyu_gamer_logged_gameinput_proxy = True
                return accepted
            except Exception:
                return False

        def manager_minimize_own_windows(interaction):
            minimized = []

            def visit(window, _):
                try:
                    _, process_id = win32process_module.GetWindowThreadProcessId(window)
                    if (
                        process_id == os.getpid()
                        and window != interaction._game_hwnd()
                        and interaction_module.win32gui.IsWindowVisible(window)
                    ):
                        interaction_module.win32gui.ShowWindow(window, 6)
                        minimized.append(window)
                except Exception:
                    pass
                return True

            interaction_module.win32gui.EnumWindows(visit, None)
            if minimized and not getattr(self, "_yeyu_gamer_logged_tool_minimized", False):
                self.log_info(
                    f"YeYu Gamer run: minimized formal tool windows for game input {minimized}"
                )
                self._yeyu_gamer_logged_tool_minimized = True
            return minimized

        def manager_wait_foreground(interaction, hwnd, timeout=1.0):
            manager_minimize_own_windows(interaction)
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                if manager_gameinput_proxy_active(interaction, hwnd):
                    return True
                time.sleep(0.02)
            return manager_gameinput_proxy_active(interaction, hwnd)

        interaction_module.EfInteraction._wait_foreground = manager_wait_foreground
        # Keep the formal tool's native pynput path.  The foreground owner can
        # legitimately be the signed GameInput proxy, but that system-owned
        # window is not an input target and rejects PostMessage with access
        # denied.  Once the proxy is verified, the tool's ordinary physical
        # input is the supported route used by a direct GUI run.
        manager_minimize_own_windows(self.executor.interaction)
        # Endfield's current title screen says "点击任意位置继续", while the
        # upstream zh_CN matcher still only accepts "点击空白处继续".  Patch the
        # run-local language cache after the formal updater has finished so an
        # upstream working-tree replacement cannot discard the compatibility
        # fix.  Outside a Manager-owned run the upstream matcher is untouched.
        game_flow_lang = self.lang.game_flow_mixin
        game_flow_data = getattr(game_flow_lang, "_data", None)
        if isinstance(game_flow_data, dict):
            game_flow_data["k_8b2ca27a"] = {
                "pattern": re.compile(r"点击(?:空白处|任意位置)继续").pattern
            }

        # The current game HUD no longer matches the upstream ``fL.esc``
        # image shipped with v1.0.72.  "耐久度" is present in the lower half
        # of the live-world HUD and absent from the radial menu that Esc opens,
        # so use it as a Manager-run-only fallback instead of toggling between
        # those two screens until ensure_main times out.
        original_in_world = self.in_world

        def manager_in_world():
            if original_in_world():
                return True
            cancel = self.wait_ocr(
                match=re.compile(r"^取消$"),
                box=self.box_of_screen(0, 0, 1, 1),
                time_out=0,
                raise_if_not_found=False,
            )
            confirm = self.wait_ocr(
                match=re.compile(r"^确认$"),
                box=self.box_of_screen(0, 0, 1, 1),
                time_out=0,
                raise_if_not_found=False,
            )
            if cancel and confirm:
                self.log_info("YeYu Gamer run: reversible cancel/confirm overlay detected during main recovery; cancelling it")
                self.click(cancel[0])
                self._next_main_recovery_time = self.active_time() + self.once_sleep_time
                return False
            monthly_card = self.wait_ocr(
                match=re.compile(r"月卡剩余天数"),
                box=self.box_of_screen(0, 0, 1, 1),
                time_out=0,
                raise_if_not_found=False,
            )
            if monthly_card:
                self.log_info("YeYu Gamer run: current monthly-card page confirmed by OCR; using the formal tool's centre click")
                self.click(x=0.5, y=0.5, after_sleep=self.once_sleep_time)
                self._next_main_recovery_time = self.active_time() + self.once_sleep_time
                return False
            region_building = self.wait_ocr(
                match=re.compile(r"^地区建设等级$"),
                box=self.box_of_screen(0, 0, 0.7, 0.7),
                time_out=0,
                raise_if_not_found=False,
            )
            if region_building:
                # Delivery can leave the current client on the reversible
                # region-construction overlay.  Upstream waits almost two
                # minutes before trying ESC once, then aborts the remaining
                # selected dailies.  This exact OCR anchor was captured in a
                # Manager-owned run; close only that known menu and let
                # ensure_main verify the world again.
                self.log_info(
                    "YeYu Gamer run: region-construction overlay confirmed by OCR; returning to the world"
                )
                self.press_key("esc")
                self._next_main_recovery_time = self.active_time() + self.once_sleep_time
                return False
            hud = self.wait_ocr(
                match=re.compile(r"耐久度"),
                box=self.box.bottom,
                time_out=0,
                raise_if_not_found=False,
            )
            if not hud:
                # The current account's ordinary world HUD can omit the
                # durability label.  Its pinned quest tracker remains visible
                # in the upper-left world layer and disappears from the radial
                # menu and Action Manual overlays.  Accept only these observed
                # current-client tracker strings during a Manager-owned run.
                hud = self.wait_ocr(
                    match=re.compile(r"(?:扩装铳械塔|看.*发来的消息|请打开Baker)"),
                    box=self.box_of_screen(0, 0, 0.55, 0.55),
                    time_out=0,
                    raise_if_not_found=False,
                )
            if not hud:
                return False
            self._logged_in = True
            if not getattr(self, "_yeyu_gamer_logged_hud_fallback", False):
                self.log_info("YeYu Gamer run: current Endfield world HUD confirmed by OCR")
                self._yeyu_gamer_logged_hud_fallback = True
            return True

        self.in_world = manager_in_world
        # The formal GUI's --exit flag also terminates the game window.  This
        # Manager-owned run closes the tool through its Qt quit signal instead,
        # preserving the client on success, failure, and cancellation.
        self.exit_after_task = False
        self.active_and_send_mouse_delta(only_activate=True)
        repeat_times = self.config.get("重复测试的次数", 1) if self.debug else 1

        original_to_stage = self.daily_battle.to_stage

        def manager_to_stage():
            if original_to_stage():
                return True
            if self.daily_battle.battle_ctx.stage_name != "超距辉映管":
                return False
            # OK-EF 1.0.72's bundled template predates the current green
            # reflection-tube icon.  The current client exposes that configured
            # material under the explicit, OCR-stable IV row.  Use the row only
            # as a narrow fallback after the upstream audited lookup fails.
            self.log_info("YeYu Gamer run: current reflection-tube icon changed; using the observed 高阶培养IV row")
            self.daily_battle._open_index()
            self.daily_battle.wait_click_ocr(
                match=re.compile(r"^危境预演$"),
                box=self.box.left,
                time_out=6,
                log=True,
            )
            for _ in range(6):
                rows = self.daily_battle.wait_ocr(
                    match=re.compile(r"^协议空间[·・]高阶培养IV$"),
                    box=self.box_of_screen(0.30, 0.20, 0.76, 0.88),
                    time_out=2,
                    raise_if_not_found=False,
                    log=True,
                ) or []
                if rows:
                    row = rows[0]
                    top = max(0.0, row.y / self.height - 0.05)
                    bottom = min(1.0, row.y / self.height + 0.18)
                    go = self.daily_battle.wait_ocr(
                        match=re.compile(r"^前往$"),
                        box=self.box_of_screen(0.72, top, 0.92, bottom),
                        time_out=3,
                        raise_if_not_found=False,
                        log=True,
                    ) or []
                    if go:
                        self.click(go[0])
                        return self.daily_battle._switch_stage_reward_tier()
                self.scroll_relative(650 / 1920, 0.5, count=-2)
                self.wait_ui_stable(refresh_interval=0.5)
            return False

        self.daily_battle.to_stage = manager_to_stage

        def manager_claim_mail():
            self.info_set("current_task", "claim_delivery_rewards")
            self.log_info("YeYu Gamer run: opening the mail panel through the configured game hotkey")
            self.press_key("k")
            mail_anchor = self.wait_ocr(
                match=re.compile(r"(?:邮件|邮箱|收件箱|一键收取|收取|暂无邮件|已领取)"),
                box=self.box_of_screen(0, 0, 1, 1),
                time_out=10,
                raise_if_not_found=False,
            ) or []
            if not mail_anchor:
                # This protected build has rejected the K hotkey through all
                # three fixed Windows input paths. ESC is the one proven game
                # menu input, so open the native menu and resolve the mailbox
                # from visible text instead of clicking through the locked
                # world cursor.
                self.log_info("YeYu Gamer run: K was ignored; opening the native ESC menu")
                self.press_key("esc")
                mail_anchor = self.wait_ocr(
                    match=re.compile(r"(?:邮件|邮箱|收件箱|一键收取|收取|暂无邮件|已领取)"),
                    box=self.box_of_screen(0, 0, 1, 1),
                    time_out=10,
                    raise_if_not_found=False,
                ) or []
            if not mail_anchor:
                debug_frame = reporter.path.with_suffix(".mail.png")
                if self.frame is not None:
                    cv2.imwrite(str(debug_frame), self.frame)
                    self.log_info(f"YeYu Gamer run: saved rejected mail frame {debug_frame}")
                raise RuntimeError("mail entry was not visible after K and native ESC menu recovery")
            collect = [
                item for item in mail_anchor
                if re.search(r"(?:一键收取|收取)", str(getattr(item, "name", "")))
            ]
            if collect:
                self.click(collect[0])
                self.wait_pop_up()
                self.log_info("YeYu Gamer run: mail collect action was visible and executed")
            else:
                self.log_info("YeYu Gamer run: mail panel opened with no visible collect action")
            self.press_key("esc")
            self.ensure_main(time_out=30)
            return True

        mappings = [
            ("mail", "⭐收邮件", manager_claim_mail),
            ("spend-sanity", "⭐刷体力", self.daily_battle.battle),
            ("delivery-commission", "⭐转交运送委托", self.daily_routine.delivery_send_others),
            ("collect-credit", "⭐收信用", self.daily_routine.collect_credit),
            ("dijiang-harvest", "⭐帝江号收菜", self.daily_routine.boat_claim_rewards),
            ("claim-daily-reward", "⭐日常奖励", self.daily_routine.claim_daily_rewards),
        ]
        try:
            if reporter.selected_for_run("attach-world"):
                reporter.write("attach-world", "started")
                try:
                    self.ensure_main(time_out=600)
                except Exception as error:
                    reporter.write("attach-world", "failed", error)
                    raise
                reporter.write("attach-world", "completed")
            plan = [
                (title, reporter.wrap(operation, callback), lambda: True)
                for operation, title, callback in mappings
                if reporter.selected_for_run(operation)
            ]
            if not plan:
                return True
            # The Manager's selected operations are authoritative for this
            # run. DailyTaskRunner predicates above bypass stale local GUI
            # switches. The boat callback additionally needs its two concrete
            # sub-stages, so expose them only in memory and restore the user's
            # tool configuration before leaving the run.
            boat_config_present = "⭐帝江号收菜" in self.config
            boat_config_before = self.config.get("⭐帝江号收菜")
            if reporter.selected_for_run("dijiang-harvest"):
                self.config["⭐帝江号收菜"] = list(self.daily_routine.BOAT_STAGES)
            self.daily_runner = DailyTaskRunner(
                self,
                plan,
                shared_state_task_keys=self.BOAT_STATE_TASK_KEYS,
            )
            try:
                self.daily_runner.run(repeat_times=repeat_times)
            except Exception as error:
                current_title = getattr(self.daily_runner, "current_task_key", None)
                current_operation = next(
                    (operation for operation, title, _ in mappings if title == current_title),
                    None,
                )
                detail = f"{type(error).__name__}: {error}"
                if current_operation:
                    reporter.write(current_operation, "failed", detail)
                self.log_error(
                    "YeYu Gamer run: daily chain failed\n" + traceback.format_exc()
                )
                raise
            return True
        finally:
            if "boat_config_present" in locals():
                if boat_config_present:
                    self.config["⭐帝江号收菜"] = boat_config_before
                else:
                    self.config.pop("⭐帝江号收菜", None)
            self.run_daily_finally()

    def manager_finally(self):
        if _load_run_bridge() is not None:
            self.log_info("YeYu Gamer run: skip opening a local summary file")
            # Qt's normal shutdown tears down the bound game device and can
            # terminate Endfield even when exit_after_task is false. This is a
            # Manager-owned one-time child with fully flushed stage events, so
            # leave only the tool process without invoking that destructive
            # application cleanup path.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        return original_finally(self)

    DailyTask.run = manager_run
    DailyTask.run_daily_finally = manager_finally
    DailyTask._yeyu_gamer_bridge_v2 = True

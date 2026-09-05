"""One saved WW account per Manager run; the upstream task owns game input.

This resource is copied only by the packaged Adapter into its formal GUI's
working copy. Importing it does not import or start the game/tool runtime.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import sys
import time


LOGIN_NAMES = frozenset(("登录", "登入", "Log In", "Login", "LOG IN", "LOGIN"))
START_NAMES = frozenset(("进入游戏", "進入遊戲", "开始游戏", "開始遊戲"))
# A remembered-account page may also show passive policy/footer links and
# alternative password/SMS-login tabs. Only active input/consent requirements
# block that already-identified page; the absence of its account structure
# independently prevents any login click.
ACTIVE_INPUT = re.compile(r"(?:输入|輸入|填写|填寫|请输入).*(?:密码|密碼|手机|手機|验证码|驗證碼)|(?:发送|發送|获取|獲取|重发|重發).*验证码|(?:短信|安全|实名|實名|滑动|滑動).*验证|验证码(?:错误|已发送)|扫码(?:验证|登录)|掃碼(?:驗證|登入)|(?:enter|type).*(?:password|phone|verification code)|captcha|verify your|scan.*(?:qr|code)", re.I)
ACTIVE_CONSENT = re.compile(r"(?:请|請).*(?:同意|勾选|勾選)|阅读并同意|閱讀並同意|同意(?:并|並)(?:继续|繼續|登录|登入)|agree and (?:continue|log)|please.*(?:agree|accept)", re.I)
POLICY_LINK = re.compile(r"隐私|隱私|协议|協議|条款|條款|privacy|terms", re.I)
CONSENT_BUTTONS = frozenset(("同意", "接受", "Agree", "AGREE", "Accept", "ACCEPT"))
INPUT_LABELS = frozenset(("密码", "密碼", "验证码", "驗證碼", "短信验证码", "手機號", "手机号", "+86", "Password", "Verification Code"))


def active_login_gate(boxes):
    names = [str(getattr(box, "name", "")) for box in boxes]
    if any(name in INPUT_LABELS or ACTIVE_INPUT.search(name) or ACTIVE_CONSENT.search(name) for name in names):
        return True
    return any(name in CONSENT_BUTTONS for name in names) and any(POLICY_LINK.search(name) for name in names)


class AccountHumanRequired(RuntimeError):
    pass


def exact_saved_account(boxes, expected):
    """OCR normalization must never merge two remembered account identities."""
    matches = [box for box in boxes if getattr(box, "name", "") == expected]
    if len(matches) != 1:
        raise AccountHumanRequired("ww_saved_account_missing_or_ambiguous")
    return matches[0]


class AccountSwitch:
    def __init__(self, task, expected, report, upstream_logout, sleep):
        self.task = task
        self.expected = expected
        self.report = report
        self.upstream_logout = upstream_logout
        self.sleep = sleep
        self.ready = False
        self.login_clicked = False
        self.logout_clicked = False
        self.enter_clicked = False

    def observe(self):
        self.task.next_frame()
        boxes = self.task.ocr()
        if active_login_gate(boxes):
            raise AccountHumanRequired("ww_account_login_verification_required")
        return boxes

    def login_page(self, boxes):
        accounts = [box for box in boxes if "****" in str(getattr(box, "name", ""))]
        buttons = [box for box in boxes if getattr(box, "name", "") in LOGIN_NAMES]
        return accounts, buttons

    def wait_page(self, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            boxes = self.observe()
            accounts, buttons = self.login_page(boxes)
            if len(accounts) == 1 and len(buttons) == 1:
                return accounts, buttons
            self.sleep(0.25)
        raise AccountHumanRequired("ww_saved_account_login_screen_required")

    # Narrow facade for the upstream _switch_to_login implementation. Its sole
    # fixed logout action is allowed only after a fresh recognized menu anchor.
    def log_info(self, *_):
        pass

    def tr(self, text):
        return text

    def send_key(self, key, **kwargs):
        if key != "esc":
            raise AccountHumanRequired("ww_account_logout_contract_changed")
        self.observe()
        self.task.send_key(key)
        self.sleep(1.5)

    def wait_feature(self, name):
        if name != "esc_setting":
            raise AccountHumanRequired("ww_account_logout_contract_changed")
        self.observe()
        return self.task.wait_feature(name, time_out=10, raise_if_not_found=True)

    def click_relative(self, x, y, **kwargs):
        self.observe()
        if self.logout_clicked or (x, y) != (0.04, 0.96) or not self.task.find_one("esc_setting"):
            raise AccountHumanRequired("ww_account_logout_menu_unverified")
        self.logout_clicked = True
        self.task.click_relative(x, y)
        self.sleep(1)

    def click_confirm(self, timeout=10):
        self.observe()
        # Reuse the upstream visual confirm feature, without its permissive
        # missing-feature behavior and without a coordinate substitute.
        target = self.task.find_one(["confirm_btn_hcenter_vcenter", "confirm_btn_highlight_hcenter_vcenter"], threshold=0.6)
        if target is None:
            raise AccountHumanRequired("ww_account_logout_confirmation_unverified")
        self.task.click(target)
        self.sleep(1)

    def find_account_drop_down(self):
        return self.wait_page()[0][0]

    def run(self):
        if not self.expected or "****" not in self.expected:
            raise AccountHumanRequired("ww_saved_account_selector_required")
        self.report("account_capture_before", "ww_account_switch_observation")
        boxes = self.observe()
        accounts, buttons = self.login_page(boxes)
        if len(accounts) != 1 or len(buttons) != 1:
            if not self.task.in_team_and_world():
                raise AccountHumanRequired("ww_saved_account_login_screen_required")
            # Only this upstream method is reused, never its whole multi-account
            # run, automatic login, or login-button coordinate fallback.
            self.upstream_logout(self)
            accounts, buttons = self.wait_page()
        # Always inspect the opened list, including when the collapsed label
        # already matches: two remembered accounts can share one masked label.
        self.task.click(accounts[0])
        self.sleep(1)
        boxes = self.observe()
        if len(self.login_page(boxes)[0]) < 2:
            raise AccountHumanRequired("ww_saved_account_dropdown_not_open")
        exact_saved_account(boxes, self.expected)
        self.report("account_capture_list", "ww_saved_account_unique_list_match")
        boxes = self.observe()
        if len(self.login_page(boxes)[0]) < 2:
            raise AccountHumanRequired("ww_saved_account_dropdown_changed")
        selected = exact_saved_account(boxes, self.expected)
        self.task.click(selected)
        self.sleep(1)
        accounts, buttons = self.wait_page()
        exact_saved_account(accounts, self.expected)
        self.report("account_capture_selected", "ww_saved_account_exact_label_verified")
        # Re-read after the evidence handoff; the screenshot wait must not let a
        # changed account or a newly opened verification dialog inherit approval.
        accounts, buttons = self.login_page(self.observe())
        exact_saved_account(accounts, self.expected)
        if len(accounts) != 1 or len(buttons) != 1:
            raise AccountHumanRequired("ww_saved_account_login_button_unverified")
        self.login_clicked = True
        self.task.click(buttons[0])
        deadline = time.monotonic() + 90
        stable_world = 0
        while time.monotonic() < deadline:
            boxes = self.observe()
            if self.task.in_team_and_world():
                stable_world += 1
                if stable_world >= 2:
                    self.ready = True
                    self.task.logged_in = True
                    self.report("account_capture_after", "ww_saved_account_entered_world")
                    self.report("account_verified", "ww_saved_account_verified: exact saved label; stable game world; UID not verified")
                    return
            else:
                stable_world = 0
                starts = [box for box in boxes if getattr(box, "name", "") in START_NAMES]
                if not self.enter_clicked and len(starts) == 1 and not self.login_page(boxes)[0]:
                    allowed = self.task.find_boxes(starts, boundary="bottom_right")
                    if len(allowed) == 1:
                        self.enter_clicked = True
                        self.task.click(allowed[0])
            self.sleep(0.5)
        raise AccountHumanRequired("ww_saved_account_login_not_confirmed")


class Reporter:
    def __init__(self, bridge):
        self.path = Path(bridge["stageFile"])
        self.operation = bridge["accountOperation"]
        self.terminal = False

    def write(self, state, detail=""):
        if self.terminal:
            return
        record = {"operation": self.operation, "state": state, "detail": detail, "at": time.time()}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
        if state.startswith("account_capture_") or state == "account_verified":
            ack = Path(str(self.path) + ".capture.ack")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if ack.read_text(encoding="utf-8").startswith(state + ";"):
                        return
                except OSError:
                    pass
                time.sleep(0.05)
            raise AccountHumanRequired("ww_account_evidence_handoff_timeout")
        if state == "human_required":
            self.terminal = True


def load_request(main_path):
    path = Path(main_path).parent / "configs" / "YeYuGamerRun.json"
    try:
        bridge = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if "--task" in sys.argv or "-t" in sys.argv:
            raise RuntimeError("ww_account_formal_run_bridge_unreadable")
        return None
    if not bridge.get("accountSnapshot"):
        return None
    age = time.time() - float(bridge.get("createdAtUnix", 0))
    if bridge.get("schemaVersion") != 1 or bridge.get("active") is not True or not -300 <= age <= 3600:
        raise RuntimeError("ww_account_run_bridge_expired")
    snapshot = bridge["accountSnapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"label", "saved_account_label"}:
        raise RuntimeError("ww_account_run_bridge_invalid")
    if not isinstance(snapshot["saved_account_label"], str) or not snapshot["saved_account_label"]:
        raise RuntimeError("ww_saved_account_selector_required")
    if bridge.get("accountOperation") not in bridge.get("selectedOperations", []):
        raise RuntimeError("ww_account_operation_scope_invalid")
    if not isinstance(bridge.get("stageFile"), str) or not Path(bridge["stageFile"]).is_absolute():
        raise RuntimeError("ww_account_stage_scope_invalid")
    return bridge


def install(config, main_path):
    bridge = load_request(main_path)
    if bridge is None:
        return
    sys.argv[:] = [argument for argument in sys.argv if argument not in ("--exit", "-e")]
    from ok import BaseTask
    from src.task.BaseWWTask import BaseWWTask
    from src.task.DailyTask import DailyTask
    from src.task.MultiAccountDailyTask import MultiAccountDailyTask

    reporter = Reporter(bridge)
    state = {"switch": None, "verified": False}
    original_daily = DailyTask.run
    original_sleep = BaseWWTask.sleep

    # Preserve trigger task instances/configuration required by daily combat,
    # while suppressing background input before the selected identity is ready.
    for module_name, class_name in config.get("trigger_tasks", []):
        cls = getattr(importlib.import_module(module_name), class_name)
        original = cls.run
        def gated_trigger(self, *args, _original=original, _name=class_name, **kwargs):
            if not state["verified"] or _name == "AutoLoginTask" or reporter.terminal:
                return False
            return _original(self, *args, **kwargs)
        cls.run = gated_trigger

    def guarded_sleep(self, seconds):
        if not state["verified"]:
            return BaseTask.sleep(self, seconds)
        return original_sleep(self, seconds)

    def guarded_login(self):
        if state["verified"] and self.in_team_and_world():
            self.logged_in = True
            return True
        state["verified"] = False
        try:
            reporter.write("account_capture_blocked", "ww_account_login_state_changed")
        except AccountHumanRequired:
            pass
        reporter.write("human_required", "ww_account_login_state_changed")
        while True:
            time.sleep(0.25)

    def manager_daily(self):
        reporter.write("started", "ww_account_switch_started")
        try:
            switch = AccountSwitch(self, bridge["accountSnapshot"]["saved_account_label"], reporter.write,
                                   MultiAccountDailyTask._switch_to_login, lambda seconds: BaseTask.sleep(self, seconds))
            state["switch"] = switch
            switch.run()
            state["verified"] = True
            original_daily(self)
            # Exit only this tool process, after its stage writes have flushed.
            # Upstream --exit/Qt cleanup would also terminate the game client.
            os._exit(0)
        except AccountHumanRequired as error:
            state["verified"] = False
            try:
                reporter.write("account_capture_blocked", str(error))
            except AccountHumanRequired:
                pass
            reporter.write("human_required", str(error))
            # Do not run Qt's device teardown: it may kill the game. The Adapter
            # consumes the flushed human terminal and stops only its own tool.
            while True:
                time.sleep(0.25)
        except Exception as error:
            if not state["verified"]:
                state["verified"] = False
                try:
                    reporter.write("account_capture_blocked", "ww_account_switch_unverified")
                except AccountHumanRequired:
                    pass
                reporter.write("human_required", "ww_account_switch_unverified: " + type(error).__name__)
                while True:
                    time.sleep(0.25)
            os._exit(1)

    BaseWWTask.sleep = guarded_sleep
    BaseWWTask.wait_login = guarded_login
    DailyTask.run = manager_daily

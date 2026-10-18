"""Official OK-EF telemetry and explicitly scoped login handoffs."""
from __future__ import annotations
from datetime import datetime
from functools import wraps
import json
import os
import re
import uuid
from OfficialStepCapture import install_recognition_observers, recognition_scope

_METHOD_OPERATIONS = {"claim_mail": "mail", "battle": "spend-sanity", "delivery_send_others": "delivery-commission", "collect_credit": "collect-credit", "boat_claim_rewards": "dijiang-harvest", "claim_daily_rewards": "claim-daily-reward"}
_FEATURE_OPERATIONS = {
    ("MailTask", "run_mail", "⭐收邮件"): "mail",
    ("DailyBattleTask", "run_battle", "⭐刷体力"): "spend-sanity",
    ("DeliverySendTask", "delivery_send_others", "⭐转交运送委托"): "delivery-commission",
    ("CreditCollectTask", "run_credit_collect", "⭐收信用"): "collect-credit",
    ("BoatHarvestTask", "boat_claim_rewards", "⭐帝江号收菜"): "dijiang-harvest",
    ("DailyRewardTask", "claim_daily_rewards", "⭐日常奖励"): "claim-daily-reward",
}


def _plan_operation(host, label, method):
    operation = _METHOD_OPERATIONS.get(getattr(method, "__name__", ""))
    if operation:
        return operation
    # v1.1.8 composes registered subtasks through DailyFeature.run. Attribute
    # the actual official wrapper rather than treating every method named run
    # as a daily step. Leave its predicate and account context untouched.
    feature = getattr(method, "__self__", None)
    feature_type = type(feature)
    if (feature_type.__module__ != "src.tasks.daily.daily_feature"
            or feature_type.__name__ != "DailyFeature"
            or getattr(method, "__func__", None) is not feature_type.run
            or feature.host is not host or feature.switch_key != label):
        return None
    task_class = feature.task_class
    if task_class.__module__ != "src.tasks.onetime." + task_class.__name__:
        return None
    return _FEATURE_OPERATIONS.get((task_class.__name__, feature.run_method, label))

def _install_upstream_logging():
    try:
        from upstream_logging import install
        install(os.environ.get("YEYU_GAMER_UPSTREAM_LOG_FILE", ""))
    except Exception: pass

def _context():
    path = os.environ.get("YEYU_GAMER_STAGE_FILE")
    try:
        selected = json.loads(os.environ.get("YEYU_GAMER_SELECTED_OPERATIONS", ""))
        if isinstance(selected, list) and all(isinstance(item, str) for item in selected):
            return path, set(selected)
    except Exception:
        pass
    return path, set()

def _emit(path, operation, state, detail):
    if not path: return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": operation, "state": state, "detail": detail, "at": datetime.now().astimezone().isoformat()}, ensure_ascii=False) + "\n")
    except Exception: pass

def _observe(operation, original, false_is_failure=True, task=None):
    @wraps(original)
    def wrapped(*args, **kwargs):
        _install_upstream_logging(); path, selected = _context(); active = operation in selected
        owner = task if task is not None else args[0]
        reward_probe_history = operation in {"mail", "dijiang-harvest", "claim-daily-reward"}
        credit_probe_history = operation == "collect-credit"
        if active:
            _emit(path, operation, "started", "official DailyTask method entered: " + original.__name__)
        try:
            with recognition_scope(owner, path if active else None, operation, _emit, "endfield",
                                   max_images=8 if operation == "attach-world" else (4 if reward_probe_history or credit_probe_history else 2),
                                   first_distinct=5 if operation == "attach-world" else 0,
                                   template_sample_interval_seconds=30 if operation == "attach-world" else 0,
                                   retain_other_templates=reward_probe_history,
                                   template_history_names=("reward_ok",) if reward_probe_history else
                                   (("friend_page",) if credit_probe_history else
                                    (("esc", "esc_2k", "esc_4k") if operation == "attach-world" else ()))):
                result = original(*args, **kwargs)
        except Exception as error:
            if active:
                _emit(path, operation, "failed", "official %s raised %s: %s" % (original.__name__, type(error).__name__, error))
            raise
        if active:
            state = "failed" if false_is_failure and result is False else "completed"
            detail = "official DailyTask method %s returned %r" % (original.__name__, result)
            if not false_is_failure:
                detail = "official prerequisite method %s returned %r" % (original.__name__, result)
            _emit(path, operation, state, detail)
        return result
    return wrapped

def _install_methods(DailyTask):
    original_build = DailyTask.build_task_plan
    @wraps(original_build)
    def observed_build(self, *args, **kwargs):
        plan = original_build(self, *args, **kwargs); observed = []
        for entry in plan:
            label, method, *rest = entry
            try:
                operation = _plan_operation(self, label, method)
            except Exception:
                # A telemetry attribution failure must leave this exact
                # official plan entry callable and its predicate untouched.
                operation = None
            observed.append((label, _observe(operation, method, task=self), *rest) if operation else entry)
        return observed
    DailyTask.build_task_plan = observed_build
    # Activating a window does not establish that the official game-world
    # precondition passed. Observe its first actual ensure_main invocation;
    # subsequent calls remain the official subtask's own readiness checks.
    original_main = DailyTask.ensure_main
    observed_main = _observe("attach-world", original_main, false_is_failure=False)
    @wraps(original_main)
    def first_ensure_main(self, *args, **kwargs):
        if getattr(self, "_yeyu_world_observed", False):
            return original_main(self, *args, **kwargs)
        self._yeyu_world_observed = True
        return observed_main(self, *args, **kwargs)
    DailyTask.ensure_main = first_ensure_main


def _login_log_info(task, message):
    try:
        task.log_info(message)
    except Exception:
        # Diagnostics must not block a handoff or replace an official error.
        pass


def _login_event(task, status, code, message, kind="idle_logout"):
    detail = {
        "code": code, "message": message,
        "metrics": {
            "integrationStatus": status,
            "trigger": "Official wait_login: exact idle-logout notice and one confirmation button" if kind == "idle_logout" else "Official wait_login returned without a click; exact title-page continue prompt on current official frame",
            "waitingFor": "Official ensure_main to confirm game world" if status == "pending" else "",
            "initiator": "EndfieldAdapter", "step": "wait_login",
        },
    }
    _login_log_info(task, "YeYuGamer login handoff: " + json.dumps(detail, ensure_ascii=False))
    _emit(_context()[0], "endfield-" + kind.replace("_", "-") + "-confirm", "integration", json.dumps(detail, ensure_ascii=False))


def _login_capture(task, phase, kind="idle_logout"):
    path = _context()[0]
    if not path:
        return
    try:
        from ok import og
        frame = task.executor.nullable_frame()
        if frame is None:
            raise ValueError("official frame buffer unavailable")
        captured_at = datetime.now().astimezone().isoformat()
        saved = og.ok.screenshot.generate_screen_shot(
            frame.copy(), {}, os.path.dirname(path), "endfield-login-" + uuid.uuid4().hex, False, None)
        if not saved or not os.path.isfile(saved):
            raise OSError("official screenshot writer produced no file")
        _emit(path, "endfield-" + kind.replace("_", "-") + "-confirm", "integration_artifact",
              "file=%s; kind=game-ui-login-%s-raw; capturedAt=%s" %
              (os.path.basename(saved), phase, captured_at))
    except Exception as error:
        detail = {"code": "login_capture_unavailable", "message": str(error),
                  "metrics": {"integrationStatus": "failed", "trigger": kind + " handoff: " + phase,
                              "waitingFor": ""}}
        _emit(path, "endfield-" + kind.replace("_", "-") + "-screenshot-" + phase, "integration",
              json.dumps(detail, ensure_ascii=False))
        _login_log_info(task, "YeYuGamer login screenshot failed: " + str(error))


def _observe_credential_gate(task, frame):
    """Read one official frame; report the login gate without entering data."""
    path, selected = _context()
    if not path or 'attach-world' not in selected:
        return False
    if getattr(task, '_yeyu_credential_gate_observed', False):
        return True
    try:
        # Sealed native-frame replay: the phone icon joins the placeholder as
        # 品, and PP-OCR reads the logo's 鹰 as 应. Keep these two observed
        # confusions bounded; all three independent fields must remain unique.
        phone = task.ocr(frame=frame, match=re.compile(r'^(?:品)?请输入手机(?:号|号码)$'))
        code = task.ocr(frame=frame, match=re.compile(r'^请输入验证码$'))
        vendor = task.ocr(frame=frame, match=re.compile(r'^(?:[鹰应]\s*角\s*网\s*络|HYPERGRYPH)$', re.I))
        if len(phone) != 1 or len(code) != 1 or len(vendor) != 1:
            return False
    except Exception:
        # A failed diagnostic cannot alter the official task or its result.
        return False
    task._yeyu_credential_gate_observed = True
    _login_capture(task, 'before', 'credential')
    _emit(path, 'attach-world', 'human_required', json.dumps({
        'code': 'endfield_credentials_required',
        'message': 'Official Hypergryph login requires phone number and verification code; user login required.'}, ensure_ascii=False))
    return True


def _install_login_handoff(task_type, kind="idle_logout"):
    """Own input only within official wait_login; never poll/click from a monitor.

    The pinned official login handler misses these observed screens. Remove a
    handoff once upstream covers it. Each handoff clicks at most once per task;
    only the enclosing official ensure_main can confirm the handoff.
    """
    original_login = task_type.wait_login
    original_main = task_type.ensure_main
    state_attribute = "_yeyu_" + kind

    def event(task, status, suffix, message):
        _login_event(task, status, kind + "_" + suffix, message, kind)

    def capture(task, phase):
        _login_capture(task, phase, kind)

    @wraps(original_login)
    def wait_login(self, *args, **kwargs):
        result = original_login(self, *args, **kwargs)
        if result or getattr(self, state_attribute, None):
            return result
        frame = self.executor.nullable_frame()
        if frame is None:
            return result
        if _observe_credential_gate(self, frame):
            return result
        if kind == "idle_logout":
            notice = self.ocr(frame=frame, match=re.compile(r"^长时间没有操作自动登出$"))
            if len(notice) != 1:
                return result
            buttons = self.ocr(frame=frame, match=re.compile(r"^确认$"))
            if len(buttons) != 1 or buttons[0].y <= notice[0].y + notice[0].height:
                return result
        else:
            if getattr(self, "_logged_in", False):
                return result
            buttons = self.ocr(frame=frame, match=re.compile(r"^点击任意位置继续$"))
            if len(buttons) != 1:
                return result
        setattr(self, state_attribute, "pending")
        capture(self, "before")
        event(self, "pending", "confirmation_requested",
              "Recognized " + kind + "; requesting official click on its unique confirmation target")
        try:
            self.click(buttons[0])
        except Exception as error:
            setattr(self, state_attribute, "failed")
            event(self, "failed", "confirmation_failed", str(error))
            capture(self, "after")
            raise
        event(self, "pending", "confirmation_dispatched",
                     "Official click returned; waiting for official ensure_main, login not yet confirmed")
        return result

    @wraps(original_main)
    def ensure_main(self, *args, **kwargs):
        try:
            result = original_main(self, *args, **kwargs)
        except Exception as error:
            if getattr(self, state_attribute, None) == "pending":
                setattr(self, state_attribute, "failed")
                event(self, "failed", "world_unconfirmed",
                             "Official ensure_main raised: " + str(error))
                capture(self, "after")
            raise
        if getattr(self, state_attribute, None) == "pending":
            setattr(self, state_attribute, "completed")
            event(self, "completed", "world_confirmed",
                         "Official ensure_main returned after confirmation; handing back to official daily flow")
            capture(self, "after")
        return result

    task_type.wait_login = wait_login
    task_type.ensure_main = ensure_main


def _configure_resolution(config):
    # The pinned config declares its minimum but leaves resize_to commented.
    # StartController and HwndWindow own the operation and the user's existing
    # Auto Resize Game Window switch. Never override an upstream resize policy.
    supported = config["supported_resolution"]
    if "resize_to" in supported:
        return
    supported["resize_to"] = [tuple(supported["min_size"])]
    _emit(_context()[0], "endfield-resolution-config", "integration", json.dumps({
        "code": "official_resolution_configured",
        "message": "Supplied official resize_to from the configured minimum resolution; no resize result claimed",
        "metrics": {
            "integrationStatus": "configured", "resizeTo": supported["resize_to"],
            "trigger": "Bound official config declares min_size but omits resize_to",
            "waitingFor": "Official StartController resolution check; existing Auto Resize Game Window switch applies",
        },
    }, ensure_ascii=False))


def install_yeyu_gamer_bridge():
    from src.tasks.onetime.DailyTask import DailyTask
    from ok.core.events import communicate
    from OfficialTaskExit import install_failure_exit
    from src.config import config
    if os.environ.get("YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY") == "1":
        from src.tasks.onetime.RewardStatusInspectionTask import RewardStatusInspectionTask
        from EndfieldRewardStatusObserver import install
        if getattr(RewardStatusInspectionTask, "_yeyu_inspection_bridge_v1", False): return
        install_recognition_observers()
        _configure_resolution(config)
        _install_login_handoff(RewardStatusInspectionTask)
        _install_login_handoff(RewardStatusInspectionTask, "title_login")
        RewardStatusInspectionTask.ensure_main = _observe(
            "attach-world", RewardStatusInspectionTask.ensure_main, false_is_failure=False)
        install(RewardStatusInspectionTask, _emit)
        install_failure_exit(RewardStatusInspectionTask, communicate, "EndfieldInspectionAdapter")
        RewardStatusInspectionTask._yeyu_inspection_bridge_v1 = True
        return
    if getattr(DailyTask, "_yeyu_gamer_observer_v7", False): return
    install_recognition_observers()
    _configure_resolution(config)
    _install_methods(DailyTask)
    _install_login_handoff(DailyTask)
    _install_login_handoff(DailyTask, "title_login")
    install_failure_exit(DailyTask, communicate, "EndfieldAdapter",
                         lambda state, detail: _emit(_context()[0], "upstream-daily-task", state, detail))
    DailyTask._yeyu_gamer_observer_v7 = True

"""Run-scoped YeYu Gamer bridge for the formal ok-nte DailyTask.

The formal updater may replace the working tree before every run.  The Adapter
therefore imports this module from its immutable promoted package only after the
visible updater has finished.  Direct ok-nte launches remain untouched.
"""

from __future__ import annotations

from datetime import datetime
import json
import os


STAGE_FILE_ENV = "YEYU_GAMER_STAGE_FILE"
SELECTED_ENV = "YEYU_GAMER_SELECTED_OPERATIONS"
PROFILE_ENV = "YEYU_GAMER_NTE_PROFILE"
_SUPPORTED = {
    "attach-world",
    "claim-mail",
    "inspect-daily-progress",
    "spend-urban-vitality",
    "claim-daily-reward",
    "claim-period-reward",
}


class StageReporter:
    def __init__(self) -> None:
        self.path = os.environ.get(STAGE_FILE_ENV, "")
        selected = json.loads(os.environ.get(SELECTED_ENV, "[]"))
        if (
            not self.path
            or not isinstance(selected, list)
            or not selected
            or not all(isinstance(item, str) for item in selected)
            or not set(selected).issubset(_SUPPORTED)
        ):
            raise ValueError("nte_manager_stage_contract_invalid")
        self.selected = set(selected)

    def emit(self, operation: str, state: str, detail: str) -> None:
        if operation not in self.selected:
            return
        record = {
            "operation": operation,
            "state": state,
            "detail": str(detail or f"{operation} {state}")[:1000],
            "at": datetime.now().astimezone().isoformat(),
        }
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def run(self, operation: str, callback, completed_detail: str):
        if operation not in self.selected:
            return None
        self.emit(operation, "started", f"{operation} started")
        try:
            result = callback()
        except Exception as error:
            self.emit(operation, "failed", f"{type(error).__name__}: {error}")
            raise
        if result is False:
            self.emit(operation, "failed", "upstream task returned False")
        else:
            self.emit(operation, "completed", completed_detail)
        return result


class DailyProfile:
    _FIELDS = {
        "anomalyTaskType",
        "expRewardTarget",
        "materialIndex",
        "staminaTarget",
        "autoCycleSubTask",
        "coffeeMode",
    }

    def __init__(self) -> None:
        document = json.loads(os.environ.get(PROFILE_ENV, "{}"))
        if not isinstance(document, dict) or set(document) != self._FIELDS:
            raise ValueError("nte_daily_profile_invalid")
        self.task_type = document["anomalyTaskType"]
        self.exp_target = document["expRewardTarget"]
        self.material_index = document["materialIndex"]
        self.stamina_target = document["staminaTarget"]
        if (
            self.task_type not in {"经验与甲硬币", "异能升级材料", "弧盘突破材料", "空幕"}
            or self.exp_target not in {"角色经验", "弧盘经验", "甲硬币"}
            or isinstance(self.material_index, bool)
            or not isinstance(self.material_index, int)
            or not 1 <= self.material_index <= 6
            or isinstance(self.stamina_target, bool)
            or not isinstance(self.stamina_target, int)
            or not 40 <= self.stamina_target <= 360
            or self.stamina_target % 40 != 0
            or document["autoCycleSubTask"] is not False
            or document["coffeeMode"] != "不执行"
        ):
            raise ValueError("nte_daily_profile_invalid")

    def apply(self, task) -> dict:
        from src.tasks.AnomalyTask import AnomalyTask

        original = task.config
        config = dict(original)
        config[task.CONF_TASK] = AnomalyTask.TASK_NAME
        config[task.CONF_CLAIM_MAIL] = True
        config[task.CONF_COMPLETE_DAILY] = True
        config[task.CONF_CLAIM_ACTIVITY] = True
        config[task.CONF_CLAIM_BP] = True
        config[task.CONF_COFFEE_TASK] = task.TASK_NONE
        config[task.CONF_CINEMA_DATE] = False
        config[task.CONF_FOUNTAIN_SIGN] = task.TASK_NONE
        config[task.CONF_FURNITURE] = False
        config[task.CONF_GIFT] = False
        config[task.DAILY_STAMINA_TARGET] = self.stamina_target
        config[AnomalyTask.CONF_TASK_TYPE] = self.task_type
        config[AnomalyTask.CONF_EXP_TARGET] = self.exp_target
        material_key = {
            AnomalyTask.TASK_ABILITY: AnomalyTask.CONF_ABILITY_ID,
            AnomalyTask.TASK_ARC: AnomalyTask.CONF_ARC_ID,
            AnomalyTask.TASK_CONSOLE: AnomalyTask.CONF_CONSOLE_ID,
        }.get(self.task_type)
        if material_key:
            config[material_key] = self.material_index
        task.config = config
        return original


def install() -> None:
    """Replace only DailyTask.do_run for this environment-bound process."""

    from src.tasks.DailyTask import DailyTask

    if getattr(DailyTask, "_yeyu_gamer_bridge_v1", False):
        return
    required = (
        "ensure_main",
        "claim_mail",
        "check_activity",
        "complete_daily_activities",
        "claim_activity_rewards",
        "claim_battle_pass_rewards",
        "_print_result",
    )
    if any(not callable(getattr(DailyTask, name, None)) for name in required):
        raise RuntimeError("nte_daily_task_identity_changed")

    original_do_run = DailyTask.do_run

    def manager_do_run(self):
        reporter = StageReporter()
        profile = DailyProfile()
        original_config = profile.apply(self)
        try:
            reporter.run("attach-world", self.ensure_main, "ok-nte reached the actionable game home")
            self.log_info("YeYu Gamer selected formal DailyTask started")
            reporter.run("claim-mail", self.claim_mail, "mail stage completed")

            def inspect_daily_progress():
                self.check_activity()
                return True

            reporter.run(
                "inspect-daily-progress",
                inspect_daily_progress,
                "daily activity and vitality panel was inspected",
            )
            reporter.run(
                "spend-urban-vitality",
                self.complete_daily_activities,
                "configured ordinary vitality route completed or was already satisfied",
            )
            reporter.run(
                "claim-daily-reward",
                self.claim_activity_rewards,
                "daily activity reward claim stage completed",
            )
            reporter.run(
                "claim-period-reward",
                self.claim_battle_pass_rewards,
                "period reward claim stage completed",
            )
            self.ensure_main()
            self._print_result()
            self.log_info("YeYu Gamer selected formal DailyTask ended", notify=True)
        finally:
            self.config = original_config

    DailyTask._yeyu_gamer_original_do_run = original_do_run
    DailyTask.do_run = manager_do_run
    DailyTask._yeyu_gamer_bridge_v1 = True


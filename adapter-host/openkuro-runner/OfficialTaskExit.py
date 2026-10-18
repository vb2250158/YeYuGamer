"""Requested exit after the official OK task executor has handled failure."""
from functools import wraps
import os


def install_failure_exit(task_type, communicate, initiator, observe=None):
    """Complete the requested exit lifecycle after upstream handles failure.

    TaskExecutor logs and captures the exception before emitting task(None).
    Never quit from the run wrapper, while that official cleanup is pending.
    """
    from ok.task.exceptions import FinishedException, TaskDisabledException
    original = task_type.run
    pending = [None]

    def observed(state, detail):
        if observe is not None:
            try:
                observe(state, detail)
            except Exception:
                # Telemetry must not replace an official result or exception.
                pass

    def log_lifecycle(task, message):
        try:
            task.log_info(message)
        except Exception:
            # A diagnostic write cannot drop the pending quit or change emit's result.
            pass

    @wraps(original)
    def run(self, *args, **kwargs):
        observed("started", "Official DailyTask.run entered")
        try:
            result = original(self, *args, **kwargs)
        except (FinishedException, TaskDisabledException):
            raise
        except Exception as error:
            observed("failed", f"Official DailyTask.run raised {type(error).__name__}: {error}")
            if getattr(self, "exit_after_task", False) or self.config.get("Exit After Task"):
                pending[0] = (self, type(error).__name__)
            raise
        observed("returned", "Official DailyTask.run returned; child method results remain authoritative")
        return result

    def task_changed(current):
        entry = pending[0]
        if current is not None or entry is None:
            return
        task, error_type = entry
        if task.running or getattr(getattr(task, "executor", None), "current_task", None) is not None:
            return
        pending[0] = None
        log_lifecycle(task, f"YeYuGamer lifecycle: initiator={initiator}; "
                      "trigger=official_failed_task_idle; action=official_quit_signal; "
                      f"targetPid={os.getpid()}; step={type(task).__name__}.run; "
                      f"exceptionType={error_type}; result=requested")
        try:
            communicate.quit.emit()
        except Exception as error:
            log_lifecycle(task, f"YeYuGamer lifecycle: initiator={initiator}; targetPid={os.getpid()}; "
                          f"action=official_quit_signal; result=dispatch_failed; error={type(error).__name__}")
        else:
            log_lifecycle(task, f"YeYuGamer lifecycle: initiator={initiator}; targetPid={os.getpid()}; "
                          "action=official_quit_signal; result=dispatched")

    original_emit = communicate.task.emit
    @wraps(original_emit)
    def task_emitted(*args, **kwargs):
        result = original_emit(*args, **kwargs)
        # Observe the actual executor idle emission. Qt subscribers run later,
        # potentially after a trigger task has acquired current_task again.
        if len(args) == 1 and not kwargs:
            task_changed(args[0])
        return result
    communicate.task.emit = task_emitted
    task_type.run = run

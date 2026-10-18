"""Exercise bound ok-script failure cleanup, event dispatch and actual App.quit."""
from __future__ import annotations
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace


def verify_failure_exit(install_failure_exit):
    # Use the actual official event bus.  A method exception alone cannot
    # request quit: TaskExecutor must finish handling it and publish idle.
    from ok.core.events import EventBus
    for headless, exit_flag, config_exit, fails in [
        (True, True, False, True), (True, False, True, True),
        (True, False, False, True), (False, True, False, True),
        (True, True, False, False), (False, False, False, True),
        (False, False, True, True), (False, True, False, False),
    ]:
        bus = EventBus()
        quits, logs = [], []
        bus.quit.connect(lambda: quits.append("official quit requested"))
        error = RuntimeError("official task failure")
        class LifecycleTask:
            def run(self):
                if fails: raise error
                return "official result"
        install_failure_exit(LifecycleTask, bus)
        task = LifecycleTask()
        task._app = SimpleNamespace(headless=headless)
        task.exit_after_task = exit_flag
        task.config = {"Exit After Task": config_exit}
        task.running = True
        task.log_info = logs.append
        try:
            assert task.run() == "official result"
            assert not fails
        except RuntimeError as caught:
            assert caught is error
        bus.task.emit(None)
        assert not quits, "cannot quit before official exception handling finishes"
        task.running = False
        bus.task.emit(task)
        assert not quits
        bus.task.emit(None)
        bus.task.emit(None)
        expected = int((exit_flag or config_exit) and fails)
        assert len(quits) == expected and len(logs) == expected * 2

    from ok.task.exceptions import FinishedException, TaskDisabledException
    for exception_type in (FinishedException, TaskDisabledException):
        bus = EventBus()
        quits = []
        bus.quit.connect(lambda: quits.append(True))
        class ControlTask:
            def run(self): raise exception_type()
        install_failure_exit(ControlTask, bus)
        task = ControlTask()
        task._app = SimpleNamespace(headless=True)
        task.exit_after_task = True
        task.running = False
        try: task.run()
        except exception_type: pass
        bus.task.emit(None)
        assert not quits, "official cancellation/finish control flow is not failure"

    # Run the unmodified official execute() exception path and HeadlessApp.quit.
    # Replace device/window collaborators only; no synthetic idle notification.
    import threading
    import time
    from unittest.mock import patch
    from ok import HeadlessApp
    executor_module = importlib.import_module("ok.task.TaskExecutor")
    trace = []
    bus = EventBus()
    exit_event = threading.Event()
    app = SimpleNamespace(
        headless=True, tr=lambda value: value, notification_manager=None,
        exit_event=exit_event, sync_overlay_source=lambda *args: None,
        show_notification=lambda *args: None,
        _on_blur_patches=lambda *args: None,
        _on_clear_blur_patches=lambda *args: None,
        _close_overlay=lambda **kwargs: trace.append("official_app_quit"),
    )
    class FailedDaily:
        def run(self): raise RuntimeError("official failure fixture")
    install_failure_exit(FailedDaily, bus)
    task = FailedDaily()
    task.name = "DailyTask"
    task._app = app
    task.exit_after_task = True
    task.config = {}
    task.disable = lambda: trace.append("disabled")
    task.info_set = lambda *args: trace.append("official_error_info")
    task.log_info = lambda message: trace.append("quit_dispatched" if "result=dispatched" in message else "quit_requested")
    bus.screenshot.connect(lambda *args: trace.append("official_screenshot"))
    bus.quit.connect(lambda: HeadlessApp.quit(app))
    selections = iter([(task, False, False)])
    executor = SimpleNamespace(
        exit_event=exit_event, paused=False, _last_frame_time=time.time(),
        _frame=object(), frame=object(), _get_wake_version=lambda: 0,
        next_task=lambda: next(selections), reset_scene=lambda: None,
        destroy=lambda: trace.append("official_executor_destroy"),
    )
    prepare = getattr(executor_module.TaskExecutor, "_prepare_task_for_run", None)
    if prepare is not None:
        executor.interaction = SimpleNamespace(on_run=lambda: trace.append("official_input_prepare"))
        executor._prepare_task_for_run = prepare.__get__(executor, type(executor))
    logger = SimpleNamespace(info=lambda *args: None, debug=lambda *args: None,
                             error=lambda *args: trace.append("official_error_log"))
    with patch.object(executor_module, "communicate", bus), patch.object(
        executor_module, "prevent_sleeping", lambda *args: None
    ), patch.object(executor_module, "logger", logger):
        executor_module.TaskExecutor.execute(executor)
    assert exit_event.is_set() and executor.current_task is None
    expected_trace = (["official_input_prepare"] if prepare is not None else []) + ["disabled", "official_error_info", "official_error_log",
                     "official_screenshot", "quit_requested", "official_app_quit",
                     "quit_dispatched", "official_executor_destroy"]
    assert trace == expected_trace, trace

    # Exercise the actual Qt dispatcher and App.quit, rather than making a
    # GUI-looking object use the synchronous headless event bus.
    from PySide6.QtCore import QCoreApplication, QTimer
    from ok import App
    from ok.core.events import set_event_dispatcher
    from ok.ui.qt.events import install_qt_event_dispatcher
    qt_app = QCoreApplication.instance() or QCoreApplication([])
    gui_bus = EventBus()
    gui_trace = []
    gui_exit = threading.Event()
    main_thread = threading.get_ident()
    class GuiFailure:
        def run(self): raise RuntimeError("Qt lifecycle fixture")
    install_failure_exit(GuiFailure, gui_bus)
    gui_task = GuiFailure()
    gui_task.exit_after_task = True
    gui_task.config = {}
    gui_task.running = True
    gui_task.executor = SimpleNamespace(current_task=None)
    gui_task.log_info = lambda message: gui_trace.append((
        "dispatched" if "result=dispatched" in message else "requested", threading.get_ident()))
    gui_facade = SimpleNamespace(
        exit_event=gui_exit, app=qt_app,
        _on_blur_patches=lambda *args: None,
        _on_clear_blur_patches=lambda *args: None,
        _close_overlay=lambda **kwargs: gui_trace.append(("overlay", threading.get_ident())),
        _close_device_manager=lambda: gui_trace.append(("device", threading.get_ident())),
    )
    gui_task._app = gui_facade
    gui_bus.quit.connect(lambda: App.quit(gui_facade))
    def worker_failure():
        try: gui_task.run()
        except RuntimeError: pass
        gui_task.running = False
        gui_bus.task.emit(None)
        gui_task.executor.current_task = object()  # next trigger before Qt delivers subscribers
    install_qt_event_dispatcher()
    worker = threading.Thread(target=worker_failure)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(qt_app.quit)
    try:
        timeout.start(5000)
        worker.start()
        qt_app.exec()
        worker.join(timeout=2)
        assert not worker.is_alive() and gui_exit.is_set(), "official Qt quit was not delivered"
        assert [name for name, _ in gui_trace] == ["requested", "dispatched", "overlay", "device"], gui_trace
        assert all(thread_id == worker.ident for _, thread_id in gui_trace[:2]), gui_trace
        assert all(thread_id == main_thread for _, thread_id in gui_trace[2:]), gui_trace
    finally:
        timeout.stop()
        set_event_dispatcher(None)



if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openkuro-runner"))
    from OfficialTaskExit import install_failure_exit
    verify_failure_exit(lambda task, bus: install_failure_exit(task, bus, "EndfieldAdapter"))
    from ok.core.events import EventBus
    from ok.task.exceptions import FinishedException, TaskDisabledException
    for failure in (None, RuntimeError("world prerequisite failed"), FinishedException(), TaskDisabledException()):
        events = []
        class RootTask:
            config = {}
            exit_after_task = False
            def run(self):
                if failure is not None:
                    raise failure
                return False
        install_failure_exit(RootTask, EventBus(), "EndfieldAdapter", lambda *event: events.append(event))
        try:
            assert RootTask().run() is False
        except Exception as caught:
            assert caught is failure
        expected = ["started", "returned"] if failure is None else ["started", "failed"] if isinstance(failure, RuntimeError) else ["started"]
        assert [event[0] for event in events] == expected, events
        if isinstance(failure, RuntimeError):
            assert "world prerequisite failed" in events[-1][1]
    print("official_task_exit: passed; actual_TaskExecutor=True; actual_Qt_dispatcher=True; game_started=False")

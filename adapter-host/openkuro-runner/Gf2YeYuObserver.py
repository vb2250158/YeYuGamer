"""Official OK-GF2 step telemetry and requested failure exit."""
from __future__ import annotations
from datetime import datetime
from functools import wraps
from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import uuid
from OfficialStepCapture import install_recognition_observers, recognition_scope


_METHOD_OPERATIONS = {
    "mail": "mail",
    "activities": "activity-supply",
    "star_wish": "star-wish",
    "activity": "event-battle",
    "explore_claim": "exploration-claim",
    "gongongqu": "public-area-dispatch",
    "battle": "spend-stamina",
    "guild": "squad-tasks",
    "claim_quest": "claim-daily-missions",
    "xunlu": "battle-pass-free-track",
}
_choice_context = ContextVar('gf2_official_reward_choice', default=None)
_inspection_context = ContextVar('gf2_readonly_inspection', default=None)
_INSPECTION_OPERATIONS = {
    'claim-daily-missions': ('inspect_daily_missions', frozenset({'daily-missions'})),
    'battle-pass-free-track': ('inspect_battle_pass', frozenset({'battle-pass-actions'} |
        {'battle-pass-rewards-' + str(index) for index in range(8)})),
    'activity-supply': ('inspect_activity_supply', frozenset({'supply-intelligence', 'supply-prebattle',
        'supply-intelligence-menu-not-found', 'supply-prebattle-menu-not-found'})),
    'exploration-claim': ('inspect_exploration', frozenset({'exploration-status'})),
}


def _install_inspection(task_type):
    """Export only the source inspection task's real OCR inputs, never recapture."""
    if getattr(task_type, '_yeyu_inspection_observer', False):
        return
    original_page, original_run = task_type.inspect_page, task_type.run

    @wraps(original_page)
    def page(self, name, *args, **kwargs):
        result = original_page(self, name, *args, **kwargs)
        context = _inspection_context.get()
        if context is None:
            return result
        try:
            from src.config import config
            root = (Path(config.get('screenshots_folder', 'screenshots')) / 'reward-status').resolve()
            source = Path(result['imagePath']).resolve(strict=True)
            if not source.is_relative_to(root) or source.is_symlink() or result['page'] != name:
                return result
            operations = context['selected'] if name in {'account-before', 'account-after'} else {
                operation for operation, (_, pages) in _INSPECTION_OPERATIONS.items() if name in pages}
            if not 0 < source.stat().st_size <= 20 * 1024 * 1024:
                return result
            content = source.read_bytes()
            for operation in operations & context['selected']:
                if (operation, name) in context['pages'] or context['bytes'] + len(content) > 128 * 1024 * 1024:
                    continue
                identifier = uuid.uuid4().hex
                filename = 'gf2-reward-status-' + identifier + '.png'
                stamp = datetime.now().astimezone().isoformat()
                target = Path(context['path']).parent
                (target / filename).write_bytes(content)
                metadata = {'schemaVersion': 1, 'recognitionId': identifier, 'page': name,
                    'operation': operation, 'method': 'official-onnxocr-source-input',
                    'sourceTask': 'RewardStatusInspectionTask.inspect_page',
                    'inputImage': filename, 'inputImageSha256': sha256(content).hexdigest(),
                    'imageSize': result['imageSize'], 'coordinateSpace': 'recognition-input-pixels',
                    'observedAt': stamp, 'timeMeaning': 'source-ocr-input-export-observed',
                    'frameCapturedAt': None, 'frameCaptureTimeKnown': False,
                    'readonlyInspection': True, 'diagnosticOnly': True}
                meta_name = filename[:-4] + '.json'
                (target / meta_name).write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf8')
                for file, kind in ((filename, 'game-ui-recognition-raw'), (meta_name, 'game-ui-recognition-meta')):
                    _emit(context['path'], operation, 'step_artifact',
                          'file=%s; kind=%s; capturedAt=%s' % (file, kind, stamp))
                context['pages'].add((operation, name))
                context['bytes'] += len(content)
        except Exception:
            # Missing evidence is reported by Manager; observation never changes source behavior.
            pass
        return result

    supported_operations = set()
    for operation, (method_name, _) in _INSPECTION_OPERATIONS.items():
        original = getattr(task_type, method_name, None)
        if not callable(original):
            continue  # Earlier audited core-only candidates remain supported.
        supported_operations.add(operation)
        def observe(original, operation):
            @wraps(original)
            def method(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                context = _inspection_context.get()
                if context is not None:
                    context['results'][operation] = result
                return result
            return method
        setattr(task_type, method_name, observe(original, operation))

    @wraps(original_run)
    def run(self, *args, **kwargs):
        _install_upstream_logging()
        path, selected = _context()
        if not path or not selected or not selected <= {'attach-home'} | supported_operations:
            return original_run(self, *args, **kwargs)
        operations = selected & supported_operations
        token = _inspection_context.set({'path': path, 'selected': operations, 'pages': set(), 'bytes': 0, 'results': {}})
        for operation in operations:
            _emit(path, operation, 'started', 'official RewardStatusInspectionTask.run entered; read-only')
        try:
            result = original_run(self, *args, **kwargs)
        except Exception as error:
            for operation in operations:
                _emit(path, operation, 'failed', 'official readonly inspection raised ' + type(error).__name__)
            raise
        else:
            for operation in operations:
                state = 'completed' if _inspection_context.get()['results'].get(operation) is True else 'failed'
                _emit(path, operation, state, 'official readonly page inspection returned; independent verification required')
            return result
        finally:
            _inspection_context.reset(token)
    task_type.inspect_page, task_type.run = page, run
    task_type._yeyu_inspection_observer = True


def _install_choice_observer(task_type):
    original = getattr(task_type, 'log_error', None)
    if not callable(original): return
    @wraps(original)
    def log_error(self, message, *args, **kwargs):
        result = original(self, message, *args, **kwargs)
        context = _choice_context.get()
        choice_required = (message == '未配置拂晓之光补给包奖励，请手动选择'
                           or isinstance(message, str) and message.startswith('补给包未找到配置奖励「')
                           and message.endswith('」，请手动选择'))
        if context is not None and context['task'] is self and choice_required and not context['human']:
            context['human'] = True
            _emit(context['path'], 'battle-pass-free-track', 'human_required',
                  'gf2_supply_reward_choice_required: ' + message)
        return result
    task_type.log_error = log_error

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

def _observe(operation, original, once=False, false_is_failure=True):
    @wraps(original)
    def wrapped(self, *args, **kwargs):
        _install_upstream_logging(); path, selected = _context()
        active = operation in selected and (not once or not getattr(self, "_yeyu_attach_seen", False))
        if active and once: self._yeyu_attach_seen = True
        if active:
            _emit(path, operation, "started", "official DailyTask method entered: " + original.__name__)
        choice = {'task': self, 'path': path, 'human': False} if active and operation == 'battle-pass-free-track' else None
        token = _choice_context.set(choice)
        try:
            with recognition_scope(self, path if active else None, operation, _emit, "gf2",
                                   max_images=2, first_distinct=0):
                result = original(self, *args, **kwargs)
        except Exception as error:
            if active:
                if choice is None or not choice['human']:
                    _emit(path, operation, "failed", "official %s raised %s: %s" % (original.__name__, type(error).__name__, error))
            raise
        finally:
            _choice_context.reset(token)
        if active and (choice is None or not choice['human']):
            state = "uncertain" if result == "待核查" else "failed" if false_is_failure and result is False else "completed"
            detail = "official DailyTask method %s returned %r" % (original.__name__, result)
            if not false_is_failure:
                detail = "official prerequisite method %s returned %r" % (original.__name__, result)
            _emit(path, operation, state, detail)
        return result
    return wrapped

def _install_methods(task_type):
    """Wrap only methods present in this upstream version.

    Older GF2 releases do not expose the four main-branch event methods.  A
    runner rejects a selected unavailable method before launch. Installing an
    observer is not a task attempt; methods that exist remain observable.
    """
    for method_name, operation in _METHOD_OPERATIONS.items():
        original = getattr(task_type, method_name, None)
        if not callable(original):
            continue
        setattr(task_type, method_name, _observe(operation, original))
    original_ensure_main = getattr(task_type, "ensure_main", None)
    if callable(original_ensure_main):
        task_type.ensure_main = _observe("attach-home", original_ensure_main, once=True, false_is_failure=False)


def _install_failure_exit(task_type, communicate):
    from OfficialTaskExit import install_failure_exit
    install_failure_exit(task_type, communicate, "GF2Adapter")


def install():
    if os.environ.get('YEYU_GAMER_GF2_REWARD_INSPECTION_ONLY') == '1':
        from src.tasks.RewardStatusInspectionTask import RewardStatusInspectionTask
        if getattr(RewardStatusInspectionTask, '_yeyu_inspection_bootstrap', False):
            return
        _install_inspection(RewardStatusInspectionTask)
        RewardStatusInspectionTask.ensure_main = _observe(
            'attach-home', RewardStatusInspectionTask.ensure_main, once=True, false_is_failure=False)
        from ok.core.events import communicate
        _install_failure_exit(RewardStatusInspectionTask, communicate)
        RewardStatusInspectionTask._yeyu_inspection_bootstrap = True
        return
    from src.tasks.DailyTask import DailyTask
    from ok.core.events import communicate
    if getattr(DailyTask, "_yeyu_observer_v7", False): return
    install_recognition_observers()
    _install_choice_observer(DailyTask)
    _install_methods(DailyTask)
    _install_failure_exit(DailyTask, communicate)
    DailyTask._yeyu_observer_v7 = True

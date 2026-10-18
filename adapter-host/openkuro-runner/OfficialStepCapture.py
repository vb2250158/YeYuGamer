"""Passive snapshots of frames actually consumed by official recognition.

The legacy method-boundary helper remains readable for historical replays.
New observers use recognition_scope: no click hook and no capture-provider IO.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
import hashlib
import inspect
import json
import math
import os
import time
import uuid


_scope = ContextVar("yeyu_official_recognition_scope", default=None)
_ocr_input = ContextVar("yeyu_official_ocr_input", default=None)
_color_decision = ContextVar("yeyu_official_color_decision", default=None)
_budgets = {}
_run_bytes = {}
# Sampling is subordinate to the protocol's 256 MiB aggregate hard limit.
# Keep half for complete official logs and other diagnostic artifacts.
_MAX_RECOGNITION_RUN_BYTES = 128 * 1024 * 1024
_MAX_RECOGNITION_ARTIFACT_BYTES = 20 * 1024 * 1024
_FIRST_DISTINCT = 6
_MAX_IMAGES = 8  # PNG + metadata JSON <= 16 artifacts, leaving room below 20.


def reset_recognition_budget(path):
    _run_bytes.pop(path, None)
    for key in list(_run_bytes):
        if isinstance(key, tuple) and key[0] == path:
            del _run_bytes[key]
    for key in list(_budgets):
        if key[0] == path:
            del _budgets[key]


def _safe_emit(context, state, detail):
    try:
        context["emit"](context["path"], context["operation"], state, detail)
    except Exception:
        pass


@contextmanager
def recognition_scope(task, path, operation, emit, prefix, identity=None,
                      max_images=_MAX_IMAGES, first_distinct=_FIRST_DISTINCT,
                      template_history_names=(), byte_budget_identity=None,
                      retain_other_templates=False, verification_kind=None, verification_page=None,
                      template_sample_interval_seconds=0):
    """Associate official recognizers with an existing step, never fetch a frame.

    The first distinct recognitions and the latest OCR/template input are
    retained within the caller's budget. Latest inputs are copied and flushed
    before the step terminal; a later task boundary is never photographed.
    The caller supplies upstream-confirmed identity where one is available.
    A multi-account caller may partition bytes by that confirmed identity when
    its stages are imported into separate Manager RunAttempts. The anonymous
    startup retains the shared path budget; no account is inferred.
    Optional interval samples use only the named templates and the existing
    first-input quota, leaving space for the final OCR/template inputs.
    """
    context = None
    token = None
    try:
        if path:
            key = (path, prefix, operation, identity)
            maximum = min(_MAX_IMAGES, max(2, int(max_images)))
            history_names = frozenset(template_history_names)
            reserved = 3 if history_names else 2
            if history_names and retain_other_templates:
                reserved += 1
            first = min(max(0, maximum - reserved), max(0, int(first_distinct)))
            interval = float(template_sample_interval_seconds)
            if not math.isfinite(interval) or interval < 0:
                raise ValueError("template sample interval must be finite and nonnegative")
            if interval:
                interval = max(1.0, interval)
            budget = _budgets.setdefault(key, {"saved": 0, "seen": set(),
                                               "maximum": maximum, "first": first})
            # A repeated nested scope cannot expand a previously issued quota.
            budget["maximum"] = min(budget["maximum"], maximum)
            budget["first"] = min(budget["first"], first)
            context = {"task": task, "path": path, "operation": operation,
                       "emit": emit, "prefix": prefix, "identity": identity,
                       "budget": budget, "latest": {}, "observed": 0,
                       "byte_budget_key": (path, byte_budget_identity) if byte_budget_identity is not None else path,
                       "template_history_names": history_names,
                       "template_sample_interval_seconds": interval,
                       "next_template_sample_at": 0.0,
                       "retain_other_templates": retain_other_templates,
                       "verification_kind": verification_kind, "verification_page": verification_page}
            token = _scope.set(context)
        # An unselected/repeated prerequisite may be nested inside a selected
        # step. Preserve that parent's scope rather than suppressing its OCR.
    except Exception:
        pass
    try:
        yield
    finally:
        if context is not None:
            try:
                for sample, snapshot in context["latest"].items():
                    _persist(context, snapshot, "latest-" + sample)
                if context["observed"] == 0:
                    _safe_emit(context, "progress", "step_capture_unavailable:recognition:no_official_recognition_input")
            except Exception as error:
                _safe_emit(context, "progress", "step_capture_unavailable:recognition:" + type(error).__name__)
        if token is not None:
            _scope.reset(token)


def _value(value):
    """Bounded descriptive serialization; never iterate an official generator."""
    if value is None or isinstance(value, (str, bool, int)):
        return value[:512] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value[:100]]
    # Box is an official recognition output. Include actual coordinates,
    # labels and confidence without changing or sorting the returned object.
    if all(hasattr(value, key) for key in ("x", "y", "width", "height")):
        result = {}
        for key in ("x", "y", "width", "height", "name", "confidence"):
            item = getattr(value, key, None)
            if hasattr(item, "item"):
                item = item.item()
            result[key] = _value(item)
        return result
    if hasattr(value, "pattern"):
        return {"pattern": str(value.pattern)[:512], "flags": int(value.flags)}
    return {"type": type(value).__name__}


def _arguments(signature, self, args, kwargs):
    bound = signature.bind(self, *args, **kwargs)
    bound.apply_defaults()
    return bound.arguments


def _input_snapshot(context, owner, arguments, recognition_type, source):
    image = arguments.get("image") if recognition_type == "ocr" else arguments.get("mat")
    if image is None:
        return None
    frame = image
    coordinate_space = "full-frame"
    frame_source = "official-template-mat"
    if recognition_type == "ocr":
        # OCR.ocr has already resolved executor.frame before it calls the
        # selected engine. Read that same cache, never executor.frame/get_frame.
        ocr_input = _ocr_input.get()
        full = None
        if ocr_input is not None and ocr_input[0] is owner:
            full = ocr_input[1]
            if full is None:
                full = owner.executor.nullable_frame()
                frame_source = "official-ocr-resolved-cache"
            else:
                frame_source = "official-ocr-explicit-frame"
        if full is not None:
            frame = full
        else:
            coordinate_space = "processed-ocr-region"
            frame_source = "official-ocr-engine-input"
    copied = frame.copy()
    captured_at = datetime.now().astimezone().isoformat()
    metadata = {
        "schemaVersion": 1, "recognitionId": uuid.uuid4().hex,
        "operation": context["operation"], "source": source,
        "recognitionType": recognition_type, "capturedAt": captured_at,
        "observedAt": captured_at, "timeMeaning": "recognition-input-observed",
        "frameCapturedAt": None, "frameCaptureTimeKnown": False,
        "frameSource": frame_source, "coordinateSpace": coordinate_space,
        "frameShape": list(copied.shape), "recognizerInputShape": list(image.shape),
        "recognizerInputSha256": hashlib.sha256(image.tobytes()).hexdigest(),
        "region": _value(arguments.get("box")),
        "match": _value(arguments.get("match")),
        "template": _value(arguments.get("category_name")),
        "threshold": _value(arguments.get("threshold")),
        "scaleFactor": _value(arguments.get("scale_factor")),
        "upstreamConfirmedAccount": context["identity"],
        "screenshotProcessing": "official-privacy-blur-and-screenshot-processor",
    }
    if context.get("verification_page") in {"tasks", "rewards"} and context["operation"] == "claim-battle-pass":
        metadata["verificationPage"] = context["verification_page"]
    for key in ("x", "y", "to_x", "to_y", "width", "height", "horizontal_variance", "vertical_variance"):
        if key in arguments:
            metadata[key] = _value(arguments[key])
    return {"frame": copied, "metadata": metadata}


def _persist(context, snapshot, sampling):
    budget = context["budget"]
    if budget["saved"] >= budget["maximum"]:
        return
    metadata = snapshot["metadata"]
    # Dedup includes actual pixels and recognizer output, so identical polls
    # are bounded while changed text/boxes on the same frame stay observable.
    signature = hashlib.sha256(snapshot["frame"].tobytes() + json.dumps(
        {key: metadata.get(key) for key in ("source", "region", "match", "template", "result", "exceptionType")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if signature in budget["seen"]:
        return
    budget["seen"].add(signature)
    budget["saved"] += 1  # Failure also consumes budget; never retry IO in a poll.
    try:
        from ok import og
        folder = os.path.dirname(context["path"])
        name = context["prefix"] + "-recognition-" + metadata["recognitionId"]
        saved = og.ok.screenshot.generate_screen_shot(snapshot["frame"], {}, folder, name, False, None)
        if not saved or not os.path.isfile(saved):
            raise OSError("official screenshot writer produced no file")
        metadata["sampling"] = sampling
        metadata["screenshotFile"] = os.path.basename(saved)
        with open(saved, "rb") as stream:
            metadata["screenshotSha256"] = hashlib.sha256(stream.read()).hexdigest()
        metadata_path = os.path.join(folder, name + ".json")
        with open(metadata_path, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False)
        image_bytes = os.path.getsize(saved)
        metadata_bytes = os.path.getsize(metadata_path)
        total_bytes = image_bytes + metadata_bytes
        byte_budget_key = context["byte_budget_key"]
        used_bytes = _run_bytes.get(byte_budget_key, 0)
        if (max(image_bytes, metadata_bytes) > _MAX_RECOGNITION_ARTIFACT_BYTES
                or used_bytes + total_bytes > _MAX_RECOGNITION_RUN_BYTES):
            # Keep the actual input locally, but do not publish a partial pair
            # that would abort the official tool at Manager's transport gate.
            _safe_emit(context, "progress", "step_capture_unavailable:recognition:artifact_byte_budget; files retained locally")
            return
        _run_bytes[byte_budget_key] = used_bytes + total_bytes
        detail = "; capturedAt=" + metadata["capturedAt"] + "; recognitionId=" + metadata["recognitionId"]
        kind = "game-ui-recognition-raw"
        if (context.get("verification_kind") == "game-ui-key-step-verification-raw"
                and context["prefix"] == "ww" and context["operation"] == "inspect-daily-progress"
                and context["identity"] and metadata["recognitionType"] == "ocr"
                and metadata["coordinateSpace"] == "full-frame"):
            kind = "game-ui-key-step-verification-raw"
        if (context.get("verification_kind") == "game-ui-reward-verification-raw"
                and context["prefix"] == "ww" and context["operation"] == "claim-daily-reward"
                and context["identity"] and metadata["recognitionType"] == "template"
                and metadata["coordinateSpace"] == "full-frame"
                and metadata["template"] == "daily_reward_claimed_check"):
            kind = "game-ui-reward-verification-raw"
        if (context.get("verification_kind") == "game-ui-reward-verification-raw"
                and context["prefix"] == "ww" and context["operation"] == "claim-battle-pass"
                and context["identity"] and metadata["recognitionType"] == "ocr"
                and metadata["coordinateSpace"] == "full-frame"
                and metadata.get("verificationPage") in {"tasks", "rewards"}):
            kind = "game-ui-reward-verification-raw"
        _safe_emit(context, "step_artifact", "file=" + os.path.basename(saved) + "; kind=" + kind + detail)
        _safe_emit(context, "step_artifact", "file=" + os.path.basename(metadata_path) + "; kind=official-recognition-metadata" + detail)
    except Exception as error:
        _safe_emit(context, "progress", "step_capture_unavailable:recognition:%s: %s" % (type(error).__name__, error))


def _record(context, snapshot, result=None, error=None):
    if snapshot is None:
        return
    context["observed"] += 1
    metadata = snapshot["metadata"]
    metadata["resultState"] = "raised" if error is not None else "returned"
    metadata["result"] = _value(result)
    if error is not None:
        metadata["exceptionType"] = type(error).__name__
    family = metadata["recognitionType"]
    history_names = context["template_history_names"]
    if family == "template" and history_names:
        if isinstance(metadata["template"], str) and metadata["template"] in history_names:
            # ensure_main probes the same cached input twice. Keep the prior
            # distinct input instead of replacing it with that duplicate;
            # still refresh current so its observation time stays truthful.
            snapshot["history_key"] = json.dumps(
                {key: metadata.get(key) for key in (
                    "source", "recognizerInputSha256", "region", "match",
                    "template", "result", "exceptionType")},
                ensure_ascii=False, sort_keys=True)
            previous = context["latest"].get("template-current")
            if previous is not None and previous["history_key"] != snapshot["history_key"]:
                context["latest"]["template-previous"] = previous
            context["latest"]["template-current"] = snapshot
        elif context["retain_other_templates"]:
            context["latest"]["template"] = snapshot
    else:
        context["latest"][family] = snapshot
    if context["budget"]["saved"] < context["budget"]["first"]:
        interval = context.get("template_sample_interval_seconds", 0)
        if interval:
            if family == "template" and metadata["template"] in history_names:
                now = time.monotonic()
                if now >= context["next_template_sample_at"]:
                    context["next_template_sample_at"] = now + interval
                    _persist(context, snapshot, "interval-template")
        else:
            _persist(context, snapshot, "first-distinct")


def _wrap_recognizer(owner_type, method_name, recognition_type):
    original = getattr(owner_type, method_name, None)
    if not callable(original) or getattr(original, "_yeyu_recognition_observer", False):
        return
    signature = inspect.signature(original)

    @wraps(original)
    def observed(self, *args, **kwargs):
        context = _scope.get()
        snapshot = None
        if context is not None:
            try:
                valid_owner = ((self is context["task"] or self.executor is context["task"].executor) if recognition_type == "ocr" else
                               self is context["task"].executor.feature_set)
                if valid_owner:
                    snapshot = _input_snapshot(context, self, _arguments(signature, self, args, kwargs),
                                               recognition_type, owner_type.__name__ + "." + method_name)
            except Exception as error:
                _safe_emit(context, "progress", "step_capture_unavailable:recognition:%s" % type(error).__name__)
        try:
            result = original(self, *args, **kwargs)
        except Exception as error:
            try:
                if context is not None:
                    _record(context, snapshot, error=error)
            except Exception:
                pass
            raise
        try:
            if context is not None:
                _record(context, snapshot, result=result)
        except Exception:
            pass
        return result

    observed._yeyu_recognition_observer = True
    setattr(owner_type, method_name, observed)


def install_recognition_observers(ocr_type=None, feature_type=None):
    """Wrap existing OCR engines and template matcher without changing inputs."""
    try:
        if ocr_type is None:
            from ok.task.task import OCR
            ocr_type = OCR
        if feature_type is None:
            from ok.feature.FeatureSet import FeatureSet
            feature_type = FeatureSet
        original = getattr(ocr_type, "ocr", None)
        if callable(original) and not getattr(original, "_yeyu_recognition_context", False):
            signature = inspect.signature(original)

            @wraps(original)
            def ocr(self, *args, **kwargs):
                token = None
                try:
                    frame = _arguments(signature, self, args, kwargs).get("frame")
                    token = _ocr_input.set((self, frame))
                except Exception:
                    pass
                try:
                    return original(self, *args, **kwargs)
                finally:
                    if token is not None:
                        _ocr_input.reset(token)

            ocr._yeyu_recognition_context = True
            ocr_type.ocr = ocr
        for name in ("rapid_ocr", "paddle_ocr", "duguang_ocr", "onnx_ocr"):
            _wrap_recognizer(ocr_type, name, "ocr")
        _wrap_recognizer(feature_type, "find_one_feature", "template")
    except Exception:
        pass


def install_color_rectangle_observer(owner_type, decision_module, image_utils):
    """Observe the existing NTE brightness/morphology/rectangle decision chain.

    Copies the actual binarizer argument and records the actual rectangle
    result. It never reads a controller or task frame property. Thread-local
    ownership prevents unrelated uses of the shared image utility being saved.
    """
    method_name = "get_activity_reward_box"
    original_method = getattr(owner_type, method_name, None)
    if not callable(original_method) or getattr(original_method, "_yeyu_color_observer", False):
        return
    original_binary = image_utils.binarize_bgr_by_brightness
    original_rectangles = decision_module.find_color_rectangles
    binary_signature = inspect.signature(original_binary)
    rectangle_signature = inspect.signature(original_rectangles)

    @wraps(original_binary)
    def binary(*args, **kwargs):
        decision = _color_decision.get()
        if decision is not None:
            decision["snapshot"] = None
            try:
                arguments = binary_signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                snapshot = _input_snapshot(
                    decision["context"], None, {"mat": arguments.arguments.get("image")},
                    "color", owner_type.__name__ + "." + method_name + ":find_color_rectangles",
                )
                if snapshot is not None:
                    snapshot["metadata"]["frameSource"] = "official-color-source-input"
                    snapshot["metadata"]["sourceTransform"] = "brightness-binary-then-morphology"
                    snapshot["metadata"]["brightnessThreshold"] = _value(arguments.arguments.get("threshold"))
                decision["snapshot"] = snapshot
            except Exception as error:
                _safe_emit(decision["context"], "progress", "step_capture_unavailable:color:%s" % type(error).__name__)
        return original_binary(*args, **kwargs)

    @wraps(original_rectangles)
    def rectangles(*args, **kwargs):
        decision = _color_decision.get()
        snapshot = None
        if decision is not None:
            snapshot = decision.pop("snapshot", None)
            try:
                arguments = rectangle_signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                if snapshot is not None:
                    # The recognizer receives the transformed mask. Keep its
                    # hash separately from the original color-source frame.
                    mask = next(iter(arguments.arguments.values()))
                    metadata = snapshot["metadata"]
                    metadata["nativeDecisionInputSha256"] = hashlib.sha256(mask.tobytes()).hexdigest()
                    metadata["nativeDecisionInputShape"] = list(mask.shape)
                    metadata["region"] = _value(arguments.arguments.get("box"))
                    metadata["threshold"] = _value(arguments.arguments.get("threshold"))
            except Exception as error:
                snapshot = None
                _safe_emit(decision["context"], "progress", "step_capture_unavailable:color:%s" % type(error).__name__)
        try:
            result = original_rectangles(*args, **kwargs)
        except Exception as error:
            try:
                if decision is not None:
                    _record(decision["context"], snapshot, error=error)
            except Exception:
                pass
            raise
        try:
            if decision is not None:
                _record(decision["context"], snapshot, result=result)
        except Exception:
            pass
        return result

    @wraps(original_method)
    def observed(self, *args, **kwargs):
        context = _scope.get()
        token = _color_decision.set(
            {"context": context, "snapshot": None}
            if isinstance(context, dict) and context.get("task") is self
            and context.get("prefix") == "nte" and context.get("operation") == "claim-daily-reward"
            else None
        )
        try:
            return original_method(self, *args, **kwargs)
        finally:
            _color_decision.reset(token)

    binary._yeyu_color_source_observer = True
    rectangles._yeyu_color_result_observer = True
    observed._yeyu_color_observer = True
    if not getattr(original_binary, "_yeyu_color_source_observer", False):
        image_utils.binarize_bgr_by_brightness = binary
    if not getattr(original_rectangles, "_yeyu_color_result_observer", False):
        decision_module.find_color_rectangles = rectangles
    setattr(owner_type, method_name, observed)


def capture_step(task, path, operation, phase, emit, prefix, read_frame=None):
    """Save the tool-owned frame before the task thread advances."""
    if not path:
        return
    try:
        from ok import og
        from PIL import Image, ImageDraw
        frame = task.executor.nullable_frame() if read_frame is None else read_frame(task)
        if frame is None:
            raise ValueError("official frame buffer unavailable")
        captured_at = datetime.now().astimezone().isoformat()
        folder = os.path.dirname(path)
        name = prefix + "-step-" + uuid.uuid4().hex
        screenshot = og.ok.screenshot
        # Preserve the official blur/processor and PNG encoder. Own the copy
        # so screenshot processing cannot modify the task's recognition frame.
        saved = screenshot.generate_screen_shot(frame.copy(), {}, folder, name, False, None)
        if not saved or not os.path.isfile(saved):
            raise OSError("official screenshot writer produced no file")
        kind = "game-ui-step-" + phase + "-raw"
        emit(path, operation, "step_artifact", "file=%s; kind=%s; capturedAt=%s" %
              (os.path.basename(saved), kind, captured_at))
        if phase == "after":
            with Image.open(saved) as original:
                marked = original.convert("RGB")
            ImageDraw.Draw(marked).text((12, 12), "YeYu Gamer | %s | %s | official buffer" %
                                       (captured_at, operation), fill="white", stroke_width=1, stroke_fill="black")
            marked_path = screenshot.save_pil_image(name + "-watermarked", folder, marked)
            emit(path, operation, "step_artifact", "file=%s; kind=game-ui-step-after-watermarked; capturedAt=%s" %
                  (os.path.basename(marked_path), captured_at))
    except Exception as error:
        emit(path, operation, "progress", "step_capture_unavailable:%s:%s: %s" %
              (phase, type(error).__name__, error))

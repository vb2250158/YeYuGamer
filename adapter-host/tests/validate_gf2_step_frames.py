"""Bound-official-writer replay for recognition snapshots; no game/window IO."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
GAME = sys.argv[1] if len(sys.argv) > 1 else "GF2"
assert GAME in {"GF2", "Endfield", "WW", "NTE"}
sys.path.insert(0, str(ROOT / "adapter-host/openkuro-runner"))
if GAME == "GF2":
    sys.path.insert(0, r"C:\Game\YeYuGamerCandidates\gf2-official-main-4122e01\GF2\source")
from ok import og
from ok.core.screenshot import Screenshot
from ok.task.task import OCR
from ok.feature.Box import Box
from OfficialStepCapture import install_recognition_observers, recognition_scope, reset_recognition_budget


def main():
    module = {"GF2": "Gf2YeYuObserver.py", "Endfield": "EndfieldYeYuBridge.py", "WW": "WwYeYuObserver.py", "NTE": "NteYeYuBridge.py"}[GAME]
    runner = "nte-runner" if GAME == "NTE" else "openkuro-runner"
    spec = importlib.util.spec_from_file_location("frame_observer", ROOT / "adapter-host" / runner / module)
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    screenshot = object.__new__(Screenshot)
    og.ok = SimpleNamespace(screenshot=screenshot)
    og.config = {}
    captures = []
    original_boxes = [Box(10, 20, 15, 10, .96, "official-result")]
    engine_returns = (original_boxes, original_boxes)

    class Feature:
        def find_one_feature(self, mat, category_name, box=None, threshold=0):
            captures.append(("template", mat))
            return original_boxes

    class Task(OCR):
        executor = None
        debug = False
        def __init__(self):
            # Real official OCR.ocr body, with engine/window collaborators
            # replaced. No official task object is constructed.
            self.executor = SimpleNamespace(
                paused=False, ocr_po_translation=None, feature_set=Feature(),
                nullable_frame=lambda: self.cache,
                method=SimpleNamespace(get_frame=lambda: (_ for _ in ()).throw(AssertionError("recaptured"))))
            self.cache = None
            self.ocr_default_threshold = .2
            self.log_debug = False
            self.debug = False
        def ocr_fun(self, lib):
            return self.rapid_ocr
        def rapid_ocr(self, box, image, match, scale_factor, threshold, lib):
            captures.append(("ocr", image))
            if getattr(self, "engine_error", None):
                raise self.engine_error
            return engine_returns

    install_recognition_observers(Task, Feature)
    wrapped_engine = Task.rapid_ocr
    install_recognition_observers(Task, Feature)
    assert Task.rapid_ocr is wrapped_engine, "recognizer was wrapped twice"
    task = Task()
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory) / "stage.jsonl"
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = '["mail"]'
        ocr_frame = np.full((90, 160, 3), (10, 20, 30), dtype=np.uint8)
        template_frame = np.full((90, 160, 3), (70, 80, 90), dtype=np.uint8)
        ocr_before = ocr_frame.copy()
        template_before = template_frame.copy()

        def original(self):
            result = self.ocr(frame=ocr_frame, box=Box(5, 10, 40, 30), match="official-result")
            assert result[0] is original_boxes[0]
            assert self.executor.feature_set.find_one_feature(template_frame, "page-anchor") is original_boxes
            # A later next-step buffer change cannot contaminate these inputs.
            ocr_frame[:] = 255
            template_frame[:] = 255
            self.cache = None
            return "official-result"

        if GAME == "WW":
            Task.step = original
            observer._observe(Task, "step", "mail")
            assert task.step() == "official-result"
        elif GAME == "NTE":
            Task.step = original
            observer._wrap_claim_method(Task, "step", "mail", observer.StageReporter())
            assert task.step() == "official-result"
        else:
            assert observer._observe("mail", original)(task) == "official-result"
        assert task.cache is None, "observer changed official recognition cache"
        records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                   for record in records if record["state"] == "step_artifact"]
        images = [cv2.imread(str(Path(directory) / detail["file"]))
                  for detail in details if detail["kind"] == "game-ui-recognition-raw"]
        metadata = [json.loads((Path(directory) / detail["file"]).read_text(encoding="utf-8"))
                    for detail in details if detail["kind"] == "official-recognition-metadata"]
        assert len(images) == len(metadata) == 2
        assert np.array_equal(images[0], ocr_before)
        assert np.array_equal(images[1], template_before)
        assert metadata[0]["frameSource"] == "official-ocr-explicit-frame"
        assert metadata[0]["region"]["x"] == 5 and metadata[0]["recognizerInputShape"] == [30, 40, 3]
        assert metadata[0]["result"][0][0]["name"] == "official-result"
        assert metadata[0]["frameCapturedAt"] is None and metadata[0]["frameCaptureTimeKnown"] is False
        assert metadata[0]["timeMeaning"] == "recognition-input-observed"
        assert metadata[1]["template"] == "page-anchor"
        for item in metadata:
            assert item["screenshotSha256"] == hashlib.sha256((Path(directory) / item["screenshotFile"]).read_bytes()).hexdigest()
        assert records[0]["state"] == "started" and records[-1]["state"] == "completed"

        if GAME == "WW":
            # One official MultiAccount process feeds separate Manager
            # attempts. Early polls must not consume the next account's
            # recognition transport budget or replace the final inputs.
            import OfficialStepCapture as capture
            reset_recognition_budget(str(stage))
            def polling(self):
                for value in range(1, 31):
                    frame = np.full((90, 160, 3), value, dtype=np.uint8)
                    assert self.ocr(frame=frame)[0] is original_boxes[0]
                    assert self.executor.feature_set.find_one_feature(frame, "page-anchor") is original_boxes
                return engine_returns
            Task.account_step = polling
            observer._observe(Task, "account_step", "account-budget")
            Task.after_detection_step = polling
            observer._observe(Task, "after_detection_step", "account-after-detection")
            with patch.object(capture, "_MAX_RECOGNITION_RUN_BYTES", 4500):
                for label in (None, "fixture-account-beta"):
                    observer._current_account["label"] = label
                    assert task.account_step() is engine_returns
                    if label is None:
                        observer._set_current_account("fixture-account-alpha")
                        assert task.after_detection_step() is engine_returns
                        assert (str(stage), "fixture-account-alpha") not in capture._run_bytes
            observer._current_account["label"] = None
            observer._current_account["first_label"] = None
            account_records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            account_details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                               for record in account_records if record["state"] == "step_artifact"
                               and record["operation"] == "account-budget"]
            account_metadata = [json.loads((Path(directory) / item["file"]).read_text(encoding="utf-8"))
                                for item in account_details if item["kind"] == "official-recognition-metadata"]
            assert len(account_metadata) == 4, "separate account attempts lost complete final input pairs"
            for label in (None, "fixture-account-beta"):
                samples = [item for item in account_metadata if item["upstreamConfirmedAccount"] == label]
                assert {item["sampling"] for item in samples} == {"latest-ocr", "latest-template"}
                assert all(np.all(cv2.imread(str(Path(directory) / item["screenshotFile"])) == 30)
                           for item in samples), "an early poll consumed a terminal input slot"
            assert not any("artifact_byte_budget" in record["detail"] for record in account_records
                           if record["operation"] == "account-budget")
            assert sum(capture._run_bytes.values()) > 4500, "fixture did not exercise separate transport limits"
            assert all(size <= 4500 for size in capture._run_bytes.values())
            reset_recognition_budget(str(stage))
            assert not any(key == str(stage) or isinstance(key, tuple) and key[0] == str(stage)
                           for key in capture._run_bytes), "account partitions survived reset"

        # Official delegated tasks sharing this executor remain in the parent
        # operation; an unrelated executor is never collected.
        child = Task()
        child.executor = task.executor
        def emit(path, op, state, detail):
            if GAME == "NTE":
                observer.StageReporter().emit(op, state, detail)
            elif GAME == "WW":
                observer._emit(op, state, detail)
            else:
                observer._emit(path, op, state, detail)
        with recognition_scope(task, str(stage), "delegate", emit, "fixture"):
            child.ocr(frame=ocr_before)
            stranger = Task()
            stranger.ocr(frame=template_before)
        assert len([line for line in stage.read_text(encoding="utf-8").splitlines() if '"operation": "delegate"' in line and '"state": "step_artifact"' in line]) == 2

        with recognition_scope(task, str(stage), "parent-step", emit, "fixture"):
            with recognition_scope(task, None, "already-observed-prerequisite", emit, "fixture"):
                task.ocr(frame=ocr_before)
        assert len([line for line in stage.read_text(encoding="utf-8").splitlines() if '"operation": "parent-step"' in line and '"state": "step_artifact"' in line]) == 2
        assert '"operation": "already-observed-prerequisite"' not in stage.read_text(encoding="utf-8")

        # Thirty distinct calls must remain below the Todo artifact limit and
        # retain the last actual OCR input, without an end-of-step recapture.
        with recognition_scope(task, str(stage), "bounded", emit, "fixture"):
            for value in range(1, 31):
                task.ocr(frame=np.full((90, 160, 3), value, dtype=np.uint8))
        bounded = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        bounded = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                   for record in bounded if record["state"] == "step_artifact" and record["operation"] == "bounded"]
        assert len(bounded) <= 16
        final_file = [item["file"] for item in bounded if item["kind"] == "game-ui-recognition-raw"][-1]
        assert np.all(cv2.imread(str(Path(directory) / final_file)) == 30)

        # Preserve adjacent inputs of the actual failed prerequisite probe.
        # Later unrelated templates must not overwrite that diagnostic history.
        with recognition_scope(task, str(stage), "probe-history", emit, "fixture",
                               template_history_names=("esc", "esc_2k", "esc_4k")):
            for value in range(1, 7):
                task.executor.feature_set.find_one_feature(
                    np.full((90, 160, 3), value, dtype=np.uint8), "startup-" + str(value))
            for value in (101, 102, 103):
                probe = np.full((90, 160, 3), value, dtype=np.uint8)
                assert task.executor.feature_set.find_one_feature(probe, "esc_4k") is original_boxes
                probe[:] = 255
            # Two official calls on the same cached frame must not erase
            # the preceding distinct frame when artifact dedup runs.
            for _ in range(2):
                assert task.executor.feature_set.find_one_feature(
                    np.full((90, 160, 3), 103, dtype=np.uint8), "esc_4k") is original_boxes
            task.executor.feature_set.find_one_feature(template_before, "unrelated-final-template")
            task.ocr(frame=np.full((90, 160, 3), 105, dtype=np.uint8))
        history_records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        history_details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                           for record in history_records if record["state"] == "step_artifact"
                           and record["operation"] == "probe-history"]
        assert len(history_details) == 16, "history must reserve exactly eight complete image/metadata pairs"
        history_metadata = [json.loads((Path(directory) / item["file"]).read_text(encoding="utf-8"))
                            for item in history_details if item["kind"] == "official-recognition-metadata"]
        history_samples = {item["sampling"]: item for item in history_metadata if item["sampling"].startswith("latest-")}
        assert set(history_samples) == {"latest-template-previous", "latest-template-current", "latest-ocr"}
        for sample, pixel in (("latest-template-previous", 102), ("latest-template-current", 103), ("latest-ocr", 105)):
            assert np.all(cv2.imread(str(Path(directory) / history_samples[sample]["screenshotFile"])) == pixel)
        assert len([item for item in history_metadata if item["sampling"] == "first-distinct"]) == 5

        if GAME == "Endfield":
            # Reward probes must survive unrelated templates and the final OCR.
            # These are diagnostic inputs, never invented post-claim verdicts.
            def official_reward_body():
                for pixel in (111, 112):
                    task.executor.feature_set.find_one_feature(
                        np.full((90, 160, 3), pixel, dtype=np.uint8), "reward_ok")
                task.executor.feature_set.find_one_feature(template_before, "unrelated-final-template")
                task.ocr(frame=np.full((90, 160, 3), 113, dtype=np.uint8))
                return original_boxes
            reward_stage = Path(directory) / "reward-stage.jsonl"
            with patch.dict(os.environ, {"YEYU_GAMER_STAGE_FILE": str(reward_stage),
                                         "YEYU_GAMER_SELECTED_OPERATIONS": '["claim-daily-reward"]'}):
                assert observer._observe("claim-daily-reward", official_reward_body, task=task)() is original_boxes
            reward_records = [json.loads(line) for line in reward_stage.read_text(encoding="utf-8").splitlines()]
            reward_details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                              for record in reward_records if record["state"] == "step_artifact"
                              and record["operation"] == "claim-daily-reward"]
            reward_metadata = [json.loads((Path(directory) / item["file"]).read_text(encoding="utf-8"))
                               for item in reward_details if item["kind"] == "official-recognition-metadata"]
            reward_probes = [m for m in reward_metadata if m["template"] == "reward_ok"]
            assert len(reward_probes) == 2, "both actual reward probes must remain available"
            assert {int(cv2.imread(str(Path(directory) / m["screenshotFile"]))[0, 0, 0])
                    for m in reward_probes} == {111, 112}
            assert len(reward_details) <= 8, "reward sampling must stay below the Todo quota"

        if GAME == "NTE":
            # Preserve the final original input of each recognition family;
            # long early polls must not consume later operations' byte budget.
            reporter = observer.StageReporter()
            with observer._recognition_scope(task, reporter, "nte-bounded"):
                for value in range(1, 31):
                    frame = np.full((90, 160, 3), value, dtype=np.uint8)
                    task.ocr(frame=frame)
                    task.executor.feature_set.find_one_feature(frame, "page-anchor")
            nte_records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            nte_details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                           for record in nte_records if record["state"] == "step_artifact"
                           and record["operation"] == "nte-bounded"]
            assert len(nte_details) == 4, "NTE must retain two complete final input/metadata pairs"
            latest = [json.loads((Path(directory) / item["file"]).read_text(encoding="utf-8"))
                      for item in nte_details if item["kind"] == "official-recognition-metadata"][-2:]
            assert {item["recognitionType"] for item in latest} == {"ocr", "template"}
            assert all(item["sampling"].startswith("latest-") for item in latest)
            assert all(np.all(cv2.imread(str(Path(directory) / item["screenshotFile"])) == 30) for item in latest)

            # A failed hunter returns to the world. Its health-bar probe must
            # not erase the two adjacent original inputs used to find treasure.
            with observer._recognition_scope(task, reporter, "spend-urban-vitality"):
                for value in (61, 62, 62):
                    frame = np.full((90, 160, 3), value, dtype=np.uint8)
                    assert task.executor.feature_set.find_one_feature(
                        frame, "boss_treasure") is original_boxes
                    frame[:] = 255
                task.executor.feature_set.find_one_feature(template_before, "health_bar_slash")
                task.ocr(frame=np.full((90, 160, 3), 63, dtype=np.uint8))
            hunter_records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            hunter_details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                              for record in hunter_records if record["state"] == "step_artifact"
                              and record["operation"] == "spend-urban-vitality"]
            assert len(hunter_details) == 6, "hunter diagnostics must remain three complete pairs"
            hunter_samples = {item["sampling"]: item for item in (
                json.loads((Path(directory) / detail["file"]).read_text(encoding="utf-8"))
                for detail in hunter_details if detail["kind"] == "official-recognition-metadata")}
            for sample, pixel in (("latest-template-previous", 61),
                                  ("latest-template-current", 62), ("latest-ocr", 63)):
                assert np.all(cv2.imread(str(Path(directory) / hunter_samples[sample]["screenshotFile"])) == pixel)

        if GAME == "WW":
            for operation, identity, expected in [
                ("inspect-daily-progress", "fixture-account", "game-ui-key-step-verification-raw"),
                ("inspect-daily-progress", None, "game-ui-recognition-raw"),
                ("claim-daily-reward", "fixture-account", "game-ui-recognition-raw"),
            ]:
                reset_recognition_budget(str(stage))
                records_before = len(stage.read_text(encoding="utf-8").splitlines())
                with recognition_scope(task, str(stage), operation, emit, "ww", identity=identity,
                                       verification_kind="game-ui-key-step-verification-raw", max_images=2, first_distinct=0):
                    task.ocr(frame=np.full((90, 160, 3), 79, dtype=np.uint8))
                fresh = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()[records_before:]]
                details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                           for record in fresh if record["state"] == "step_artifact"]
                assert any(detail["kind"] == expected for detail in details)
                assert all(detail["kind"] != "game-ui-reward-verification-raw" for detail in details)

            reset_recognition_budget(str(stage))
            records_before = len(stage.read_text(encoding="utf-8").splitlines())
            recognition_calls_before = len(captures)
            with recognition_scope(task, str(stage), "claim-battle-pass", emit, "ww", identity="fixture-account",
                                   max_images=8, first_distinct=0):
                task.ocr(frame=np.full((90, 160, 3), 89, dtype=np.uint8))
                for page, pixel in (("tasks", 80), *[("rewards", 81 + index) for index in range(8)]):
                    with recognition_scope(task, str(stage), "claim-battle-pass", emit, "ww", identity="fixture-account",
                                           verification_kind="game-ui-reward-verification-raw", verification_page=page,
                                           max_images=8, first_distinct=0):
                        result = task.ocr(frame=np.full((90, 160, 3), pixel, dtype=np.uint8))
                        assert result[0] is original_boxes[0]
            assert len(captures) - recognition_calls_before == 10, "observation added recognizer calls"
            fresh = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()[records_before:]]
            details = [dict(item.split("=", 1) for item in record["detail"].split("; "))
                       for record in fresh if record["state"] == "step_artifact"]
            assert len([d for d in details if d["kind"] == "game-ui-reward-verification-raw"]) == 8
            assert len(details) == 16, "history input and metadata pairs exceeded the artifact budget"
            bp_metadata = [json.loads((Path(directory) / d["file"]).read_text(encoding="utf-8"))
                           for d in details if d["kind"] == "official-recognition-metadata"]
            assert [m["verificationPage"] for m in bp_metadata] == ["tasks"] + ["rewards"] * 7
            for metadata, pixel in zip(bp_metadata, range(80, 88)):
                assert np.all(cv2.imread(str(Path(directory) / metadata["screenshotFile"])) == pixel)
            for identity, page in ((None, "tasks"), ("fixture-account", "unknown-page")):
                reset_recognition_budget(str(stage))
                records_before = len(stage.read_text(encoding="utf-8").splitlines())
                with recognition_scope(task, str(stage), "claim-battle-pass", emit, "ww", identity=identity,
                                       verification_kind="game-ui-reward-verification-raw", verification_page=page,
                                       max_images=2, first_distinct=0):
                    task.ocr(frame=np.full((90, 160, 3), 82, dtype=np.uint8))
                fresh = stage.read_text(encoding="utf-8").splitlines()[records_before:]
                assert not any('kind=game-ui-reward-verification-raw' in line for line in fresh)

        error = RuntimeError("official-engine-error")
        # Different operations share one run byte budget. Reject a whole
        # image/metadata pair while preserving recognizer output and raw files.
        import OfficialStepCapture as capture
        before_count = len([line for line in stage.read_text(encoding="utf-8").splitlines()
                            if '"state": "step_artifact"' in line])
        expected_result = task.ocr(frame=ocr_before)
        with patch.object(capture, "_MAX_RECOGNITION_RUN_BYTES", 1):
            with recognition_scope(task, str(stage), "run-byte-limit", emit, "fixture"):
                self_result = task.ocr(frame=ocr_before)
                assert self_result == expected_result
        after = stage.read_text(encoding="utf-8").splitlines()
        assert len([line for line in after if '"state": "step_artifact"' in line]) == before_count
        assert any("artifact_byte_budget" in line for line in after)
        assert list(Path(directory).glob("fixture-recognition-*.png"))
        reset_recognition_budget(str(stage))
        assert str(stage) not in capture._run_bytes
        task.engine_error = error
        screenshot.generate_screen_shot = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("capture writer unavailable"))
        with recognition_scope(task, str(stage), "failed", emit, "fixture"):
            try:
                task.ocr(frame=ocr_before)
            except RuntimeError as caught:
                assert caught is error
            else:
                raise AssertionError("official exception was swallowed")
        del task.engine_error
        with recognition_scope(task, str(stage), "write-failed", emit, "fixture"):
            assert task.rapid_ocr(None, ocr_before, None, 1, .2, "default") is engine_returns
        assert "step_capture_unavailable:recognition:OSError" in stage.read_text(encoding="utf-8")
        reset_recognition_budget(str(stage))
    print(json.dumps({"passed": True, "game": GAME, "officialPngWriter": True,
                      "recognitionInputPixelsVerified": True, "noCaptureProviderIO": True,
                      "artifactBudgetBounded": True, "originalExceptionPreserved": True, "gameStarted": False}))


if __name__ == "__main__":
    main()

"""Bound official writer replay for Endfield's diagnostic input timeline."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from ok import og
from ok.core.screenshot import Screenshot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adapter-host/openkuro-runner'))
import OfficialStepCapture as capture


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.rows = []
        self.inputs = []
        self.result = []
        outer = self

        class Feature:
            def find_one_feature(self, mat, category_name, threshold=0):
                outer.inputs.append((mat, category_name, threshold))
                if getattr(self, 'error', None):
                    raise self.error
                return outer.result

        capture.install_recognition_observers(feature_type=Feature)
        self.feature = Feature()
        self.task = SimpleNamespace(executor=SimpleNamespace(feature_set=self.feature))
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / 'stage.jsonl')
        self.writer_patch = patch.object(og, 'ok', SimpleNamespace(screenshot=object.__new__(Screenshot)))
        self.config_patch = patch.object(og, 'config', {})
        self.time_patch = patch.object(capture.time, 'monotonic', lambda: self.clock)
        for item in (self.writer_patch, self.config_patch, self.time_patch):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(capture.reset_recognition_budget, self.path)

    def scope(self, **kwargs):
        return capture.recognition_scope(
            self.task, self.path, 'attach-world', lambda *row: self.rows.append(row), 'endfield',
            template_history_names=('esc', 'esc_2k', 'esc_4k'), **kwargs)

    def frame(self, shade):
        return np.full((90, 160, 3), shade, np.uint8)

    def metadata(self):
        return [json.loads(item.read_text(encoding='utf-8'))
                for item in Path(self.folder.name).glob('*recognition-*.json')]

    def test_periodic_inputs_keep_latest_history_and_hard_quota(self):
        with self.scope(max_images=8, first_distinct=5, template_sample_interval_seconds=30):
            for index in range(70):
                self.clock = index * 5
                frame = self.frame(index)
                self.assertIs(self.feature.find_one_feature(frame, 'esc_4k'), self.result)
                self.assertTrue(np.all(frame == index), 'observation changed official input')
            context = capture._scope.get()
            snapshot = capture._input_snapshot(context, None, {'mat': self.frame(254)}, 'template', 'other')
            snapshot['metadata']['recognitionType'] = 'ocr'
            capture._record(context, snapshot, result=[])
        rows = self.metadata()
        self.assertEqual(8, len(rows))
        self.assertEqual(5, sum(row['sampling'] == 'interval-template' for row in rows))
        self.assertEqual({'latest-template-previous', 'latest-template-current', 'latest-ocr'},
                         {row['sampling'] for row in rows if row['sampling'] != 'interval-template'})
        self.assertTrue(all(row['frameSource'] == 'official-template-mat' for row in rows))
        for row in rows:
            image = cv2.imread(str(Path(self.folder.name) / row['screenshotFile']))
            self.assertIsNotNone(image)
            self.assertEqual([90, 160, 3], row['recognizerInputShape'])

    def test_unrelated_templates_do_not_consume_interval_samples(self):
        with self.scope(max_images=8, first_distinct=5, template_sample_interval_seconds=30):
            self.feature.find_one_feature(self.frame(10), 'reward_ok')
            self.assertEqual([], self.metadata())
            self.feature.find_one_feature(self.frame(20), 'esc_4k')
            self.feature.find_one_feature(self.frame(30), 'esc_4k')
            self.clock = 30
            self.feature.find_one_feature(self.frame(40), 'esc_4k')
        self.assertEqual(2, sum(row['sampling'] == 'interval-template' for row in self.metadata()))

    def test_original_exception_survives_writer_failure_and_consumes_attempt(self):
        error = RuntimeError('official matcher failure')
        self.feature.error = error
        with patch.object(Screenshot, 'generate_screen_shot', side_effect=OSError('disk unavailable')):
            with self.scope(max_images=8, first_distinct=5, template_sample_interval_seconds=30):
                for index in range(2):
                    with self.assertRaises(RuntimeError) as raised:
                        self.feature.find_one_feature(self.frame(index), 'esc_4k')
                    self.assertIs(error, raised.exception)
                self.assertEqual(1, capture._scope.get()['budget']['saved'])
        self.assertTrue(any('step_capture_unavailable' in row[-1] for row in self.rows))

    def test_default_sampling_remains_first_distinct(self):
        with self.scope(max_images=5, first_distinct=2):
            self.feature.find_one_feature(self.frame(10), 'reward_ok')
            self.feature.find_one_feature(self.frame(20), 'esc_4k')
        self.assertEqual(2, sum(row['sampling'] == 'first-distinct' for row in self.metadata()))


if __name__ == '__main__':
    unittest.main()

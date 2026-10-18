from __future__ import annotations

import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "upstream_logging.py"


def load_module():
    spec = importlib.util.spec_from_file_location("yeyu_upstream_logging_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class JsonRecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.serialized: list[str] = []
        self.record_keys: list[set[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.record_keys.append(set(record.__dict__))
        self.serialized.append(json.dumps(record.__dict__, ensure_ascii=False))


class UpstreamLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.root = logging.getLogger()
        self.logger = logging.getLogger("ok")
        self.isolated = logging.getLogger("yeyu.tests.nonpropagating")
        self._saved_loggers = {
            logger: (list(logger.handlers), logger.level, logger.propagate)
            for logger in (self.root, self.logger, self.isolated)
        }
        for logger in self._saved_loggers:
            logger.handlers = []
            logger.setLevel(logging.DEBUG)
        self.root.propagate = True
        self.logger.propagate = True
        self.isolated.propagate = False
        self.original = RecordingHandler()
        self.logger.addHandler(self.original)
        self.isolated_original = RecordingHandler()
        self.isolated.addHandler(self.isolated_original)
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        for logger in [logging.getLogger(), *[
            candidate
            for candidate in logging.Logger.manager.loggerDict.values()
            if isinstance(candidate, logging.Logger)
        ]]:
            logger.handlers = [
                handler
                for handler in logger.handlers
                if not getattr(handler, self.module._HANDLER_MARKER, False)
            ]
        for logger, (handlers, level, propagate) in self._saved_loggers.items():
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
        self.temporary.cleanup()

    def test_keeps_existing_settings_and_records_exception(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        level = self.logger.level
        propagate = self.logger.propagate
        formatter = logging.Formatter("UPSTREAM %(message)s")
        self.original.setFormatter(formatter)
        installed = self.module.install(str(path))
        self.assertIsNotNone(installed)
        self.assertIn(self.original, self.logger.handlers)
        self.assertEqual(self.logger.level, level)
        self.assertEqual(self.logger.propagate, propagate)
        self.assertIs(self.original.formatter, formatter)

        self.logger.info("official task %s", "started")
        try:
            raise RuntimeError("upstream sentinel")
        except RuntimeError:
            self.logger.exception("official task failed")

        self.assertEqual([record.getMessage() for record in self.original.records], [
            "official task started", "official task failed",
        ])
        documents = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(documents[0]["logger"], "ok")
        self.assertEqual(documents[0]["level"], "INFO")
        self.assertEqual(documents[0]["message"], "official task started")
        self.assertIn("timestamp", documents[0])
        self.assertEqual(documents[1]["level"], "ERROR")
        self.assertIn("RuntimeError: upstream sentinel", documents[1]["exception"])

    def test_captures_root_ok_and_existing_non_propagating_logger_once(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        self.module.install(str(path))

        self.root.info("root log")
        self.logger.info("ok log")
        self.isolated.info("isolated log")

        documents = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [(document["logger"], document["message"]) for document in documents],
            [
                ("root", "root log"),
                ("ok", "ok log"),
                ("yeyu.tests.nonpropagating", "isolated log"),
            ],
        )
        self.assertEqual([record.getMessage() for record in self.original.records], ["ok log"])
        self.assertEqual(
            [record.getMessage() for record in self.isolated_original.records],
            ["isolated log"],
        )

    def test_deduplication_does_not_mutate_upstream_log_record(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        json_handler = JsonRecordingHandler()
        self.logger.addHandler(json_handler)
        self.module.install(str(path))

        self.logger.info("serializable official record")

        self.assertEqual(len(json_handler.serialized), 1)
        serialized_record = json.loads(json_handler.serialized[0])
        self.assertNotIn("_yeyu_gamer_upstream_logging_paths_v1", serialized_record)
        self.assertNotIn("_yeyu_gamer_upstream_logging", " ".join(serialized_record))
        self.assertEqual(set(self.original.records[0].__dict__), json_handler.record_keys[0])
        documents = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(documents), 1)

    def test_unwritable_destination_does_not_change_upstream_return_path(self) -> None:
        blocked = Path(self.temporary.name) / "not-a-directory"
        blocked.write_text("x", encoding="utf-8")
        self.assertIsNotNone(self.module.install(str(blocked / "upstream.jsonl")))
        self.logger.warning("official warning survives observer failure")
        self.assertEqual(len(self.original.records), 1)
        self.assertEqual(self.original.records[0].getMessage(), "official warning survives observer failure")

    def test_same_destination_is_idempotent(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        first = self.module.install(str(path))
        second = self.module.install(str(path))
        self.assertIs(first, second)
        self.assertEqual(sum(handler is first for handler in self.logger.handlers), 1)

    def test_reinstall_after_upstream_replaces_ok_handlers(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        first = self.module.install(str(path))
        replacement = RecordingHandler()
        self.logger.handlers = [replacement]

        second = self.module.install(str(path))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        self.assertIn(replacement, self.logger.handlers)
        self.logger.info("after upstream config reset")

        documents = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["message"], "after upstream config reset")
        self.assertEqual([record.getMessage() for record in replacement.records], [
            "after upstream config reset",
        ])

    def test_loguru_sink_is_not_added_twice(self) -> None:
        class FakeLoguruLogger:
            def __init__(self) -> None:
                self.calls: list[tuple[object, int, bool]] = []

            def add(self, sink, *, level, catch):
                self.calls.append((sink, level, catch))
                return len(self.calls)

        class FakeLoguruModule:
            logger = FakeLoguruLogger()

        original_loguru = sys.modules.get("loguru")
        sys.modules["loguru"] = FakeLoguruModule()
        try:
            path = Path(self.temporary.name) / "upstream.jsonl"
            self.module.install(str(path))
            self.module.install(str(path))
            self.assertEqual(len(FakeLoguruModule.logger.calls), 1)
        finally:
            if original_loguru is None:
                del sys.modules["loguru"]
            else:
                sys.modules["loguru"] = original_loguru

    def test_long_upstream_message_is_not_truncated(self) -> None:
        path = Path(self.temporary.name) / "upstream.jsonl"
        self.module.install(str(path))
        message = "complete upstream output " * 10000
        self.logger.info(message)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["message"], message)


if __name__ == "__main__":
    unittest.main()

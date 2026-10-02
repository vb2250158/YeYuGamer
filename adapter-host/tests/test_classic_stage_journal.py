"""Real concurrent Windows file appends; no game or official tool is started."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
JOURNAL = ROOT / "adapter-host/classic-runner/StageJournal.py"
DRIVER = ROOT / "adapter-host/classic-runner/classic_tool_driver.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StageJournalTests(unittest.TestCase):
    def test_concurrent_driver_writes_preserve_every_utf8_record(self):
        driver = load(DRIVER, "concurrent_stage_driver")
        with tempfile.TemporaryDirectory(prefix="yeyu-stage-journal-") as raw:
            stage = Path(raw) / "stage.jsonl"
            def write(worker):
                for sequence in range(300):
                    driver._emit(stage, "observe", "recognition_frame", json.dumps({
                        "worker": worker, "sequence": sequence, "text": "日" * 400}, ensure_ascii=False))
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(8)))
            records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            values = [json.loads(record["detail"]) for record in records]
            self.assertEqual(2400, len(records))
            self.assertEqual(2400, len({(v["worker"], v["sequence"]) for v in values}))
            self.assertTrue(all(v["text"] == "日" * 400 for v in values))

    def test_separate_processes_preserve_large_records_and_existing_content(self):
        with tempfile.TemporaryDirectory(prefix="yeyu-stage-processes-") as raw:
            root = Path(raw)
            stage = root / "stage.jsonl"
            stage.write_text('{"existing":true}\n', encoding="utf-8")
            code = (
                "import importlib.util,sys;"
                "s=importlib.util.spec_from_file_location('journal',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "[m.append_record(sys.argv[2],{'worker':int(sys.argv[3]),'sequence':i,'text':'日'*4000}) for i in range(200)]"
            )
            processes = []
            streams = []
            records = []
            try:
                for worker in range(4):
                    log = root / ("worker-%s.log" % worker)
                    stream = log.open("wb")
                    streams.append(stream)
                    process = subprocess.Popen([sys.executable, "-c", code, str(JOURNAL), str(stage), str(worker)],
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    processes.append(process)
                    records.append({"purpose": "Stage journal concurrency unit test", "task": "test_classic_stage_journal",
                                    "source": str(__file__), "pid": process.pid, "parentPid": os.getpid(), "log": str(log)})
                (root / "managed-test-processes.json").write_text(json.dumps(records), encoding="utf-8")
                for process in processes:
                    self.assertEqual(0, process.wait(timeout=30))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
                for stream in streams:
                    stream.close()
            values = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({"existing": True}, values[0])
            self.assertEqual(801, len(values))
            self.assertEqual(800, len({(v["worker"], v["sequence"]) for v in values[1:]}))
            self.assertTrue(all(v["text"] == "日" * 4000 for v in values[1:]))

    def test_unavailable_parent_reports_io_error(self):
        journal = load(JOURNAL, "journal_io_failure")
        with tempfile.TemporaryDirectory(prefix="yeyu-stage-io-") as raw:
            with self.assertRaises(OSError):
                journal.append_record(Path(raw) / "missing" / "stage.jsonl", {"event": "observed"})


if __name__ == "__main__":
    unittest.main()

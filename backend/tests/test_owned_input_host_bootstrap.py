import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import threading
import unittest
import uuid
from unittest import mock

from yeyu_gamer_manager.services.owned_input_execution import OwnedInputExecution
from yeyu_gamer_manager.services import owned_input_qualification as policy


class OwnedInputHostBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.work = cls.root / ".cache" / ("owned-host-bootstrap-" + uuid.uuid4().hex)
        cls.work.mkdir(parents=True)
        cls.addClassCleanup(shutil.rmtree, cls.work)
        cls.host = cls.work / "host.exe"
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe",
            "/reference:System.Web.Extensions.dll", "/out:" + str(cls.host),
            str(cls.root / "adapter-host/YeYuGamerAdapterHost.cs")], capture_output=True,
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError("Offline fixed Host compilation failed: " + result.stdout.decode(errors="replace"))

    def start(self, job_name):
        environment = dict(os.environ)
        environment["YEYU_GAMER_OWNED_INPUT_JOB"] = job_name
        process = subprocess.Popen([str(self.host), "--operation", "execute", "--protocol-version", "1.1",
            "--run-id", str(uuid.uuid4()), "--game-id", "StarRail"], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({"owner": self.id(), "purpose": "compiled-Host-pre-request-bootstrap",
            "pid": process.pid, "parentPid": os.getpid(), "source": str(self.host)}))
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    def test_compiled_host_joins_before_receiving_any_plan(self):
        execution = OwnedInputExecution({"runId": str(uuid.uuid4()), "kind": "offline-bootstrap"})
        process = self.start(execution.job_name)
        try:
            execution.bind(process, 2)
            self.assertIsNone(process.poll())
            receipt = execution.stop_and_drain(2)
            self.assertTrue(execution.verify_receipt(receipt, {"runId": execution.scope["runId"],
                                                              "kind": "offline-bootstrap"}))
            self.assertIsNotNone(process.poll())
        finally:
            self.cleanup_process(process)
            execution.stop_and_drain(2)
            execution.close()

    def test_invalid_job_name_rejected_without_request(self):
        process = self.start("not-an-owned-job")
        output, _ = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 70)
        self.assertEqual(json.loads(output)["code"], "host_error")

    def test_auxiliary_writer_must_drain_before_native_receipt(self):
        execution = OwnedInputExecution({"runId": str(uuid.uuid4()), "kind": "auxiliary-writer"})
        process = self.start(execution.job_name)
        release = threading.Event()
        writer = threading.Thread(target=lambda: release.wait(3), daemon=True)
        try:
            execution.bind(process, 2)
            execution.start_writer(writer)
            self.assertIsNone(execution.stop_and_drain(0.05))
            self.assertIsNotNone(process.poll())
            self.assertFalse(execution.verify_receipt(None, execution.scope))
            release.set()
            writer.join(1)
            receipt = execution.stop_and_drain(2)
            self.assertTrue(execution.verify_receipt(receipt, execution.scope))
        finally:
            release.set()
            writer.join(1)
            self.cleanup_process(process)
            execution.stop_and_drain(2)
            execution.close()

    def test_unregistered_native_job_rejected_without_request(self):
        process = self.start("Local\\YeYuOwnedHost-" + uuid.uuid4().hex)
        output, _ = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 70)
        self.assertEqual(json.loads(output)["code"], "host_error")

    def test_unqualified_default_does_not_enable_containment_for_games(self):
        self.assertFalse(policy.owned_input_qualified("WW", "sha256:" + "1" * 64, "sha256:" + "2" * 64))

    def test_qualification_requires_exact_game_package_and_host(self):
        identity = ("offline", "sha256:" + "1" * 64, "sha256:" + "2" * 64)
        with mock.patch.object(policy, "_qualifications", return_value=frozenset({identity})):
            self.assertTrue(policy.owned_input_qualified(*identity))
            for changed in (("WW", *identity[1:]), (identity[0], "sha256:" + "3" * 64, identity[2]),
                            (*identity[:2], "sha256:" + "3" * 64)):
                self.assertFalse(policy.owned_input_qualified(*changed))

    def test_qualification_policy_rejects_boolean_version_and_zero_digest(self):
        for document in ({"schemaVersion": True, "qualifications": []},
                         {"schemaVersion": 1, "qualifications": [{"gameId": "offline",
                          "packageDigest": "sha256:" + "0" * 64, "hostSha256": "sha256:" + "2" * 64}]}):
            policy._qualifications.cache_clear()
            try:
                with mock.patch.object(policy.Path, "read_text", return_value=json.dumps(document)):
                    with self.assertRaises(ValueError):
                        policy._qualifications()
            finally:
                policy._qualifications.cache_clear()

    def test_qualification_policy_is_in_wheel_package_data(self):
        project = tomllib.loads((self.root / "backend/pyproject.toml").read_text(encoding="utf-8"))
        declared = project["tool"]["setuptools"]["package-data"]["yeyu_gamer_manager"]
        self.assertIn("services/resources/owned_input_qualifications.json", declared)

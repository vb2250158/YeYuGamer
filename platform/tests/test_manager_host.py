from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yeyu_gamer_platform.manager_host import (
    _publish_owned_pid_record,
    main,
    run_manager,
)


class ManagerHostTests(unittest.TestCase):
    def test_restarts_only_after_explicit_restart_exit(self) -> None:
        children = [Mock(), Mock()]
        children[0].wait.return_value = 75
        children[1].wait.return_value = 0
        factory = Mock(side_effect=children)
        injected = {
            "PYTHONPATH": "attacker-path",
            "PYTHONHOME": "attacker-home",
            "PYTHONSTARTUP": "attacker-startup",
            "PYTHONUSERBASE": "attacker-userbase",
            "PYTHONINSPECT": "1",
        }
        ephemeral_secret = (
            "ephemeral-tray-bootstrap-secret-0123456789abcdefghijkl"
        )
        with patch.dict(
            os.environ,
            {
                **injected,
                "YEYU_GAMER_TRAY_BOOTSTRAP_SECRET": ephemeral_secret,
            },
        ):
            result = run_manager(
                ("python", "-m", "manager"),
                popen_factory=factory,
                monotonic=Mock(return_value=1.0),
                sleep=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_count, 2)
        for call in factory.call_args_list:
            self.assertEqual(call.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(
                call.kwargs["env"]["YEYU_GAMER_TRAY_BOOTSTRAP_SECRET"],
                ephemeral_secret,
            )
            self.assertNotIn(ephemeral_secret, " ".join(call.args[0]))
            for variable in injected:
                self.assertNotIn(variable, call.kwargs["env"])

    def test_main_uses_only_the_fixed_manager_module(self) -> None:
        with patch(
            "yeyu_gamer_platform.manager_host.run_manager", return_value=0
        ) as runner:
            result = main([])
        self.assertEqual(result, 0)
        command = runner.call_args.args[0]
        self.assertEqual(
            command[1:],
            ("-I", "-B", "-m", "yeyu_gamer_manager"),
        )

    def test_does_not_restart_an_ordinary_failure(self) -> None:
        child = Mock()
        child.wait.return_value = 1
        factory = Mock(return_value=child)
        result = run_manager(("python", "-m", "manager"), popen_factory=factory)
        self.assertEqual(result, 1)
        factory.assert_called_once()

    def test_breaks_restart_storm(self) -> None:
        child = Mock()
        child.wait.return_value = 75
        factory = Mock(return_value=child)
        result = run_manager(
            ("python", "-m", "manager"),
            maximum_restarts=2,
            popen_factory=factory,
            monotonic=Mock(return_value=1.0),
            sleep=Mock(),
        )
        self.assertEqual(result, 76)
        self.assertEqual(factory.call_count, 3)

    def test_child_import_does_not_create_bytecode_in_application_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application_root = Path(temporary)
            (application_root / "runtime_probe.py").write_text(
                "VALUE = 'loaded'\n", encoding="utf-8"
            )
            import_statement = (
                f"import sys; sys.path.insert(0, {str(application_root)!r}); "
                "import runtime_probe; assert runtime_probe.VALUE == 'loaded'"
            )
            result = run_manager((sys.executable, "-c", import_statement))
            self.assertEqual(result, 0)
            self.assertFalse((application_root / "__pycache__").exists())

    def test_main_publishes_then_cleans_only_its_owned_pid_record(self) -> None:
        context = (
            Path("manager-process.json"),
            "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
            1234,
            "windows-filetime:123",
        )
        with (
            patch(
                "yeyu_gamer_platform.manager_host._pid_record_context_from_environment",
                return_value=context,
            ),
            patch(
                "yeyu_gamer_platform.manager_host._publish_owned_pid_record"
            ) as publish,
            patch(
                "yeyu_gamer_platform.manager_host.run_manager",
                return_value=1,
            ),
            patch(
                "yeyu_gamer_platform.manager_host.remove_manager_pid_record_if_owned"
            ) as cleanup,
        ):
            result = main([])
        self.assertEqual(result, 1)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args, (context,))
        self.assertEqual(
            publish.call_args.kwargs["manager_command"][1:],
            ("-I", "-B", "-m", "yeyu_gamer_manager"),
        )
        cleanup.assert_called_once_with(
            context[0],
            instance_id=context[1],
            pid=context[2],
            process_creation_identity=context[3],
        )

    def test_host_publishes_its_own_pid_and_identity(self) -> None:
        context = (
            Path("manager-process.json"),
            "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
            4321,
            "windows-filetime:4321",
        )
        with patch(
            "yeyu_gamer_platform.manager_host.publish_manager_pid_record"
        ) as publish:
            _publish_owned_pid_record(
                context,
                manager_command=("pythonw", "-I", "-B", "-m", "manager"),
            )
        path, record = publish.call_args.args
        self.assertEqual(path, context[0])
        self.assertEqual(record["instanceId"], context[1])
        self.assertEqual(record["pid"], context[2])
        self.assertEqual(record["processCreationIdentity"], context[3])
        self.assertEqual(record["managerExecutable"], "pythonw")
        self.assertEqual(record["managerArgumentCount"], 4)

    def test_failed_publication_never_cleans_another_instances_record(self) -> None:
        context = (
            Path("manager-process.json"),
            "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
            4321,
            "windows-filetime:4321",
        )
        with (
            patch(
                "yeyu_gamer_platform.manager_host._pid_record_context_from_environment",
                return_value=context,
            ),
            patch(
                "yeyu_gamer_platform.manager_host._publish_owned_pid_record",
                side_effect=RuntimeError("another live Manager host owns the PID record"),
            ),
            patch(
                "yeyu_gamer_platform.manager_host.remove_manager_pid_record_if_owned"
            ) as cleanup,
        ):
            self.assertEqual(main([]), 3)
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()

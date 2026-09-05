"""Machine-check Adapter distribution, promotion, and operation bindings.

This test is intentionally static: it reads source and manifests/contracts, but
does not launch Manager, an Adapter candidate, an automation tool, or a game.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "adapter-host" / "operation-contracts.json"
PROMOTION_FIELDS = {
    "status",
    "replaySuiteDigest",
    "shadowSuiteDigest",
    "canarySuiteDigest",
    "payloadDigest",
    "receiptFile",
    "receiptSha256",
    "receiptResourceId",
}
EVIDENCE_FIELDS = {
    "schemaVersion",
    "resourceType",
    "status",
    "passed",
    "packageId",
    "packageVersion",
    "buildId",
    "supportedGameIds",
    "payloadDigest",
    "replaySuiteDigest",
    "shadowSuiteDigest",
    "generatedAt",
    "gameStarted",
}


def text(relative: str) -> str:
    path = ROOT / relative
    assert path.is_file(), f"missing distribution source: {relative}"
    return path.read_text(encoding="utf-8")


def operation_keys(build_text: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z0-9_])'([a-z][a-z0-9-]+)'\s*=", build_text))


def _constant_string(node: ast.AST, context: str) -> str:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str), context
    return node.value


def _keyword(call: ast.Call, name: str) -> ast.AST:
    value = next((item.value for item in call.keywords if item.arg == name), None)
    assert value is not None, f"missing keyword {name}"
    return value


def integration_operations() -> dict[str, set[str]]:
    source = text("backend/yeyu_gamer_manager/services/integration_catalog.py")
    tree = ast.parse(source)
    result: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "GameIntegrationRegistration":
            continue
        game_id = _constant_string(_keyword(node, "game_id"), "integration game_id must be literal")
        operations_node = _keyword(node, "daily_operations")
        assert isinstance(operations_node, (ast.Tuple, ast.List)), (
            f"{game_id}: daily_operations must be a literal sequence"
        )
        operations = {
            _constant_string(item, f"{game_id}: integration operation must be literal")
            for item in operations_node.elts
        }
        assert game_id not in result and len(operations) == len(operations_node.elts)
        result[game_id] = operations
    return result


def executable_todo_operations() -> dict[str, set[str]]:
    source = text("backend/yeyu_gamer_manager/services/todo_catalog.py")
    tree = ast.parse(source)
    result: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_daily" or len(node.args) < 2:
            continue
        automation_state = next(
            (item.value for item in node.keywords if item.arg == "automation_state"),
            None,
        )
        if not isinstance(automation_state, ast.Name) or automation_state.id != "TOOL":
            continue
        capability = next(
            (item.value for item in node.keywords if item.arg == "capability"),
            None,
        )
        if isinstance(capability, ast.Constant) and capability.value is None:
            # A source tool may expose an operation that policy deliberately
            # leaves unbound.  Only Todos carrying the default daily
            # capability belong in the executable Adapter contract.
            continue
        game_id = _constant_string(node.args[0], "Todo game_id must be literal")
        operation = _constant_string(node.args[1], f"{game_id}: Todo operation must be literal")
        assert operation not in result[game_id], f"{game_id}: duplicate tool Todo {operation}"
        result[game_id].add(operation)
    return dict(result)


def main() -> None:
    raw = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"schemaVersion", "contracts"} and raw["schemaVersion"] == 1
    contracts = raw["contracts"]
    assert set(contracts) == {
        "PGR", "ZZZ", "NIKKE", "WW", "Endfield", "GF2",
        "NTE", "FGO", "StarRail", "CZN", "BD2",
    }

    integration_by_game = integration_operations()
    todo_by_game = executable_todo_operations()
    assert set(integration_by_game) == set(contracts), (
        "integration/contract game drift; "
        f"missing={sorted(set(contracts) - set(integration_by_game))}, "
        f"extra={sorted(set(integration_by_game) - set(contracts))}"
    )
    assert set(todo_by_game) == set(contracts), (
        "tool Todo/contract game drift; "
        f"missing={sorted(set(contracts) - set(todo_by_game))}, "
        f"extra={sorted(set(todo_by_game) - set(contracts))}"
    )

    builds: dict[str, set[str]] = defaultdict(set)
    for game_id, contract in contracts.items():
        assert set(contract) == {"runner", "build", "installer", "operations"}
        operations = contract["operations"]
        assert operations and len(operations) == len(set(operations))
        operation_set = set(operations)
        assert integration_by_game[game_id] == operation_set, (
            f"{game_id}: integration/contract operation drift; "
            f"missing={sorted(operation_set - integration_by_game[game_id])}, "
            f"extra={sorted(integration_by_game[game_id] - operation_set)}"
        )
        assert todo_by_game[game_id] == operation_set, (
            f"{game_id}: tool Todo/contract operation drift; "
            f"missing={sorted(operation_set - todo_by_game[game_id])}, "
            f"extra={sorted(todo_by_game[game_id] - operation_set)}"
        )
        builds[contract["build"]].update(operations)
        runner_source = text(contract["runner"])
        installer_source = text(contract["installer"])
        for operation in operations:
            literal = f'"{operation}"'
            quoted = f"'{operation}'"
            assert literal in runner_source or quoted in runner_source, (
                f"{game_id}: runner missing {operation}"
            )
            assert literal in installer_source or quoted in installer_source, (
                f"{game_id}: installer validation missing {operation}"
            )

    for build_path, expected in builds.items():
        build_source = text(build_path)
        actual = operation_keys(build_source)
        assert actual == expected, (
            f"{build_path}: operation binding drift; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
        for field in PROMOTION_FIELDS:
            assert re.search(rf"\b{re.escape(field)}\s*=", build_source), (
                f"{build_path}: candidate promotion field missing: {field}"
            )
        assert "Get-YeYuGamerPayloadDigest" in build_source

    installers = sorted({contract["installer"] for contract in contracts.values()})
    discovered_installers = sorted(
        f"scripts/{path.name}"
        for path in (ROOT / "scripts").glob("Install-YeYuGamer*Adapter*.ps1")
    )
    assert installers == discovered_installers, (
        "operation contract must enumerate every Adapter installer; "
        f"missing={sorted(set(discovered_installers) - set(installers))}, "
        f"extra={sorted(set(installers) - set(discovered_installers))}"
    )
    for installer_path in installers:
        installer = text(installer_path)
        assert "installed-unpromoted" in installer
        assert "promotionOwner" in installer
        assert "CandidateTestEvidence" in installer
        assert not re.search(r"promotion\.status\s*=\s*['\"]promoted['\"]", installer)
        assert not re.search(r"executionReady\s*=\s*\$true", installer)
        assert not re.search(r"sha256:\s*['\"]?\s*\+?\s*\(?['\"]0['\"]\s*\*\s*64", installer)

    retired_local_modules = text("scripts/Install-YeYuGamerLocalDailyModules.ps1")
    assert "is retired and cannot install or promote adapters" in retired_local_modules
    assert "Publish-YeYuGamerLocalRelease.ps1" in retired_local_modules
    assert "Copy-Item" not in retired_local_modules and "Move-Item" not in retired_local_modules
    assert not re.search(r"promotion\.status\s*=\s*['\"]promoted['\"]", retired_local_modules)
    assert not re.search(r"executionReady\s*=\s*\$true", retired_local_modules)

    runners = sorted({contract["runner"] for contract in contracts.values()})
    for runner_path in runners:
        runner = text(runner_path)
        assert "terminalEventDigest" in runner
        assert not re.search(r"new\s+string\s*\(\s*['\"]0['\"]\s*,\s*64\s*\)", runner)
        assert not re.search(r"return\s+['\"]sha256:['\"]\s*\+\s*new\s+string\s*\(\s*['\"]0", runner)

    tests = [
        "scripts/Test-YeYuGamerClassicAdapter.ps1",
        "scripts/Test-YeYuGamerWwAdapter.ps1",
        "scripts/Test-YeYuGamerNteAdapter.ps1",
        "scripts/Test-YeYuGamerFgoAdapter.ps1",
        "scripts/Test-YeYuGamerStarRailAdapter.ps1",
        "scripts/Test-YeYuGamerCznAdapter.ps1",
        "scripts/Test-YeYuGamerBd2Adapter.ps1",
    ]
    for test_path in tests:
        test_source = text(test_path)
        for field in EVIDENCE_FIELDS:
            assert re.search(rf"\b{re.escape(field)}\s*=", test_source), (
                f"{test_path}: candidate evidence field missing: {field}"
            )
        assert "adapter-candidate-test-evidence" in test_source

    host = text("adapter-host/YeYuGamerAdapterHost.cs")
    assert "ValidatePromotionReceipt" in host
    assert "GetDurableCancelPaths" in host and "ValidateDurableCancel" in host
    assert "EnsureProtectedControlDirectory" in host
    assert "EnsureNoReparseDirectoryPath(common, root)" in host
    assert "SetAccessRuleProtection(true, false)" in host
    assert "runner_deadline_exceeded" in host and "runner_exited_without_terminal" in host
    assert "ComputeTerminalDigest(request, status" in host
    assert "--completed--" in host and "--unresolved--" in host
    openkuro = text("adapter-host/openkuro-runner/Program.cs")
    assert "launcher_download_required" in openkuro
    assert "client_update_required" in openkuro
    assert "telemetry_missing" in openkuro
    assert "WaitForEndfieldClientTransport" not in openkuro
    assert "endfieldGameWindow=ready" not in openkuro
    nte = text("adapter-host/nte-runner/Program.cs")
    assert "splash_not_ready" in nte and "visible && interactive && ready" in nte
    fgo = text("adapter-host/fgo-runner/Program.cs")
    starrail_protocol = text("adapter-host/starrail-runner/Protocol.cs")
    starrail_runner = text("adapter-host/starrail-runner/Program.cs")
    assert "events.TerminalDigest(" in fgo and "events.Digest()" not in fgo
    assert "events.TerminalDigest(" in starrail_runner and "events.Digest()" not in starrail_runner
    assert "--completed--" in fgo and "--unresolved--" in fgo
    assert "--completed--" in starrail_protocol and "--unresolved--" in starrail_protocol

    release = text("scripts/Publish-YeYuGamerLocalRelease.ps1")
    assert "Copy-YeYuGamerReleaseSource" in release
    assert "validate_adapter_distribution_contracts.py" in release
    assert "promotion-requests" in release and "Invoke-ManagerPromotion" in release
    assert "-SkipBuild" in release and "-NoOpenWebGui" in release
    assert "dailyBatchStarted = $false" in release and "gameStarted = $false" in release
    assert "batch.daily.run" not in release and "/batches" not in release
    for build_path in builds:
        assert Path(build_path).name in release, f"release omits build: {build_path}"
    for installer_path in installers:
        assert Path(installer_path).name in release, (
            f"release omits installer: {installer_path}"
        )
    for test_path in tests:
        assert Path(test_path).name in release, f"release omits test: {test_path}"

    print(json.dumps({
        "status": "passed",
        "games": len(contracts),
        "builds": len(builds),
        "installers": len(installers),
        "operationSourcesChecked": ["contract", "build", "installer", "runner", "integration", "todo"],
        "unifiedRelease": True,
    }))


if __name__ == "__main__":
    main()

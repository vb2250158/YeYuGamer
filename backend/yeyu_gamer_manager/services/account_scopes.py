"""Manager-owned account identities and immutable daily target scopes."""

from __future__ import annotations

from typing import Any, Callable
from copy import deepcopy
import uuid

from ..domain.models import OKWWProfileConfig
from .manager_errors import ManagerValidation


DEFAULT_ACCOUNT_ID = "default"


def game_accounts(config_values: dict[str, Any], game_id: str) -> list[dict[str, Any]]:
    configured = config_values.get("game_accounts", {}).get(game_id)
    if configured is None:
        return [{"account_id": DEFAULT_ACCOUNT_ID, "label": "当前账号",
                 "enabled": True, "saved_account_label": ""}]
    return deepcopy(configured)


def initialize_ww_account_configs(config_values: dict[str, Any],
                                 required_definition_ids: list[str]) -> dict[str, Any]:
    """Copy legacy defaults once; persisted account values never follow shared edits."""
    result = deepcopy(config_values.get("game_accounts", {}))
    selection = config_values.get("daily_todo_selection", {}).get("WW")
    if not isinstance(selection, list):
        selection = required_definition_ids
    raw_profiles = config_values.get("daily_tool_profiles", {})
    raw_profile = raw_profiles.get("ok_ww") if isinstance(raw_profiles, dict) else None
    raw_profile = raw_profile if isinstance(raw_profile, dict) else {}
    profile = OKWWProfileConfig().model_dump()
    profile.update({key: deepcopy(value) for key, value in raw_profile.items()
                    if key in OKWWProfileConfig.model_fields})
    accounts = game_accounts(config_values, "WW")
    for account in accounts:
        if account.get("daily_todo_selection") is None:
            account["daily_todo_selection"] = deepcopy(selection)
        if account.get("daily_tool_profiles") is None:
            account["daily_tool_profiles"] = {"ok_ww": deepcopy(profile)}
    result["WW"] = accounts
    return result


def freeze_account_snapshot(account: dict[str, Any]) -> dict[str, Any]:
    snapshot = {"label": str(account["label"]),
                "saved_account_label": str(account.get("saved_account_label") or "")}
    for key in ("daily_todo_selection", "daily_tool_profiles"):
        if account.get(key) is not None:
            snapshot[key] = deepcopy(account[key])
    return snapshot


def frozen_run_tool_profiles(run: dict[str, Any], owning_batch: dict[str, Any] | None,
                             current_profiles: dict[str, Any]) -> dict[str, Any]:
    """A WW recovery may use only a durable Run or original batch snapshot."""
    if run["game_id"] == "WW":
        profiles = run.get("account_snapshot", {}).get("daily_tool_profiles")
        if isinstance(profiles, dict):
            return deepcopy(profiles)
        if owning_batch is None or "dailyToolProfiles" not in owning_batch["result"]:
            raise ManagerValidation("此旧鸣潮 Run 没有冻结的每日参数，不能用当前设置恢复；请保留历史记录并创建新计划。")
    if owning_batch is not None and "dailyToolProfiles" in owning_batch["result"]:
        return deepcopy(owning_batch["result"]["dailyToolProfiles"])
    return deepcopy(current_profiles)


def enabled_account_ids(config_values: dict[str, Any], game_id: str) -> list[str]:
    return [str(item["account_id"]) for item in game_accounts(config_values, game_id)
            if item.get("enabled", True)]


def resolve_game_account(config_values: dict[str, Any], game_id: str,
                         account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
    for account in game_accounts(config_values, game_id):
        if account["account_id"] == account_id:
            return account
    raise ManagerValidation("accountId does not belong to this game")


def account_target_id(game_id: str, account_id: str = DEFAULT_ACCOUNT_ID) -> str:
    # The default key preserves historical frozen scopes. This is a target key,
    # never a GameId, Adapter registration, or process-cleanup authorization.
    return game_id if account_id == DEFAULT_ACCOUNT_ID else f"{game_id}::{account_id}"


def run_target_id(run: dict[str, Any]) -> str:
    return account_target_id(str(run.get("game_id", run.get("gameId"))),
                             str(run.get("account_id", run.get("accountId", DEFAULT_ACCOUNT_ID))))


def daily_account_targets(config_values: dict[str, Any], game_ids: list[str]) -> list[dict[str, Any]]:
    targets = []
    for game_id in game_ids:
        for account in game_accounts(config_values, game_id):
            if not account.get("enabled", True):
                continue
            if game_id == "WW" and account.get("daily_todo_selection") == []:
                continue
            account_id = str(account["account_id"])
            targets.append({"targetId": account_target_id(game_id, account_id),
                            "gameId": game_id, "accountId": account_id,
                            "accountLabel": str(account["label"]),
                            "accountSnapshot": freeze_account_snapshot(account)})
    return targets


def validate_account_patch(current: dict[str, Any], patch: dict[str, Any], *,
                           has_execution: Callable[[str, str], bool] | None = None,
                           required_definition_ids: list[str] | None = None) -> dict[str, Any]:
    """Generate new identity once inside the idempotent config transaction."""
    if set(patch) - {"WW"}:
        raise ManagerValidation("Multi-account configuration is currently supported only for WW")
    result = dict(current.get("game_accounts", {}))
    for game_id, inputs in patch.items():
        if len(inputs) > 20:
            raise ManagerValidation("At most 20 account profiles are supported per game")
        existing = {item["account_id"]: item for item in game_accounts(current, game_id)}
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for supplied in inputs:
            item = dict(supplied)
            account_id = item.get("account_id")
            if account_id is None:
                account_id = str(uuid.uuid4())
            elif account_id not in existing:
                raise ManagerValidation("Unknown accountId; omit accountId to register a new account")
            if account_id in seen:
                raise ManagerValidation("gameAccounts contains duplicate accountId")
            seen.add(account_id)
            item["account_id"] = account_id
            item["label"] = str(item["label"]).strip()
            item["saved_account_label"] = str(item.get("saved_account_label") or "").strip()
            if not item["label"]:
                raise ManagerValidation("Account label cannot be blank")
            if any(ord(char) < 32 or ord(char) == 127 for char in item["label"] + item["saved_account_label"]):
                raise ManagerValidation("Account labels cannot contain control characters")
            if any(char in item["saved_account_label"] for char in ("/", "\\")):
                raise ManagerValidation("Saved account labels cannot contain path separators")
            if item["saved_account_label"] and "****" not in item["saved_account_label"]:
                raise ManagerValidation("请填写鸣潮登录页已记住账号显示的掩码标签（含 ****），不要填写密码或完整手机号。")
            prior = existing.get(account_id)
            for key in ("daily_todo_selection", "daily_tool_profiles"):
                if item.get(key) is None and prior is not None and prior.get(key) is not None:
                    item[key] = deepcopy(prior[key])
            if (prior is not None and item["saved_account_label"] != (prior.get("saved_account_label") or "")
                    and has_execution is not None and has_execution(game_id, account_id)):
                raise ManagerValidation("已有执行记录的账号不能更换登录标签；请停用旧项并新增账号，以保留各账号独立的每日记录。")
            accounts.append(item)
        if set(existing) - seen:
            raise ManagerValidation("Existing accounts must be retained; disable them instead of deleting history")
        selectors = [item["saved_account_label"] for item in accounts if item.get("enabled", True) and item["saved_account_label"]]
        if len(selectors) != len(set(selectors)):
            raise ManagerValidation("Enabled accounts must have distinct saved login labels")
        result[game_id] = accounts
    return initialize_ww_account_configs({**current, "game_accounts": result}, required_definition_ids or [])

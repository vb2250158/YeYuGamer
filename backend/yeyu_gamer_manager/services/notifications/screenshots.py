"""Presentation semantics for daily reward captures, not completion decisions."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

REWARD_KINDS = (
    "game-ui-claimed-reward", "game-ui-daily-reward-watermarked",
    "game-ui-daily-reward-raw", "game-ui-reward-screen", "reward-screenshot",
)

# Exact daily reward stages from the integration Todo catalog. Mail and free
# battle-pass rewards are not substitutes for the daily reward capture.
DAILY_REWARD_OPERATIONS = {
    "StarRail": frozenset({"claim-daily-training-rewards"}),
    "ZZZ": frozenset({"engagement-reward"}),
    "Endfield": frozenset({"claim-daily-reward"}),
    "GF2": frozenset({"claim-daily-missions"}),
    "NTE": frozenset({"claim-activity-reward", "claim-cycle-reward"}),
    "PGR": frozenset({"claim-daily-tasks"}),
    "WW": frozenset({"claim-daily-reward"}),
}


def is_reward_capture(frame: Mapping[str, Any], game_id: str,
                      instances: Sequence[Mapping[str, Any]]) -> bool:
    if frame.get("kind") in REWARD_KINDS:
        return True
    if (frame.get("kind") != "game-ui-step-after-watermarked"
            or frame.get("operation") not in DAILY_REWARD_OPERATIONS.get(game_id, ())):
        return False
    return any(
        item.get("todoInstanceId") == frame.get("todoInstanceId")
        and bool(item.get("todoInstanceId"))
        and item.get("operation") == frame.get("operation")
        and item.get("status") == "completed"
        and frame.get("artifactId") in item.get("evidenceRefs", [])
        for item in instances
    )

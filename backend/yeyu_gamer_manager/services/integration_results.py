"""Freeze recorded integration results without adjudicating game tasks."""
from typing import Any, Iterable, Mapping


def integration_results(
    events: Iterable[Mapping[str, Any]],
    *,
    capture_failures: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: item["sequence"]):
        payload = event.get("payload", {})
        metrics = payload.get("metrics", {})
        if event.get("event_type") != "run_progress" or not isinstance(metrics, dict):
            continue
        status = metrics.get("integrationStatus")
        operation = payload.get("operation")
        if (not isinstance(status, str) or status not in {"completed", "pending", "failed"}
                or not isinstance(operation, str) or not operation):
            continue
        latest[operation] = {
            "runAttemptId": event["run_attempt_id"],
            "sequence": event["sequence"],
            "source": "adapter-event",
            "confirmedAt": event["created_at"],
            "operation": operation,
            "status": status,
            "code": payload.get("code", ""),
            "message": payload.get("message", ""),
            "trigger": metrics.get("trigger", ""),
            "waitingFor": metrics.get("waitingFor", ""),
        }
    results = list(latest.values())
    for event in capture_failures:
        if event.get("event_type") != "todo-step-capture.failed" or event.get("entity_type") != "run-attempt":
            continue
        payload = event["payload"]
        operation, phase = payload["operation"], payload["stepPhase"]
        results.append({
            "runAttemptId": event["entity_id"],
            "sequence": event["sequence"],
            "source": "manager-event",
            "eventId": event["event_id"],
            "confirmedAt": event["created_at"],
            "operation": f"{operation}-screenshot-{phase}",
            "status": "failed",
            "code": "step_capture_unavailable",
            "message": payload["reason"],
            "trigger": f"Manager step boundary: {operation}:{phase}",
            "waitingFor": "",
            "todoInstanceId": payload.get("todoInstanceId", ""),
            "todoAttemptId": payload.get("todoAttemptId", ""),
            "stepPhase": phase,
        })
    return results

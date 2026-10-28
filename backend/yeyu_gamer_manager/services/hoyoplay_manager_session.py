"""Durable journal for native work under the current Manager-owned operation."""
from contextlib import contextmanager
import uuid

from ..domain.models import utc_now
from ..store.sqlite_store import RecordNotFound
from .hoyoplay_native_execution import NativeContext
from .manager_errors import ManagerConflict

RESOURCE = "zzz-launcher-native-session"


def session_resource_id(task_id):
    # manager_resources has a global ID key, shared by all resource kinds.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "yeyugamer:zzz-native-session:" + str(uuid.UUID(task_id))))


def manager_context(manager, task_id, authority_valid, *, on_record=None):
    task_id = str(uuid.UUID(task_id))
    journal_id = session_resource_id(task_id)

    @contextmanager
    def fence():
        with manager.store.atomic():
            if not authority_valid():
                raise ManagerConflict("zzz_launcher_native_authority_lost")
            yield

    def record(event):
        if event.get("taskId") != task_id or event.get("managerId") != manager.manager_id:
            raise ManagerConflict("zzz_launcher_native_owner_mismatch")
        with manager.store.atomic():
            try:
                row = manager.store.get_resource(RESOURCE, journal_id)
            except RecordNotFound:
                row = None
            kind = event.get("event")
            if kind == "native-start-issued":
                if row is not None or not authority_valid():
                    raise ManagerConflict("zzz_launcher_native_session_already_consumed_or_fenced")
                manager.store.create_resource(RESOURCE, resource_id=journal_id, state="running",
                    document={**event, "createdAt": utc_now().isoformat(), "events": [event]})
            else:
                if row is None or row["document"].get("managerId") != manager.manager_id:
                    raise ManagerConflict("zzz_launcher_native_journal_missing")
                if kind != "native-closed" and not authority_valid():
                    raise ManagerConflict("zzz_launcher_native_event_fenced")
                events = row["document"].get("events", [])
                if len(events) >= 64 and kind != "native-closed":
                    raise ManagerConflict("zzz_launcher_native_event_limit")
                state = ("closed" if event.get("helperClosed") is True else "cleanup_required") if kind == "native-closed" else "running"
                manager.store.update_resource(RESOURCE, journal_id, state=state,
                    document={**row["document"], **event, "events": [*events[-63:], event],
                              "updatedAt": utc_now().isoformat()})
            if on_record is not None:
                on_record(event)

    return NativeContext(task_id, manager.manager_id,
                         manager.settings.data_dir / "launcher-executions" / task_id, fence, record)

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from yeyu_gamer_manager.domain.execution_control import (
    ActionReceipt,
    ActionResult,
    Checkpoint,
    CheckpointState,
    ControllerLease,
    ExecutionScope,
    FencingIdentity,
    FocusLease,
    LeaseState,
    Observation,
    ObservationPhase,
    ReplayDisposition,
    TodoBlocker,
    TodoBlockerKind,
    TodoBlockerState,
    WindowBinding,
    WindowBindingState,
)
from yeyu_gamer_manager.services.execution_control import (
    ExecutionControlViolation,
    accept_observation,
    active_todo_blockers,
    authorize_action_replay,
    grant_controller_lease,
    grant_focus_lease,
    raise_todo_blocker,
    record_action_receipt,
    record_checkpoint,
    record_checkpoint_invalidation,
    register_window_binding,
    reopen_todo_blocker,
    resolve_todo_blocker,
    transition_controller_lease,
    transition_focus_lease,
    transition_window_binding,
)


T0 = datetime(2026, 8, 28, 4, 0, tzinfo=timezone(timedelta(hours=8)))
ACTION_ID = "action-request-1"


class ExecutionControlFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = ExecutionScope(
            manager_id="manager-local",
            game_id="StarRail",
            run_id="run-1",
            run_attempt_id="attempt-1",
            game_day_key="2026-08-28",
        )
        self.fencing = FencingIdentity(
            token_hash="sha256:" + "a" * 64,
            public_fingerprint="sha256:" + "a" * 16,
        )
        self.controller = self.make_controller()
        self.binding = self.make_binding()
        self.focus = self.make_focus()

    def other_scope(self, **updates: str) -> ExecutionScope:
        values = {
            "manager_id": self.scope.manager_id,
            "game_id": self.scope.game_id,
            "run_id": self.scope.run_id,
            "run_attempt_id": self.scope.run_attempt_id,
            "game_day_key": self.scope.game_day_key,
        }
        values.update(updates)
        return ExecutionScope(**values)

    def make_controller(self, **updates: object) -> ControllerLease:
        values: dict[str, object] = {
            "controller_lease_id": "controller-1",
            "desktop_id": "desktop-session-1",
            "scope": self.scope,
            "holder_principal_id": "manager-executor",
            "generation": 1,
            "fencing": self.fencing,
            "acquired_at": T0,
            "expires_at": T0 + timedelta(minutes=10),
            "state": LeaseState.ACTIVE,
        }
        values.update(updates)
        return ControllerLease(**values)

    def make_binding(self, **updates: object) -> WindowBinding:
        values: dict[str, object] = {
            "window_binding_id": "window-binding-1",
            "controller_lease_id": self.controller.controller_lease_id,
            "desktop_id": self.controller.desktop_id,
            "scope": self.scope,
            "hwnd": 0x123456,
            "pid": 4321,
            "process_created_at": T0 - timedelta(minutes=1),
            "bound_at": T0 + timedelta(seconds=1),
            "state": WindowBindingState.ACTIVE,
        }
        values.update(updates)
        return WindowBinding(**values)

    def make_focus(self, **updates: object) -> FocusLease:
        values: dict[str, object] = {
            "focus_lease_id": "focus-1",
            "controller_lease_id": self.controller.controller_lease_id,
            "window_binding_id": self.binding.window_binding_id,
            "desktop_id": self.controller.desktop_id,
            "scope": self.scope,
            "generation": 1,
            "controller_generation": self.controller.generation,
            "fencing_token_fingerprint": self.fencing.public_fingerprint,
            "acquired_at": T0 + timedelta(seconds=2),
            "expires_at": T0 + timedelta(minutes=5),
            "state": LeaseState.ACTIVE,
        }
        values.update(updates)
        return FocusLease(**values)

    def make_observation(
        self,
        observation_id: str,
        phase: ObservationPhase,
        captured_at: datetime,
        *,
        artifact_ref: str | None = None,
        action_request_id: str | None = ACTION_ID,
        **updates: object,
    ) -> Observation:
        values: dict[str, object] = {
            "observation_id": observation_id,
            "scope": self.scope,
            "controller_lease_id": self.controller.controller_lease_id,
            "focus_lease_id": self.focus.focus_lease_id,
            "window_binding_id": self.binding.window_binding_id,
            "hwnd": self.binding.hwnd,
            "pid": self.binding.pid,
            "process_created_at": self.binding.process_created_at,
            "phase": phase,
            "action_request_id": action_request_id,
            "artifact_ref": artifact_ref or f"artifact-{observation_id}",
            "content_type": "image/png",
            "scene_code": "game-scene",
            "captured_at": captured_at,
        }
        values.update(updates)
        return Observation(**values)

    def action_frames(self) -> tuple[Observation, Observation]:
        return (
            self.make_observation(
                "observation-before",
                ObservationPhase.BEFORE_ACTION,
                T0 + timedelta(seconds=10),
            ),
            self.make_observation(
                "observation-after",
                ObservationPhase.AFTER_ACTION,
                T0 + timedelta(seconds=12),
            ),
        )

    def make_receipt(self, **updates: object) -> ActionReceipt:
        values: dict[str, object] = {
            "action_receipt_id": "action-receipt-1",
            "action_request_id": ACTION_ID,
            "action_code": "open-daily-panel",
            "scope": self.scope,
            "controller_lease_id": self.controller.controller_lease_id,
            "focus_lease_id": self.focus.focus_lease_id,
            "window_binding_id": self.binding.window_binding_id,
            "controller_generation": self.controller.generation,
            "fencing_token_fingerprint": self.fencing.public_fingerprint,
            "hwnd": self.binding.hwnd,
            "pid": self.binding.pid,
            "process_created_at": self.binding.process_created_at,
            "before_observation_id": "observation-before",
            "after_observation_id": "observation-after",
            "result": ActionResult.SUCCEEDED,
            "replay_disposition": ReplayDisposition.NEVER,
            "started_at": T0 + timedelta(seconds=11),
            "completed_at": T0 + timedelta(seconds=13),
        }
        values.update(updates)
        return ActionReceipt(**values)

    def assert_violation(self, code: str):
        class ViolationContext:
            def __init__(inner, owner: ExecutionControlFixture) -> None:
                inner.owner = owner
                inner.context = owner.assertRaises(ExecutionControlViolation)

            def __enter__(inner):
                return inner.context.__enter__()

            def __exit__(inner, exc_type, exc, traceback):
                result = inner.context.__exit__(exc_type, exc, traceback)
                if result:
                    inner.owner.assertEqual(inner.context.exception.code, code)
                return result

        return ViolationContext(self)


class ImmutableFactTests(ExecutionControlFixture):
    def test_fencing_accepts_only_hash_and_matching_public_fingerprint(self) -> None:
        self.assertEqual(self.fencing.public_fingerprint, "sha256:" + "a" * 16)
        with self.assertRaises(ValidationError):
            FencingIdentity(
                token_hash="a" * 64,
                public_fingerprint="sha256:" + "a" * 16,
            )
        with self.assertRaises(ValidationError):
            FencingIdentity(
                token_hash="sha256:" + "a" * 64,
                public_fingerprint="sha256:" + "b" * 16,
            )
        with self.assertRaises(ValidationError):
            FencingIdentity(
                token_hash="sha256:" + "a" * 64,
                public_fingerprint="sha256:" + "a" * 16,
                fencing_token="raw-secret",  # type: ignore[call-arg]
            )

    def test_models_are_strict_frozen_and_forbid_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_controller(generation="1")
        with self.assertRaises(ValidationError):
            self.controller.generation = 2  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            self.make_controller(raw_token="secret")

    def test_window_binding_requires_pid_creation_identity(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_binding(process_created_at=T0 + timedelta(seconds=2))
        registered = register_window_binding(self.controller, self.binding)
        self.assertEqual(
            (registered.hwnd, registered.pid, registered.process_created_at),
            (self.binding.hwnd, self.binding.pid, self.binding.process_created_at),
        )

    def test_screenshot_reference_is_opaque_and_action_phase_is_typed(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_observation(
                "observation-path",
                ObservationPhase.CHECKPOINT,
                T0 + timedelta(seconds=3),
                artifact_ref=r"C:\screens\proof.png",
                action_request_id=None,
            )
        with self.assertRaises(ValidationError):
            self.make_observation(
                "observation-no-action",
                ObservationPhase.BEFORE_ACTION,
                T0 + timedelta(seconds=3),
                action_request_id=None,
            )


class LeaseAndBindingTransitionTests(ExecutionControlFixture):
    def test_controller_is_single_per_desktop_until_explicitly_ended(self) -> None:
        self.assertIs(
            grant_controller_lease(self.controller, (), at=T0 + timedelta(seconds=1)),
            self.controller,
        )
        second_fence = FencingIdentity(
            token_hash="sha256:" + "b" * 64,
            public_fingerprint="sha256:" + "b" * 16,
        )
        second = self.make_controller(
            controller_lease_id="controller-2",
            generation=2,
            fencing=second_fence,
            acquired_at=T0 + timedelta(minutes=11),
            expires_at=T0 + timedelta(minutes=20),
        )
        with self.assert_violation("desktop_controller_busy"):
            grant_controller_lease(
                second,
                (self.controller,),
                at=T0 + timedelta(minutes=12),
            )

        expired = self.make_controller(
            state=LeaseState.EXPIRED,
            ended_at=self.controller.expires_at,
            end_reason_code="lease-expired",
            end_reason="Manager recorded expiry",
        )
        transition_controller_lease(self.controller, expired)
        self.assertIs(
            grant_controller_lease(second, (expired,), at=T0 + timedelta(minutes=12)),
            second,
        )

    def test_controller_generation_and_fencing_identity_cannot_go_backwards(self) -> None:
        released = self.make_controller(
            state=LeaseState.RELEASED,
            ended_at=T0 + timedelta(minutes=1),
            end_reason_code="completed",
            end_reason="controller released",
        )
        stale = self.make_controller(
            controller_lease_id="controller-2",
            acquired_at=T0 + timedelta(minutes=2),
            expires_at=T0 + timedelta(minutes=3),
        )
        with self.assert_violation("stale_controller_generation"):
            grant_controller_lease(stale, (released,), at=T0 + timedelta(minutes=2, seconds=1))

        reused = self.make_controller(
            controller_lease_id="controller-3",
            generation=2,
            acquired_at=T0 + timedelta(minutes=2),
            expires_at=T0 + timedelta(minutes=3),
        )
        with self.assert_violation("fencing_identity_reused"):
            grant_controller_lease(reused, (released,), at=T0 + timedelta(minutes=2, seconds=1))

    def test_window_replacement_requires_terminal_binding_and_preserves_identity(self) -> None:
        replacement = self.make_binding(
            window_binding_id="window-binding-2",
            hwnd=0x999,
            bound_at=T0 + timedelta(seconds=6),
        )
        with self.assert_violation("controller_window_already_bound"):
            register_window_binding(self.controller, replacement, (self.binding,))

        invalidated = self.make_binding(
            state=WindowBindingState.INVALIDATED,
            ended_at=T0 + timedelta(seconds=5),
            end_reason_code="pid-reused",
            end_reason="process creation identity changed",
        )
        transition_window_binding(self.binding, invalidated)
        self.assertIs(
            register_window_binding(self.controller, replacement, (invalidated,)),
            replacement,
        )

        time_regressed = self.make_binding(
            window_binding_id="window-binding-3",
            hwnd=0x998,
            bound_at=T0 + timedelta(seconds=4),
        )
        with self.assert_violation("window_binding_time_regressed"):
            register_window_binding(self.controller, time_regressed, (invalidated,))

        altered_terminal = self.make_binding(
            process_created_at=T0 - timedelta(minutes=2),
            state=WindowBindingState.INVALIDATED,
            ended_at=T0 + timedelta(seconds=5),
            end_reason_code="changed",
            end_reason="changed identity",
        )
        with self.assert_violation("window_process_identity_changed"):
            transition_window_binding(self.binding, altered_terminal)

    def test_focus_is_single_per_desktop_and_fenced_to_controller(self) -> None:
        self.assertIs(
            grant_focus_lease(
                self.controller,
                self.binding,
                self.focus,
                at=T0 + timedelta(seconds=3),
            ),
            self.focus,
        )
        second = self.make_focus(
            focus_lease_id="focus-2",
            generation=2,
            acquired_at=T0 + timedelta(seconds=20),
            expires_at=T0 + timedelta(minutes=4),
        )
        with self.assert_violation("desktop_focus_busy"):
            grant_focus_lease(
                self.controller,
                self.binding,
                second,
                (self.focus,),
                at=T0 + timedelta(seconds=21),
            )

        released = self.make_focus(
            state=LeaseState.RELEASED,
            ended_at=T0 + timedelta(seconds=15),
            end_reason_code="action-complete",
            end_reason="focus released",
        )
        transition_focus_lease(self.focus, released)
        self.assertIs(
            grant_focus_lease(
                self.controller,
                self.binding,
                second,
                (released,),
                at=T0 + timedelta(seconds=21),
            ),
            second,
        )

    def test_same_run_attempt_and_game_day_are_indivisible(self) -> None:
        mismatched = self.make_focus(scope=self.other_scope(run_attempt_id="attempt-2"))
        with self.assert_violation("execution_scope_mismatch"):
            grant_focus_lease(
                self.controller,
                self.binding,
                mismatched,
                at=T0 + timedelta(seconds=3),
            )


class ObservationAndActionTests(ExecutionControlFixture):
    def test_observation_rejects_pid_reuse_even_when_hwnd_and_pid_match(self) -> None:
        observation = self.make_observation(
            "observation-reused-pid",
            ObservationPhase.CHECKPOINT,
            T0 + timedelta(seconds=3),
            action_request_id=None,
            process_created_at=T0 - timedelta(minutes=2),
        )
        with self.assert_violation("window_process_identity_mismatch"):
            accept_observation(self.controller, self.binding, self.focus, observation)

    def test_action_requires_distinct_ordered_before_and_after_frames(self) -> None:
        before, after = self.action_frames()
        receipt = self.make_receipt()
        self.assertIs(
            record_action_receipt(
                self.controller,
                self.binding,
                self.focus,
                before,
                after,
                receipt,
            ),
            receipt,
        )

        same_artifact = self.make_observation(
            "observation-after-same",
            ObservationPhase.AFTER_ACTION,
            T0 + timedelta(seconds=12),
            artifact_ref=before.artifact_ref,
        )
        receipt_same = self.make_receipt(after_observation_id=same_artifact.observation_id)
        with self.assert_violation("action_frames_not_distinct"):
            record_action_receipt(
                self.controller,
                self.binding,
                self.focus,
                before,
                same_artifact,
                receipt_same,
            )

    def test_cross_attempt_after_frame_cannot_complete_action(self) -> None:
        before, _ = self.action_frames()
        after = self.make_observation(
            "observation-after-other-attempt",
            ObservationPhase.AFTER_ACTION,
            T0 + timedelta(seconds=12),
            scope=self.other_scope(run_attempt_id="attempt-previous"),
        )
        receipt = self.make_receipt(after_observation_id=after.observation_id)
        with self.assert_violation("execution_scope_mismatch"):
            record_action_receipt(
                self.controller,
                self.binding,
                self.focus,
                before,
                after,
                receipt,
            )

    def test_unknown_result_is_recordable_but_never_replayable(self) -> None:
        before, after = self.action_frames()
        receipt = self.make_receipt(
            result=ActionResult.UNKNOWN,
            reason_code="effect-ambiguous",
            reason="input may have reached the game",
        )
        record_action_receipt(
            self.controller,
            self.binding,
            self.focus,
            before,
            after,
            receipt,
        )
        with self.assert_violation("unknown_action_not_replayable"):
            authorize_action_replay(
                receipt,
                self.controller,
                self.binding,
                self.focus,
                at=T0 + timedelta(seconds=20),
            )
        with self.assertRaises(ValidationError):
            self.make_receipt(
                result=ActionResult.UNKNOWN,
                replay_disposition=ReplayDisposition.SAFE_RETRY,
                reason_code="effect-ambiguous",
                reason="input may have reached the game",
            )

    def test_only_explicit_confirmed_safe_retry_is_authorized(self) -> None:
        safe = self.make_receipt(
            result=ActionResult.NO_EFFECT,
            replay_disposition=ReplayDisposition.SAFE_RETRY,
        )
        self.assertIsNone(
            authorize_action_replay(
                safe,
                self.controller,
                self.binding,
                self.focus,
                at=T0 + timedelta(seconds=20),
            )
        )
        with self.assertRaises(ValidationError):
            self.make_receipt(replay_disposition=ReplayDisposition.SAFE_RETRY)

    def test_action_request_gets_one_receipt(self) -> None:
        before, after = self.action_frames()
        first = self.make_receipt()
        duplicate = self.make_receipt(action_receipt_id="action-receipt-2")
        with self.assert_violation("action_request_already_recorded"):
            record_action_receipt(
                self.controller,
                self.binding,
                self.focus,
                before,
                after,
                duplicate,
                (first,),
            )


class CheckpointTransitionTests(ExecutionControlFixture):
    def checkpoint_observation(self, suffix: str, at: datetime) -> Observation:
        return self.make_observation(
            f"observation-checkpoint-{suffix}",
            ObservationPhase.CHECKPOINT,
            at,
            action_request_id=None,
        )

    def make_checkpoint(
        self,
        observation: Observation,
        **updates: object,
    ) -> Checkpoint:
        values: dict[str, object] = {
            "checkpoint_id": "checkpoint-1",
            "scope": self.scope,
            "todo_instance_id": "todo-instance-1",
            "sequence": 1,
            "progress_index": 10,
            "state": CheckpointState.VALID,
            "observation_ids": (observation.observation_id,),
            "recorded_at": observation.captured_at + timedelta(seconds=1),
        }
        values.update(updates)
        return Checkpoint(**values)

    def test_checkpoint_sequence_and_progress_are_monotonic(self) -> None:
        first_observation = self.checkpoint_observation("1", T0 + timedelta(seconds=20))
        first = self.make_checkpoint(first_observation)
        record_checkpoint(None, first, observations=(first_observation,))

        second_observation = self.checkpoint_observation("2", T0 + timedelta(seconds=22))
        second = self.make_checkpoint(
            second_observation,
            checkpoint_id="checkpoint-2",
            sequence=2,
            progress_index=20,
            previous_checkpoint_id=first.checkpoint_id,
        )
        self.assertIs(
            record_checkpoint(first, second, observations=(second_observation,)),
            second,
        )

        stalled = self.make_checkpoint(
            second_observation,
            checkpoint_id="checkpoint-stalled",
            sequence=2,
            progress_index=10,
            previous_checkpoint_id=first.checkpoint_id,
        )
        with self.assert_violation("checkpoint_progress_not_monotonic"):
            record_checkpoint(first, stalled, observations=(second_observation,))

    def test_invalidation_is_explicit_and_progress_cannot_be_reused(self) -> None:
        first_observation = self.checkpoint_observation("1", T0 + timedelta(seconds=20))
        first = self.make_checkpoint(first_observation)
        invalidation_observation = self.make_observation(
            "observation-invalidation",
            ObservationPhase.RECOVERY,
            T0 + timedelta(seconds=22),
            action_request_id=None,
        )
        invalidation = self.make_checkpoint(
            invalidation_observation,
            checkpoint_id="checkpoint-invalidation-2",
            sequence=2,
            progress_index=first.progress_index,
            state=CheckpointState.INVALIDATED,
            previous_checkpoint_id=first.checkpoint_id,
            invalidates_checkpoint_id=first.checkpoint_id,
            invalidation_reason_code="scene-mismatch",
            invalidation_reason="new frame disproved the saved progress",
        )
        with self.assert_violation("checkpoint_invalidation_requires_explicit_transition"):
            record_checkpoint(first, invalidation, observations=(invalidation_observation,))
        record_checkpoint_invalidation(
            first,
            invalidation,
            observations=(invalidation_observation,),
        )

        next_observation = self.checkpoint_observation("3", T0 + timedelta(seconds=24))
        reused_progress = self.make_checkpoint(
            next_observation,
            checkpoint_id="checkpoint-3",
            sequence=3,
            progress_index=first.progress_index,
            previous_checkpoint_id=invalidation.checkpoint_id,
        )
        with self.assert_violation("checkpoint_progress_not_monotonic"):
            record_checkpoint(
                invalidation,
                reused_progress,
                observations=(next_observation,),
            )

        advanced = self.make_checkpoint(
            next_observation,
            checkpoint_id="checkpoint-3-advanced",
            sequence=3,
            progress_index=first.progress_index + 1,
            previous_checkpoint_id=invalidation.checkpoint_id,
        )
        record_checkpoint(invalidation, advanced, observations=(next_observation,))

    def test_checkpoint_observation_cannot_cross_game_day(self) -> None:
        observation = self.checkpoint_observation("other-day", T0 + timedelta(seconds=20))
        foreign = self.make_observation(
            observation.observation_id,
            ObservationPhase.CHECKPOINT,
            observation.captured_at,
            action_request_id=None,
            scope=self.other_scope(game_day_key="2026-08-27"),
        )
        checkpoint = self.make_checkpoint(foreign)
        with self.assert_violation("execution_scope_mismatch"):
            record_checkpoint(None, checkpoint, observations=(foreign,))


class TodoBlockerLifecycleTests(ExecutionControlFixture):
    def make_blocker(self, **updates: object) -> TodoBlocker:
        values: dict[str, object] = {
            "blocker_id": "blocker-1",
            "scope": self.scope,
            "todo_instance_id": "todo-instance-1",
            "kind": TodoBlockerKind.EVIDENCE_MISSING,
            "code": "fresh-evidence-missing",
            "state": TodoBlockerState.ACTIVE,
            "revision": 1,
            "raised_at": T0 + timedelta(seconds=30),
            "transitioned_at": T0 + timedelta(seconds=30),
            "reason": "fresh evidence is missing",
            "artifact_refs": ("artifact-blocker-open",),
        }
        values.update(updates)
        return TodoBlocker(**values)

    def test_blocker_lifecycle_is_raise_resolve_and_explicit_reopen(self) -> None:
        active = self.make_blocker()
        raise_todo_blocker(active)
        self.assertEqual(active_todo_blockers((active,)), (active,))

        resolved = self.make_blocker(
            state=TodoBlockerState.RESOLVED,
            revision=2,
            transitioned_at=T0 + timedelta(seconds=40),
            resolved_at=T0 + timedelta(seconds=40),
            resolution_code="fresh-observation",
            resolution_reason="a fresh frame established the result",
            resolution_artifact_refs=("artifact-blocker-resolved",),
        )
        resolve_todo_blocker(active, resolved)
        self.assertEqual(active_todo_blockers((active, resolved)), ())

        reopened = self.make_blocker(
            state=TodoBlockerState.ACTIVE,
            revision=3,
            transitioned_at=T0 + timedelta(seconds=50),
            reason="later evidence made the result ambiguous again",
            artifact_refs=("artifact-blocker-reopened",),
        )
        reopen_todo_blocker(resolved, reopened)
        self.assertEqual(active_todo_blockers((active, resolved, reopened)), (reopened,))

    def test_unknown_action_blocker_is_bound_to_its_receipt(self) -> None:
        receipt = self.make_receipt(
            result=ActionResult.UNKNOWN,
            reason_code="effect-ambiguous",
            reason="input may have reached the game",
        )
        blocker = self.make_blocker(
            kind=TodoBlockerKind.AMBIGUOUS_ACTION,
            code="action-effect-unknown",
            reason="the action effect is unknown",
            action_receipt_id=receipt.action_receipt_id,
        )
        self.assertIs(raise_todo_blocker(blocker, receipt=receipt), blocker)
        with self.assert_violation("ambiguous_action_receipt_required"):
            raise_todo_blocker(blocker)

    def test_blocker_cannot_skip_or_repeat_lifecycle_edges(self) -> None:
        active = self.make_blocker()
        duplicate = self.make_blocker(blocker_id="blocker-2")
        with self.assert_violation("blocker_already_active"):
            raise_todo_blocker(duplicate, (active,))

        with self.assert_violation("blocker_not_resolved"):
            reopen_todo_blocker(active, duplicate)

        resolved = self.make_blocker(
            state=TodoBlockerState.RESOLVED,
            revision=2,
            transitioned_at=T0 + timedelta(seconds=40),
            resolved_at=T0 + timedelta(seconds=40),
            resolution_code="reviewed",
            resolution_reason="reviewed",
            resolution_artifact_refs=("artifact-reviewed",),
        )
        with self.assert_violation("blocker_not_active"):
            resolve_todo_blocker(resolved, resolved)

    def test_blocker_resolution_preserves_scope_and_origin_evidence(self) -> None:
        active = self.make_blocker()
        foreign = self.make_blocker(
            scope=self.other_scope(game_day_key="2026-08-27"),
            state=TodoBlockerState.RESOLVED,
            revision=2,
            transitioned_at=T0 + timedelta(seconds=40),
            resolved_at=T0 + timedelta(seconds=40),
            resolution_code="reviewed",
            resolution_reason="reviewed",
            resolution_artifact_refs=("artifact-reviewed",),
        )
        with self.assert_violation("blocker_identity_changed"):
            resolve_todo_blocker(active, foreign)


if __name__ == "__main__":
    unittest.main()

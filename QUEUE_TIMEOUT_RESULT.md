# Queue timeout QA candidate — CHANGES_REQUIRED

2026-10-28 follow-up: the orphan pending-execution and daily-priority candidate,
additional verification and the two missing scheduler/serial integration points
are documented in `ORPHAN_PENDING_EXECUTION_RESULT.md`. This original report
describes the preceding `34c4523` subset, not full acceptance of later work.

## Scope and base

- Branch: `qa/queue-timeout-20261028`, parent `f4ab6bf`.
- Candidate only. Do not merge, install, switch Manager bindings or enable this in production.
- No running games, Manager APIs, runtime databases, configurations or task registrations were changed.
- The running source worktree has 1113 changes. This branch does not incorporate them.

## Implemented subset

The existing `review_required` state represents unfinished work; no new success state is introduced.

For a terminal human-gated attempt, Manager persists an `awaiting` queue-parking resource keyed by the original attempt. At budget expiry it rechecks batch seal, run state, latest attempt and membership owner inside the store transaction. Only if the Host ownership is already idle does it revoke that attempt's controller lease and terminalize that batch membership as `review_required`. Remaining members continue in the same batch. The original batch remains unsealed while it contains unresolved parked work.

Parking does not resolve human blockers or authorize execution. The formal release entry remains available. A new takeover reclaims the parked gate and invalidates the prior queue-yield authorization. Client cleanup excludes currently parked games. Late events/results are associated with the original attempt and cannot mark the parked work successful. The per-iteration completion closure now binds its own result holder/event/run identity instead of sharing late-bound loop cells.

### Budget data

Defaults live in `backend/yeyu_gamer_manager/services/resources/queue_timeout_budgets.json`. Limits live in `queue_timeout_limits.json`; each fact has one source. Existing Manager config accepts partial overrides under `queue_timeout_budget` through the typed config model. No real configuration was written.

| Field | Default seconds | Consumer |
| --- | ---: | --- |
| human_takeover_seconds | 300 | queue parking |
| completion_review_seconds | 900 | persisted completion review barrier |
| callback_overhead_seconds | 40 | batch wait in addition to execution budget |
| callback_cancel_grace_seconds | 15 | second callback wait after cancel |
| poll_seconds | 1 | bounded parking wait slices |

Human/review budgets allow zero. Other budgets must be positive. Booleans, non-finite values, unknown fields and values above the policy bounds are rejected. Maximum budget is 7200 seconds; maximum poll interval is 5 seconds. Defaults are cached; mutable caller configuration is not.

### Evidence fields

`batchId`, `gameId`, `runId`, `runAttemptId`, reason/code, started/expiry/parked timestamps, frozen budget, existing Todo evidence references, log correlation scope, evidence availability, ownership-yield status and `businessCompleted=false`.

Missing screenshot evidence is explicitly `missing`; this implementation does not fabricate or capture an image from an unrelated foreground window. Complete startup screenshot/artifact collection remains a gap.

## Verification

From this worktree, with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=<worktree>/backend;<worktree>/backend/tests`:

```powershell
& D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe -m unittest -v test_queue_timeout_budget test_queue_parking_policy test_completion_contract_manager.CompletionContractManagerTests.test_failed_attempt_does_not_hold_the_review_barrier test_completion_contract_manager.CompletionContractManagerTests.test_review_barrier_expires_into_machine_seal
```

20 tests passed in the recorded run. Tests use temporary SQLite/configuration and mocks, not the live Manager. They cover budget validation/immutability, unattended terminal human gate yielding to the next member without successful acceptance, formal release, rejection of premature resume, new takeover, late results/events, central seal rejection, and refusal to clear an exited-but-not-handed-off Host owner. Existing completion-review expiration tests also pass.

Important limitation: HEAD itself passes `selected_operations` to an execution-plan dataclass that does not define it. It also reads an undefined `observation_only_operations`. The parking tests explicitly isolate non-WW plans with a test-only dataclass supplying empty fields. They are NOT unmodified protocol integration tests, WW acceptance or end-to-end queue acceptance. The initial unmodified test failed before reaching parking. No production protocol repair or dirty-worktree copy was smuggled into this branch.

`git diff --check` and the shared AI security prefilter passed. These are not a semantic release approval.

## Remaining blockers and transfer

1. Supply a coherent committed baseline for the current protocol and scheduler. `autonomous_daily.py` and current human-gate modules exist only outside this HEAD; 04:00/05:10, heartbeat and reboot reconciliation have not been integrated or verified here.
2. A live, hung, killed-but-not-reconciled or callback-stuck Host still enters the existing safe recovery barrier. This candidate deliberately does not clear its ownership. Host exit alone is not proof that all input executors are quiet: the existing C# Job permits breakaway. A process/attempt-scoped quiescence receipt and callback handoff are required before implementing unconditional timeout advancement. Do not replace the barrier with a bare `continue` or arbitrary process-tree kill.
3. The same-GameRun reconciliation provider is not implemented here. Formal release remains possible, but execution resume is explicitly refused while the parking gate remains unresolved. The required observation/checkpoint/binding validation must become persisted facts before this restriction can be lifted; a text field is not authorization.
4. Restart recovery of awaiting/parked members, multi-game client retention, full evidence collection and nonzero-budget concurrent races still need dedicated integration coverage.
5. Existing tests that require indefinite human queue suspension need migration to the new product contract once the committed integration baseline exists. A full suite/build was not run; no actual game acceptance is claimed.

The requested three-case acceptance (unattended human, indefinitely pending review, stuck/killed lock owner all allowing later games to finish) is NOT complete. This branch is an isolated, reviewable partial candidate, not a release or a completed fix.

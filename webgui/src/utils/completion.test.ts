import { describe, expect, it } from 'vitest'
import type { AgentWorkItem, BatchRun, EvidenceArtifact, TodoInstance } from '../api/contracts'
import {
  artifactOptionsForPredicate,
  awaitingCompletionReviewRunIds,
  batchRunIds,
  blockersFromBatchResult,
  blockersFromDiagnostics,
  buildCompletionReviewSubmission,
  emptyCompletionReviewForm,
  parseCompletionReviewContract,
  parseCompletionReviewScope,
  workItemCompletionArtifacts,
} from './completion'

function workItem(overrides: Partial<AgentWorkItem> = {}): AgentWorkItem {
  return {
    workItemId: 'work-review-1',
    kind: 'evidence_review',
    batchId: 'batch-1',
    gameId: 'GameA',
    cadence: 'daily',
    runId: 'run-1',
    artifactRefs: ['artifact-frame'],
    result: {
      completionReviewScope: {
        schemaVersion: 3,
        batchId: 'batch-1',
        gameId: 'GameA',
        runId: 'run-1',
        runAttemptId: 'attempt-1',
        attemptLineageIds: ['attempt-0', 'attempt-1'],
        gameDayKey: 'daily:2026-08-28',
        periodStartsAt: '2026-08-28T04:00:00+08:00',
        periodEndsAt: '2026-08-29T04:00:00+08:00',
        requiredTodoInstanceIds: ['todo-1'],
        requiredTodos: [{
          todoInstanceId: 'todo-1',
          operation: 'claim_daily',
          status: 'completed',
          artifactRefs: ['artifact-frame'],
        }],
      },
      completionReviewContract: {
        schemaVersion: 'completion-review-contract/v2',
        policyId: 'game-a.daily',
        policyVersion: '2.1.0',
        gameId: 'GameA',
        cadence: 'daily',
        timezone: 'Asia/Shanghai',
        resetTime: '04:00',
        periodStartsAt: '2026-08-28T04:00:00+08:00',
        periodEndsAt: '2026-08-29T04:00:00+08:00',
        policyStatus: 'supported',
        unsupportedReason: null,
        todoReviewContract: {
          requiredForAccepted: true,
          acceptedVerdict: 'confirmed',
          reasonCodeRequired: true,
          artifactRefsRequired: true,
          allowedEvidenceContentTypes: ['image/jpeg', 'image/png'],
        },
        predicates: [{
          predicateId: 'daily.progress',
          label: '每日进度',
          constraints: [
            { metric: 'current', operator: 'at_least', expected: 10 },
            { metric: 'target', operator: 'equals', expected: 10 },
          ],
          allowedArtifactKinds: ['raw_frame'],
        }],
      },
    },
    ...overrides,
  }
}

function artifact(overrides: Partial<EvidenceArtifact> = {}): EvidenceArtifact {
  return {
    artifactId: 'artifact-frame',
    kind: 'raw_frame',
    gameId: 'GameA',
    runId: 'run-1',
    runAttemptId: 'attempt-1',
    todoInstanceId: 'todo-1',
    raw: true,
    contentType: 'image/png',
    ...overrides,
  }
}

function todo(overrides: Partial<TodoInstance> = {}): TodoInstance {
  return {
    todoInstanceId: 'todo-1',
    todoDefinitionId: 'definition-1',
    definitionVersion: 1,
    catalogVersion: 'catalog-1',
    sourceHash: 'a'.repeat(64),
    gameId: 'StarRail',
    cadence: 'daily',
    periodKey: 'daily:2026-08-28',
    periodStartsAt: '2026-08-28T04:00:00+08:00',
    periodEndsAt: '2026-08-29T04:00:00+08:00',
    operation: 'claim_daily_training',
    title: '领取每日实训奖励',
    category: 'daily',
    orderIndex: 1,
    required: true,
    risk: 'routine_action',
    automationDifficulty: 'high',
    automationState: 'implemented',
    resetRule: { timezone: 'Asia/Shanghai', time: '04:00', cadence: 'daily' },
    sourceRefs: [],
    status: 'human_required',
    dispatchDisposition: 'deferred_human',
    dispatchReason: 'Manager requires explicit human release.',
    actionAvailability: {
      execute: false,
      resume: false,
      review: true,
      releaseHuman: true,
      submitManualEvidence: true,
      nextAction: 'Release human takeover after login.',
    },
    attempts: 2,
    reason: '登录已失效，等待明确释放',
    evidenceRefs: ['artifact-login'],
    createdAt: '2026-08-28T04:00:00+08:00',
    updatedAt: '2026-08-28T05:00:00+08:00',
    ...overrides,
  }
}

describe('CompletionReview boundary', () => {
  it('parses the Manager contract and builds its typed predicates from matching work-item artifacts', () => {
    const item = workItem()
    const scope = parseCompletionReviewScope(item, 'run-1').value
    const contract = parseCompletionReviewContract(item, scope).value
    expect(contract).toMatchObject({
      schemaVersion: 'completion-review-contract/v2',
      policyId: 'game-a.daily',
      policyStatus: 'supported',
      unsupportedReason: '',
    })
    const available = workItemCompletionArtifacts(item, [artifact()], scope)
    expect(artifactOptionsForPredicate(contract!.predicates[0], available)).toEqual([
      expect.objectContaining({ artifactId: 'artifact-frame', kind: 'raw_frame', todoInstanceId: 'todo-1' }),
    ])

    const form = emptyCompletionReviewForm(contract, scope)
    form.todoReviews['todo-1'] = {
      verdict: 'confirmed',
      reasonCode: 'visual_completion_confirmed',
      artifactRefs: ['artifact-frame'],
    }
    form.predicates['daily.progress'].metrics.current = 12
    form.predicates['daily.progress'].metrics.target = 10
    form.predicates['daily.progress'].artifactRefs = ['artifact-frame']
    expect(buildCompletionReviewSubmission(item, form, [artifact()], 'accepted', 'run-1')).toEqual({
      value: {
        gameId: 'GameA',
        runId: 'run-1',
        runAttemptId: 'attempt-1',
        gameDayKey: 'daily:2026-08-28',
        predicates: [{
          predicateId: 'daily.progress',
          metrics: { current: 12, target: 10 },
          artifactRefs: ['artifact-frame'],
        }],
        todoReviews: [{
          todoInstanceId: 'todo-1',
          verdict: 'confirmed',
          reasonCode: 'visual_completion_confirmed',
          artifactRefs: ['artifact-frame'],
        }],
      },
      errors: [],
    })
  })

  it('rejects path-shaped, wrong-kind, and wrong-run artifacts instead of forwarding them', () => {
    const item = workItem({ artifactRefs: ['C:\\screens\\reward.png', 'artifact-frame'] })
    const contract = parseCompletionReviewContract(item, parseCompletionReviewScope(item).value).value
    const scope = parseCompletionReviewScope(item).value
    const form = emptyCompletionReviewForm(contract, scope)
    form.todoReviews['todo-1'] = {
      verdict: 'confirmed',
      reasonCode: 'visual_completion_confirmed',
      artifactRefs: ['artifact-frame'],
    }
    form.predicates['daily.progress'].metrics.current = 12
    form.predicates['daily.progress'].metrics.target = 10
    form.predicates['daily.progress'].artifactRefs = ['artifact-frame']
    const result = buildCompletionReviewSubmission(item, form, [artifact({ kind: 'derived_diff' })], 'accepted')
    expect(result.value).toBeUndefined()
    expect(result.errors).toContainEqual(expect.stringContaining('kind 受合同允许'))
    expect(workItemCompletionArtifacts(item, [artifact({ runId: 'run-other' })], parseCompletionReviewScope(item).value)).toEqual([])
  })

  it('fails closed on malformed scope, snapshot mismatch, and unregistered operator aliases', () => {
    const missingAttempt = workItem({ result: { completionReviewScope: {
      schemaVersion: 3,
      batchId: 'batch-1',
      gameId: 'GameA',
      runId: 'run-1',
      runAttemptId: null,
      attemptLineageIds: ['attempt-1'],
      gameDayKey: 'daily:2026-08-28',
      periodStartsAt: '2026-08-28T04:00:00+08:00',
      periodEndsAt: '2026-08-29T04:00:00+08:00',
      requiredTodoInstanceIds: ['todo-1'],
      requiredTodos: [{ todoInstanceId: 'todo-1', operation: 'claim_daily', status: 'completed', artifactRefs: ['artifact-frame'] }],
    } } })
    expect(parseCompletionReviewScope(missingAttempt).errors).toContain('runAttemptId 缺失，或不是 Manager opaque ID')

    expect(parseCompletionReviewScope(workItem(), 'run-other').errors)
      .toContain('scope runId 与 Manager 当前 snapshot 不一致')

    const wrongLineage = workItem()
    const wrongScope = wrongLineage.result?.completionReviewScope as Record<string, unknown>
    wrongScope.attemptLineageIds = ['attempt-0']
    expect(parseCompletionReviewScope(wrongLineage).errors)
      .toContain('scope runAttemptId 必须属于 attemptLineageIds')

    const periodMismatch = workItem()
    const periodContract = periodMismatch.result?.completionReviewContract as Record<string, unknown>
    periodContract.periodEndsAt = '2026-08-30T04:00:00+08:00'
    expect(parseCompletionReviewContract(periodMismatch, parseCompletionReviewScope(periodMismatch).value).errors)
      .toContain('contract periodEndsAt 与 scope 不一致')

    const malformed = workItem()
    const raw = malformed.result?.completionReviewContract as Record<string, unknown>
    const predicates = raw.predicates as Array<Record<string, unknown>>
    const constraints = predicates[0].constraints as Array<Record<string, unknown>>
    constraints[0].operator = 'gte'
    expect(parseCompletionReviewContract(malformed, parseCompletionReviewScope(malformed).value).errors)
      .toContain('completionReviewContract.predicates[0].constraints[0].operator 只能是 equals 或 at_least')
  })

  it('allows supported generic empty predicates and limits unsupported policy decisions', () => {
    const generic = workItem()
    const genericContract = generic.result?.completionReviewContract as Record<string, unknown>
    genericContract.predicates = []
    const parsedGeneric = parseCompletionReviewContract(generic, parseCompletionReviewScope(generic).value).value
    const genericScope = parseCompletionReviewScope(generic).value
    const genericForm = emptyCompletionReviewForm(parsedGeneric, genericScope)
    genericForm.todoReviews['todo-1'] = {
      verdict: 'confirmed',
      reasonCode: 'visual_completion_confirmed',
      artifactRefs: ['artifact-frame'],
    }
    expect(buildCompletionReviewSubmission(generic, genericForm, [artifact()], 'accepted').value?.predicates)
      .toEqual([])

    const unsupported = workItem()
    const unsupportedContract = unsupported.result?.completionReviewContract as Record<string, unknown>
    unsupportedContract.policyStatus = 'unsupported'
    unsupportedContract.unsupportedReason = 'adapter policy has not been published'
    expect(buildCompletionReviewSubmission(unsupported, emptyCompletionReviewForm(), [], 'accepted').errors)
      .toEqual(['当前 policy 不支持完成验收：adapter policy has not been published'])
    const unsupportedScope = parseCompletionReviewScope(unsupported).value
    const unsupportedForm = emptyCompletionReviewForm(undefined, unsupportedScope)
    unsupportedForm.todoReviews['todo-1'] = {
      verdict: 'rejected',
      reasonCode: 'visual_completion_rejected',
      artifactRefs: ['artifact-frame'],
    }
    expect(buildCompletionReviewSubmission(unsupported, unsupportedForm, [artifact()], 'rejected').value?.predicates)
      .toEqual([])
    expect(buildCompletionReviewSubmission(unsupported, emptyCompletionReviewForm(), [], 'review_required').value?.predicates)
      .toEqual([])
  })
})

describe('Completion ledger projection', () => {
  it('keeps awaiting review run IDs and final run scope deduplicated', () => {
    const batch = {
      batchId: 'batch-1',
      state: 'running',
      result: {
        awaitingCompletionReviewRunIds: ['run-1', 'run-1'],
        finalGameRunIds: ['run-2'],
        completionReviewWorkItemIds: { 'run-3': 'work-3' },
        completionContracts: [{ runId: 'run-4' }],
      },
    } as unknown as BatchRun
    expect(awaitingCompletionReviewRunIds(batch)).toEqual(['run-1'])
    expect(batchRunIds(batch)).toEqual(['run-1', 'run-2', 'run-4', 'run-3'])
  })

  it('projects human_required Todo and attempt diagnosis as explicit blockers', () => {
    const batch = {
      batchId: 'batch-1',
      state: 'running',
      result: { todoSnapshot: { StarRail: { instances: [todo()] } } },
    } as BatchRun
    expect(blockersFromBatchResult(batch.result)).toEqual([
      expect.objectContaining({ blockerId: 'todo-1', status: 'human_required', evidenceRefs: ['artifact-login'] }),
    ])
    expect(blockersFromDiagnostics([{
      todoInstanceId: 'todo-2',
      title: '窗口接管',
      humanRequiredCount: 2,
      evidenceRefs: ['artifact-takeover'],
    }])).toEqual([
      expect.objectContaining({ blockerId: 'todo-2', status: 'human_required', humanRequiredCount: 2 }),
    ])
  })
})

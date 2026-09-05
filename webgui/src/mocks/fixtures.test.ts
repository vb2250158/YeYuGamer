import { describe, expect, it } from 'vitest'
import type {
  AgentWorkItem,
  BatchRun,
  CommandReceipt,
  CompletionAdjudication,
  CompletionReview,
  NotificationAttempt,
  NotificationDelivery,
  NotificationPolicy,
  NotificationPreview,
  PageResult,
} from '../api/contracts'
import { createMockResponse } from './fixtures'

describe('notification mock contracts', () => {
  it('serves typed policy, delivery, preview, and attempt resources', async () => {
    const policy = await createMockResponse<NotificationPolicy>('GET', '/notification-policy')
    expect(policy).toMatchObject({
      enabled: true,
      automaticDispatch: true,
      recipientBindingId: 'self-primary',
      secretState: 'configured',
    })
    expect(JSON.stringify(policy).toLowerCase()).not.toMatch(/smtp|password|recipientaddress/)

    const page = await createMockResponse<PageResult<NotificationDelivery>>('GET', '/notifications?limit=100')
    const ambiguous = page.items.find((item) => item.notificationId === 'notification_mock_ambiguous')
    expect(ambiguous).toMatchObject({
      state: 'failed',
      dispatchGate: 'manual_review',
      lastErrorClass: 'transport_ambiguous',
    })

    const preview = await createMockResponse<NotificationPreview>(
      'GET',
      '/notifications/notification_mock_ambiguous/preview',
    )
    expect(preview.htmlBody).toContain('<img')
    expect(preview.attachmentDecisions[0]).toEqual({
      artifactId: 'artifact_star_reward',
      accepted: true,
      reason: 'strict-seal-artifact',
    })

    const attempts = await createMockResponse<PageResult<NotificationAttempt>>(
      'GET',
      '/notifications/notification_mock_ambiguous/attempts',
    )
    expect(attempts.items).toHaveLength(1)
    expect(attempts.items[0].outcome).toBe('ambiguous')
  })

  it('refuses an ambiguous mock retry until confirmAmbiguous is true', async () => {
    const path = '/notifications/notification_mock_ambiguous/retry-requests'
    await expect(createMockResponse('POST', path, {
      reason: 'reviewed once',
      requestedBy: 'webgui',
      confirmAmbiguous: false,
    })).rejects.toThrow('explicit confirmation')

    const receipt = await createMockResponse<CommandReceipt>('POST', path, {
      reason: 'reviewed twice',
      requestedBy: 'webgui',
      confirmAmbiguous: true,
    })
    expect(receipt.state).toBe('accepted')
    expect(receipt.result).toMatchObject({
      notification: { dispatchGate: 'automatic', lastErrorClass: '' },
    })
  })
})

describe('completion review mock contracts', () => {
  it('serves a scoped evidence-review item, batch barrier, review, and adjudication', async () => {
    const workItems = await createMockResponse<PageResult<AgentWorkItem>>('GET', '/agent/work-items')
    const evidenceReview = workItems.items.find((item) => item.kind === 'evidence_review')
    expect(evidenceReview?.result?.completionReviewScope).toMatchObject({
      schemaVersion: 3,
      gameId: 'StarRail',
      runId: 'run_star_0827',
      runAttemptId: 'attempt_star_1',
      attemptLineageIds: ['attempt_star_0', 'attempt_star_1'],
      periodStartsAt: '2026-08-27T04:00:00+08:00',
      periodEndsAt: '2026-08-28T04:00:00+08:00',
      requiredTodoInstanceIds: [
        'mock-todo.v1.starrail.daily.spend_stamina',
        'mock-todo.v1.starrail.daily.claim_daily',
      ],
    })
    expect(evidenceReview?.result?.completionReviewContract).toMatchObject({
      schemaVersion: 'completion-review-contract/v2',
      policyId: 'StarRail.daily',
      policyStatus: 'supported',
      todoReviewContract: { requiredForAccepted: true, acceptedVerdict: 'confirmed' },
      predicates: [
        { predicateId: 'starrail.daily_training.points' },
        { predicateId: 'starrail.daily_training.reward_tiers' },
      ],
    })

    const batch = await createMockResponse<BatchRun>('GET', '/batches/batch_0827')
    expect(batch.result?.awaitingCompletionReviewRunIds).toEqual(['run_star_0827'])
    expect(batch.result?.completionReviewWorkItemIds).toEqual({ run_star_0827: 'wi_star_reward' })

    const reviews = await createMockResponse<PageResult<CompletionReview>>(
      'GET',
      '/completion-reviews?runId=run_star_0827',
    )
    expect(reviews.items[0]?.predicates.map((item) => item.predicateId)).toEqual([
      'starrail.daily_training.points',
      'starrail.daily_training.reward_tiers',
    ])

    const adjudications = await createMockResponse<PageResult<CompletionAdjudication>>(
      'GET',
      '/completion-adjudications?batchId=batch_0827',
    )
    expect(adjudications.items[0]?.contract).toMatchObject({
      outcome: 'review_required',
      acceptedDone: false,
      attemptLineageIds: ['attempt_star_0', 'attempt_star_1'],
      currentAttemptEvidenceRefs: ['artifact_star_reward'],
      carriedEvidenceRefs: ['artifact_star_before'],
    })
  })

  it('projects several Todo diagnosis contexts and accepts a multi-Todo decision body', async () => {
    const workItems = await createMockResponse<PageResult<AgentWorkItem>>('GET', '/agent/work-items')
    const evidenceReview = workItems.items.find((item) => item.kind === 'evidence_review')
    const snapshot = evidenceReview?.result?.todoSnapshot as { items?: Array<Record<string, unknown>> } | undefined
    expect(snapshot?.items).toHaveLength(2)
    expect(snapshot?.items?.[0]).toEqual(expect.objectContaining({
      dispatch: expect.any(Object),
      latestTodoAttempt: expect.any(Object),
      latestAutomationAssessment: expect.any(Object),
    }))
    expect(snapshot?.items?.[1]).toEqual(expect.objectContaining({
      activeBlocker: expect.any(Object),
    }))

    const receipt = await createMockResponse<CommandReceipt>('POST', '/claims/decisions', {
      claimId: 'claim-1', fencingToken: 'f'.repeat(32), decision: 'review_required', reason: 'diagnosed',
      evidenceIds: [], requestedBy: 'webgui',
      todoDiagnoses: [
        { todoInstanceId: 'todo-1', difficulty: 'easy', automatable: true, confidence: 0.9, basis: ['fact'], failureStage: '', issue: '', recommendation: '', evidenceIds: [] },
        { todoInstanceId: 'todo-2', difficulty: 'hard', automatable: false, confidence: 0.8, basis: ['fact'], failureStage: 'login', issue: 'gate', recommendation: 'human release', evidenceIds: [] },
      ],
    })
    expect(receipt.result).toMatchObject({
      todoDiagnoses: [
        { todoInstanceId: 'todo-1', automatable: true },
        { todoInstanceId: 'todo-2', automatable: false },
      ],
      legacyTodoDiagnosisUsed: false,
    })
  })
})

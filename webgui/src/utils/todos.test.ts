import { describe, expect, it } from 'vitest'
import type { TodoInstance } from '../api/contracts'
import {
  completedTodo,
  deferredTodo,
  latestAutomationAssessment,
  resetCountdown,
  resetRuleLabel,
  resetRulePresentation,
  schedulableTodo,
  todoDiagnostics,
  todoSummaryFromItems,
  todosByGame,
} from './todos'

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
    operation: 'spend_stamina',
    title: '消耗体力',
    category: 'daily',
    orderIndex: 1,
    required: true,
    risk: 'routine_action',
    automationDifficulty: 'low',
    adapterCapabilityRef: 'game.daily.run@1.0',
    automationState: 'implemented',
    resetRule: { timezone: 'Asia/Shanghai', time: '04:00', cadence: 'daily' },
    sourceRefs: ['tool:March7thAssistant'],
    status: 'pending',
    dispatchDisposition: 'eligible',
    dispatchReason: 'Manager confirmed eligibility.',
    actionAvailability: {
      execute: true,
      resume: false,
      review: false,
      releaseHuman: false,
      submitManualEvidence: false,
      nextAction: 'Dispatch through Manager.',
    },
    attempts: 0,
    evidenceRefs: [],
    createdAt: '2026-08-28T04:00:00+08:00',
    updatedAt: '2026-08-28T04:00:00+08:00',
    ...overrides,
  }
}

describe('Todo projection', () => {
  it('checks completion while reading schedulability only from the Manager dispatch projection', () => {
    expect(completedTodo(todo({ status: 'completed' }))).toBe(true)
    expect(completedTodo(todo({ status: 'skipped' }))).toBe(false)
    expect(schedulableTodo(todo())).toBe(true)
    expect(schedulableTodo(todo({ risk: 'forbidden' }))).toBe(true)
    expect(schedulableTodo(todo({ adapterCapabilityRef: null, automationState: 'unsupported' }))).toBe(true)
    expect(schedulableTodo(todo({ dispatchDisposition: 'deferred_forbidden' }))).toBe(false)
    expect(deferredTodo(todo({ dispatchDisposition: 'deferred_forbidden' }))).toBe(true)
    expect(deferredTodo(todo({ status: 'blocked', dispatchDisposition: 'eligible' }))).toBe(false)
  })

  it('derives progress without treating skipped required work as completed', () => {
    const items = [
      todo({ todoInstanceId: 'done', status: 'completed' }),
      todo({ todoInstanceId: 'skip', status: 'skipped', orderIndex: 2 }),
      todo({ todoInstanceId: 'optional', required: false, status: 'completed', orderIndex: 3 }),
    ]
    const summary = todoSummaryFromItems(items, 'daily')
    expect(summary.requiredCompleted).toBe(1)
    expect(summary.requiredRemaining).toBe(1)
    expect(summary.allRequiredCompleted).toBe(false)
    expect(summary.progress.percent).toBe(50)
    expect(summary.periodKeys).toEqual(['daily:2026-08-28'])
    expect(summary.periodStartsAt).toEqual(['2026-08-28T04:00:00+08:00'])
    expect(summary.periodEndsAt).toEqual(['2026-08-29T04:00:00+08:00'])
    expect(summary.resetRules).toEqual([{ timezone: 'Asia/Shanghai', time: '04:00', cadence: 'daily' }])
    expect(summary.scopeKey).toMatch(/^daily:daily:2026-08-28:/)
    expect(summary.scopeFingerprint).toMatch(/^webgui-fallback-v1:[0-9a-f]{16}$/)
    expect(todoSummaryFromItems([...items].reverse(), 'daily').scopeFingerprint).toBe(summary.scopeFingerprint)
  })

  it('keeps human_required visible, difficult, and outside the automatic schedule', () => {
    const humanGate = todo({
      status: 'human_required',
      dispatchDisposition: 'deferred_human',
      reason: '登录状态失效，等待人工处理',
    })
    const summary = todoSummaryFromItems([humanGate], 'daily')
    expect(schedulableTodo(humanGate)).toBe(false)
    expect(deferredTodo(humanGate)).toBe(true)
    expect(summary.counts.human_required).toBe(1)
    expect(summary.difficultOperations).toEqual([
      expect.objectContaining({
        todoInstanceId: 'todo-1',
        status: 'human_required',
      }),
    ])
  })

  it('keeps game grouping ordered and formats an expired reset as an explicit reconcile boundary', () => {
    const grouped = todosByGame([
      todo({ todoInstanceId: 'two', orderIndex: 2 }),
      todo({ todoInstanceId: 'one', orderIndex: 1 }),
    ])
    expect(grouped.get('StarRail')?.map((item) => item.todoInstanceId)).toEqual(['one', 'two'])
    expect(resetCountdown('2026-08-29T04:00:00+08:00', Date.parse('2026-08-29T04:00:00+08:00')))
      .toBe('已到重置边界，等待显式对账')
    expect(resetRuleLabel({ timezone: 'Asia/Shanghai', time: '04:00', weekStartDay: 'Monday' }))
      .toBe('Asia/Shanghai · Monday 04:00')
  })

  it('reads Manager-owned difficult Todo context from an Agent work item', () => {
    expect(todoDiagnostics({
      todoDifficulty: {
        items: [{ todoInstanceId: 'todo-hard', gameId: 'ZZZ', status: 'blocked', automationDifficulty: 'high', reason: 'login_gate' }],
      },
      todoSnapshot: {
        items: [{ todoInstanceId: 'todo-hard', attempts: 3, evidenceRefs: ['artifact-login'], automationState: 'manual_review' }],
      },
    })).toEqual([expect.objectContaining({
      todoInstanceId: 'todo-hard', status: 'blocked', reason: 'login_gate', attempts: 3, evidenceRefs: ['artifact-login'],
    })])
  })

  it('merges repeated attempt signals into the Agent Todo diagnosis', () => {
    expect(todoDiagnostics({
      todoSnapshot: {
        items: [{ todoInstanceId: 'todo-retry', gameId: 'StarRail', title: 'Spend stamina', attempts: 2 }],
      },
      attemptAnalysis: {
        problemSignals: [{
          todoInstanceId: 'todo-retry',
          operation: 'spend_stamina',
          attemptCount: 4,
          blockedCount: 2,
          reviewRequiredCount: 1,
          humanRequiredCount: 3,
          retryableFailureCount: 2,
          latestState: 'blocked',
          latestReasonCode: 'window_lost',
          latestReason: 'The bound game window disappeared.',
          evidenceRefs: ['artifact-window'],
        }],
      },
    })).toEqual([expect.objectContaining({
      todoInstanceId: 'todo-retry',
      title: 'Spend stamina',
      attempts: 4,
      blockedCount: 2,
      humanRequiredCount: 3,
      retryableFailureCount: 2,
      latestState: 'blocked',
      latestReasonCode: 'window_lost',
      reason: 'The bound game window disappeared.',
      evidenceRefs: ['artifact-window'],
    })])
  })

  it('preserves Manager dispatch, attempt, blocker, and assessment projections for Agent display', () => {
    const [diagnostic] = todoDiagnostics({
      todoSnapshot: {
        items: [{
          todoInstanceId: 'todo-context',
          dispatch: {
            disposition: 'deferred_human',
            reason: 'login gate',
            actionAvailability: { execute: false, resume: false, review: true, releaseHuman: true, submitManualEvidence: true, nextAction: 'explicit release' },
          },
          latestTodoAttempt: { todoAttemptId: 'attempt-1', state: 'human_required', reasonCode: 'login_gate' },
          activeBlocker: { blockerId: 'blocker-1', kind: 'human_required', reason: 'login gate' },
          latestAutomationAssessment: { assessmentId: 'assessment-1', difficulty: 'unsupported', automatable: false, issue: 'login gate' },
        }],
      },
    })
    expect(diagnostic).toMatchObject({
      dispatchDisposition: 'deferred_human',
      dispatchReason: 'login gate',
      actionAvailability: { releaseHuman: true, nextAction: 'explicit release' },
      latestTodoAttempt: { todoAttemptId: 'attempt-1', state: 'human_required' },
      activeBlocker: { blockerId: 'blocker-1', kind: 'human_required' },
      latestAutomationAssessment: { assessmentId: 'assessment-1', automatable: false, issue: 'login gate' },
    })
  })

  it('prefers the current frozen reset rule over a different configured baseline', () => {
    const frozen = todo({ gameId: 'FGO', resetRule: { timezone: 'Asia/Shanghai', time: '00:00', cadence: 'daily' } })
    const presentation = resetRulePresentation(
      [frozen],
      [],
      { timezone: 'Asia/Shanghai', time: '04:00', cadence: 'daily' },
    )
    expect(presentation.mode).toBe('frozen')
    expect(presentation.rules).toEqual([{ timezone: 'Asia/Shanghai', time: '00:00', cadence: 'daily' }])
  })

  it('selects the newest Manager-owned AutomationAssessment for the Todo', () => {
    const shared = {
      todoInstanceId: 'todo-1', workItemId: 'work-1', claimId: 'claim-1', decisionId: 'decision-1',
      gameId: 'StarRail', difficulty: 'moderate' as const, confidence: 0.8, basis: [], evidenceIds: [], requestedBy: 'agent',
    }
    expect(latestAutomationAssessment('todo-1', [
      { ...shared, assessmentId: 'old', createdAt: '2026-08-28T05:00:00+08:00' },
      { ...shared, assessmentId: 'new', difficulty: 'hard', createdAt: '2026-08-28T06:00:00+08:00' },
    ])?.assessmentId).toBe('new')
  })
})

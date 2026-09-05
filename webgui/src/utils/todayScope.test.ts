import { describe, expect, it } from 'vitest'
import type { BatchRun, GameState, TodoInstance } from '../api/contracts'
import { buildTodayScope, todayScopePeriodLabel, withFrozenBatchScope } from './todayScope'

function game(gameId: string, enabled = true): GameState {
  return { gameId, displayName: gameId, enabled, runtimeState: 'planned', acceptanceState: 'not_started', reviewState: 'none' }
}

function todo(gameId: string, id: string, day: string, status: TodoInstance['status'], required = true): TodoInstance {
  return {
    todoInstanceId: `${id}-${day}`,
    todoDefinitionId: id,
    definitionVersion: 1,
    catalogVersion: 'test',
    sourceHash: 'hash',
    gameId,
    cadence: 'daily',
    periodKey: `daily:${day}`,
    periodStartsAt: `${day}T04:00:00+08:00`,
    periodEndsAt: `${day}T04:00:00+08:00`,
    operation: id,
    title: id,
    category: 'daily',
    orderIndex: 0,
    required,
    risk: 'routine_action',
    automationDifficulty: 'low',
    automationState: 'ready',
    resetRule: {},
    sourceRefs: [],
    status,
    dispatchDisposition: 'eligible',
    dispatchReason: '',
    actionAvailability: { execute: true, resume: false, review: false, releaseHuman: false, submitManualEvidence: false, nextAction: '' },
    attempts: 0,
    evidenceRefs: [],
    createdAt: `${day}T04:00:00+08:00`,
    updatedAt: `${day}T04:00:00+08:00`,
  }
}

describe('Today scope', () => {
  it('keeps the locked game and date visible while a pre-Adapter snapshot omits Todo details', () => {
    const activeBatch: BatchRun = {
      batchId: 'pgr-dorm-run', cadence: 'daily', state: 'running', gameIds: ['PGR'],
      result: { gameDay: 'batch:2026-09-05:frozen', todoPlans: {} },
    }
    const result = buildTodayScope({
      activeBatch, games: [game('PGR'), game('WW')],
      todos: [todo('PGR', 'dorm', '2026-09-05', 'pending'), todo('WW', 'daily', '2026-09-05', 'completed')],
      selectedTodoDefinitionIds: { PGR: [], WW: ['daily'] },
    })
    expect(result.source).toBe('current_batch')
    expect(result.known).toBe(false)
    expect(result.gameIds).toEqual(['PGR'])
    expect(result.todos).toEqual([])
    expect(result.requiredTotal).toBe(0)
    expect(result.note).toContain('等待同步')
    expect(todayScopePeriodLabel(result.todos, result.periodKeys)).toBe('2026-09-05')
  })

  it('hydrates only the same frozen Todo scope without replacing newer batch state or expanding selection', () => {
    const dorm = todo('PGR', 'dorm', '2026-09-05', 'pending')
    const activeBatch: BatchRun = {
      batchId: 'pgr-dorm-run', cadence: 'daily', state: 'cancelling', gameIds: ['PGR'],
      result: { currentGameId: 'PGR' },
    }
    const detail: BatchRun = {
      ...activeBatch, state: 'running',
      result: { todoScope: { games: [{ gameId: 'PGR', periodKeys: [dorm.periodKey], completionTodoInstanceIds: [dorm.todoInstanceId] }] } },
    }
    const hydrated = withFrozenBatchScope(activeBatch, detail)
    expect(hydrated?.state).toBe('cancelling')
    expect(hydrated?.result?.currentGameId).toBe('PGR')
    const result = buildTodayScope({
      activeBatch: hydrated, games: [game('PGR'), game('WW')],
      todos: [dorm, todo('PGR', 'dorm', '2026-09-04', 'completed'), todo('PGR', 'power', '2026-09-05', 'completed'), todo('WW', 'daily', '2026-09-05', 'blocked')],
      selectedTodoDefinitionIds: { PGR: ['power'], WW: ['daily'] },
    })
    expect(result.known).toBe(true)
    expect(result.gameIds).toEqual(['PGR'])
    expect(result.todos).toEqual([dorm])
    expect(result.requiredTotal).toBe(1)
    expect(result.requiredCompleted).toBe(0)
    expect(result.blocked).toBe(0)
  })

  it('keeps a retained human batch frozen to WW after all seven configured games are restored', () => {
    const ids = ['WW', 'PGR', 'StarRail', 'NIKKE', 'NTE', 'Endfield', 'ZZZ']
    const currentTodos = ids.map((id) => todo(id, 'daily', '2026-09-05', 'pending'))
    const ww = currentTodos[0]!
    const activeBatch: BatchRun = {
      batchId: 'retained-human-ww', cadence: 'daily', state: 'human_required', gameIds: ['WW'],
      result: { currentGameId: 'WW' },
    }
    const detail: BatchRun = {
      ...activeBatch,
      result: { todoScope: { games: [{ gameId: 'WW', periodKeys: [ww.periodKey], completionTodoInstanceIds: [ww.todoInstanceId] }] } },
    }
    const result = buildTodayScope({
      activeBatch: withFrozenBatchScope(activeBatch, detail),
      games: ids.map((id) => game(id)), todos: currentTodos,
      selectedTodoDefinitionIds: Object.fromEntries(ids.map((id) => [id, ['daily']])),
    })
    expect(result.source).toBe('current_batch')
    expect(result.known).toBe(true)
    expect(result.gameIds).toEqual(['WW'])
    expect(result.todos).toEqual([ww])
    expect(result.requiredTotal).toBe(1)
    expect(result.requiredCompleted).toBe(0)
  })

  it('rejects a late detail response from another batch or cadence', () => {
    const activeBatch: BatchRun = { batchId: 'new', cadence: 'daily', state: 'running', gameIds: ['PGR'] }
    expect(withFrozenBatchScope(activeBatch, { ...activeBatch, batchId: 'old' })).toBe(activeBatch)
    expect(withFrozenBatchScope(activeBatch, { ...activeBatch, cadence: 'weekly' })).toBe(activeBatch)
    expect(withFrozenBatchScope(undefined, activeBatch)).toBeUndefined()
  })

  it('summarizes the selected current Todo response without a global game day', () => {
    const todos = Array.from({ length: 7 }, (_, index) => (
      todo('Endfield', `task-${index + 1}`, '2026-09-04', 'pending', index < 3)
    ))
    const result = buildTodayScope({
      games: [game('Endfield')],
      todos,
      selectedTodoDefinitionIds: { Endfield: todos.map((item) => item.todoDefinitionId) },
    })

    expect(result.source).toBe('saved_selection')
    expect(result.known).toBe(true)
    expect(result.gameIds).toEqual(['Endfield'])
    expect(result.todos).toHaveLength(7)
    expect(result.requiredCompleted).toBe(0)
    expect(result.requiredTotal).toBe(3)
    expect(result.blocked).toBe(0)
    expect(todayScopePeriodLabel(result.todos)).toBe('2026-09-04')
  })

  it('keeps current Todos with different reset dates and labels the mixed period', () => {
    const endfield = todo('Endfield', 'daily', '2026-09-04', 'pending')
    const fgo = todo('FGO', 'daily', '2026-09-05', 'pending')
    const result = buildTodayScope({
      games: [game('Endfield'), game('FGO')],
      todos: [endfield, fgo],
      selectedTodoDefinitionIds: { Endfield: ['daily'], FGO: ['daily'] },
    })

    expect(result.known).toBe(true)
    expect(result.todos).toEqual([endfield, fgo])
    expect(todayScopePeriodLabel(result.todos)).toBe('多个当前周期')
    expect(todayScopePeriodLabel([])).toBe('暂无项目')
  })

  it('uses a current batch frozen scope instead of later config changes', () => {
    const frozen = todo('WW', 'frozen', '2026-09-04', 'review_required')
    const unrelated = todo('PGR', 'global-snapshot-item', '2026-09-04', 'blocked')
    const activeBatch: BatchRun = {
      batchId: 'batch-current',
      cadence: 'daily',
      state: 'running',
      gameIds: ['WW'],
      result: {
        gameDay: 'batch:daily:2026-09-04:fixture',
        todoScope: { games: [{ gameId: 'WW', periodKeys: ['daily:2026-09-04'], completionTodoInstanceIds: [frozen.todoInstanceId] }] },
        todoSnapshot: { WW: { instances: [frozen] }, PGR: { instances: [unrelated] } },
      },
    }
    const result = buildTodayScope({
      activeBatch,
      games: [game('WW'), game('PGR')],
      todos: [frozen, todo('PGR', 'new-selection', '2026-09-04', 'blocked')],
      selectedTodoDefinitionIds: { WW: [], PGR: ['new-selection'] },
    })

    expect(result.source).toBe('current_batch')
    expect(result.known).toBe(true)
    expect(result.gameIds).toEqual(['WW'])
    expect(result.todos).toEqual([frozen])
    expect(result.reviewRequired).toBe(1)
    expect(result.blocked).toBe(0)
  })

  it('isolates a frozen batch after its Todo IDs leave the current response', () => {
    const frozen = todo('WW', 'daily', '2026-09-03', 'blocked')
    const current = todo('WW', 'daily', '2026-09-04', 'pending')
    const activeBatch: BatchRun = {
      batchId: 'batch-old', cadence: 'daily', state: 'running', gameIds: ['WW'],
      result: {
        todoScope: { games: [{ gameId: 'WW', periodKeys: ['2026-09-03'], completionTodoInstanceIds: [frozen.todoInstanceId] }] },
        todoSnapshot: { WW: { instances: [frozen] } },
      },
    }
    const result = buildTodayScope({
      activeBatch, games: [game('WW')], todos: [current], selectedTodoDefinitionIds: { WW: ['daily'] },
    })
    expect(result.known).toBe(false)
    expect(result.requiredTotal).toBe(0)
    expect(result.blocked).toBe(0)
    expect(result.note).toContain('隔离')
  })
})

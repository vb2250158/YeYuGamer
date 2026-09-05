import type { BatchRun, GameState, TodoInstance } from '../api/contracts'
import { accountIdOf, accountTargetId } from './gameAccounts'

export type TodayScopeSource = 'current_batch' | 'saved_selection' | 'unknown'

export interface TodayScopeInput {
  activeBatch?: BatchRun | null
  games: GameState[]
  todos: TodoInstance[]
  selectedTodoDefinitionIds: Record<string, string[]>
  enabledAccountIds?: Record<string, string[]>
  selectedTodoDefinitionIdsByTarget?: Record<string, string[]>
}

export interface TodayScopeResult {
  source: TodayScopeSource
  known: boolean
  note: string
  gameIds: string[]
  periodKeys: string[]
  todos: TodoInstance[]
  requiredTotal: number
  requiredCompleted: number
  blocked: number
  reviewRequired: number
}

export function todayScopePeriodLabel(todos: readonly TodoInstance[], frozenPeriodKeys: readonly string[] = []): string {
  const periods = [...new Set((todos.length ? todos.map((todo) => todo.periodKey) : frozenPeriodKeys)
    .map((periodKey) => periodKey?.match(/\d{4}-\d{2}-\d{2}/)?.[0] ?? periodKey)
    .filter(Boolean))]
  if (!periods.length) return '暂无项目'
  if (periods.length > 1) return '多个当前周期'
  return periods[0]
}

/** Snapshot rows omit these immutable details; never merge another batch or stale runtime state. */
export function withFrozenBatchScope(batch: BatchRun | null | undefined, detail: BatchRun | null | undefined): BatchRun | null | undefined {
  if (!batch || !detail || batch.batchId !== detail.batchId || batch.cadence !== detail.cadence) return batch
  return {
    ...batch,
    result: {
      ...batch.result,
      todoScope: detail.result?.todoScope ?? batch.result?.todoScope,
      todoPlans: detail.result?.todoPlans ?? batch.result?.todoPlans,
      accountTargets: batch.result?.accountTargets ?? detail.result?.accountTargets,
    },
  }
}

function selectedGameIds(input: TodayScopeInput): string[] {
  return input.games
    .filter((game) => game.enabled !== false)
    .filter((game) => input.enabledAccountIds?.[game.gameId] && input.selectedTodoDefinitionIdsByTarget
      ? input.enabledAccountIds[game.gameId]!.some((accountId) => (input.selectedTodoDefinitionIdsByTarget![accountTargetId(game.gameId, accountId)] ?? []).length > 0)
      : (input.selectedTodoDefinitionIds[game.gameId] ?? []).length > 0)
    .filter((game) => !input.enabledAccountIds?.[game.gameId] || input.enabledAccountIds[game.gameId]!.length > 0)
    .map((game) => game.gameId)
}

interface FrozenBatchScope {
  gameIds: string[]
  todoInstanceIds: string[]
  periodKeys: string[]
  exact: boolean
}

function frozenBatchScope(batch: BatchRun): FrozenBatchScope {
  const scopeGames = Array.isArray(batch.result?.todoScope?.games) ? batch.result.todoScope.games : []
  if (scopeGames.length) {
    const gameIds = scopeGames.map((item) => item.gameId).filter(Boolean)
    const todoInstanceIds = scopeGames.flatMap((item) => item.completionTodoInstanceIds ?? [])
    const periodKeys = scopeGames.flatMap((item) => item.periodKeys ?? [])
    return { gameIds: [...new Set(gameIds)], todoInstanceIds: [...new Set(todoInstanceIds)], periodKeys: [...new Set(periodKeys)], exact: true }
  }

  const plans = batch.result?.todoPlans
  if (plans && typeof plans === 'object' && Object.keys(plans).length > 0) {
    const entries = Object.entries(plans)
    return {
      gameIds: [...new Set(entries.map(([targetId, plan]) => plan.gameId
        ?? batch.result?.accountTargets?.find((target) => target.targetId === targetId)?.gameId
        ?? targetId))],
      todoInstanceIds: [...new Set(entries.flatMap(([, plan]) => plan.completionTodoInstanceIds ?? []))],
      periodKeys: [...new Set(entries.flatMap(([, plan]) => plan.periodKeys ?? []))],
      exact: entries.length > 0,
    }
  }

  const fallbackGameIds = batch.gameIds ?? []
  const fallbackPeriod = typeof batch.result?.gameDay === 'string'
    ? batch.result.gameDay.match(/\d{4}-\d{2}-\d{2}/)?.[0]
    : undefined
  return { gameIds: [...new Set(fallbackGameIds)], todoInstanceIds: [], periodKeys: fallbackPeriod ? [`daily:${fallbackPeriod}`] : [], exact: false }
}

/**
 * Resolve the only scope that Today may summarize.
 *
 * A live daily batch owns its frozen game/Todo scope. Without a live batch,
 * the saved enabled + selected configuration owns the scope. The global Todo
 * overview and historical blockers are deliberately not inputs here.
 */
export function buildTodayScope(input: TodayScopeInput): TodayScopeResult {
  const selectedIds = selectedGameIds(input)
  const dailyBatch = input.activeBatch?.cadence === 'daily' ? input.activeBatch : undefined
  const frozen = dailyBatch ? frozenBatchScope(dailyBatch) : undefined
  const useBatch = Boolean(dailyBatch && frozen?.gameIds.length)
  const gameIds = useBatch ? frozen?.gameIds ?? [] : selectedIds
  const source: TodayScopeSource = useBatch ? 'current_batch' : 'saved_selection'
  const frozenTodoIdSet = new Set(frozen?.todoInstanceIds ?? [])
  const currentTodoIdSet = new Set(input.todos.map((todo) => todo.todoInstanceId))
  const frozenTodosAreCurrent = [...frozenTodoIdSet].every((todoId) => currentTodoIdSet.has(todoId))
  if (dailyBatch && (!useBatch || !frozen?.exact || !frozenTodoIdSet.size)) {
    return {
      source: useBatch ? 'current_batch' : 'unknown',
      known: false,
      note: '本次已锁定游戏范围，冻结项目明细等待同步；未确认的 Todo 不计入进度。',
      gameIds: frozen?.gameIds ?? [],
      periodKeys: frozen?.periodKeys ?? [],
      todos: [],
      requiredTotal: 0,
      requiredCompleted: 0,
      blocked: 0,
      reviewRequired: 0,
    }
  }
  if (dailyBatch && !frozenTodosAreCurrent) {
    return {
      source: 'current_batch',
      known: false,
      note: '活动批次的冻结 Todo 已不在当前周期，已从今日计数中隔离。',
      gameIds: frozen?.gameIds ?? [],
      periodKeys: frozen?.periodKeys ?? [],
      todos: [],
      requiredTotal: 0,
      requiredCompleted: 0,
      blocked: 0,
      reviewRequired: 0,
    }
  }

  const gameIdSet = new Set(gameIds)
  const selectedByGame = input.selectedTodoDefinitionIds
  const todos = input.todos.filter((todo) => {
    if (todo.cadence !== 'daily' || !gameIdSet.has(todo.gameId)) return false
    if (useBatch && frozen?.exact) return frozenTodoIdSet.has(todo.todoInstanceId)
    const enabledAccounts = input.enabledAccountIds?.[todo.gameId]
    if (enabledAccounts && !enabledAccounts.includes(accountIdOf(todo))) return false
    if (enabledAccounts && input.selectedTodoDefinitionIdsByTarget) {
      return (input.selectedTodoDefinitionIdsByTarget[accountTargetId(todo.gameId, accountIdOf(todo))] ?? []).includes(todo.todoDefinitionId)
    }
    return (selectedByGame[todo.gameId] ?? []).includes(todo.todoDefinitionId)
  })
  const allFrozenTodosLoaded = !useBatch || !frozen?.exact || frozenTodoIdSet.size === todos.length
  const known = !dailyBatch || Boolean(frozen?.exact && frozenTodoIdSet.size && frozenTodosAreCurrent && allFrozenTodosLoaded)
  const required = todos.filter((todo) => todo.required)
  const blocked = required.filter((todo) => ['blocked', 'human_required'].includes(todo.status)).length
  const reviewRequired = required.filter((todo) => todo.status === 'review_required').length

  let note = useBatch
    ? '按当前批次冻结的游戏与 Todo 范围统计。'
    : '按当前游戏日中已启用且已选择的 Todo 统计。'
  if (dailyBatch && !allFrozenTodosLoaded) note = '当前 Todo 明细尚未覆盖批次冻结范围，计数标记为待确认。'

  return {
    source,
    known,
    note,
    gameIds,
    periodKeys: [...new Set(todos.map((todo) => todo.periodKey))],
    todos,
    requiredTotal: required.length,
    requiredCompleted: required.filter((todo) => todo.status === 'completed').length,
    blocked,
    reviewRequired,
  }
}

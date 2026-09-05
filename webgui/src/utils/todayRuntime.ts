import type { GameState, TodoInstance } from '../api/contracts'

const runtimeDisplayStates = new Set([
  'planned', 'pending_execution', 'queued', 'preflight', 'launching', 'attaching',
  'observing', 'running', 'executing', 'verifying', 'reconciling', 'cancelling',
  'terminal', 'done', 'partial', 'retry_wait', 'blocked_technical', 'human_takeover',
  'human_required', 'review_required', 'blocked', 'cancelled', 'crashed', 'failed',
])
const issueStates = new Set(['failed', 'crashed', 'blocked_technical', 'retry_wait', 'partial', 'blocked'])
const humanGateStates = new Set(['human_required', 'human_takeover'])
const activeStates = new Set(['preflight', 'launching', 'attaching', 'observing', 'running', 'executing', 'verifying', 'reconciling'])
const reviewDisplayStates = new Set(['none', 'agent_queued', 'agent_in_progress', 'approval_required', 'human_required', 'resolved'])

/** Scope terminal problems to the Manager's current per-game Todo windows. */
export function todayRuntimeState(game: GameState, todos: readonly TodoInstance[]): string {
  const state = game.runtimeState
  // A runtime terminal must not borrow the Todo badge's "completed" wording.
  if (state === 'completed') return 'terminal'
  if (!runtimeDisplayStates.has(state)) return 'unknown'
  if (!issueStates.has(state) && !humanGateStates.has(state)) return state

  const updatedAt = Date.parse(game.updatedAt ?? '')
  const windows = todos.filter((todo) => todo.gameId === game.gameId && todo.cadence === 'daily')
    .map((todo) => [Date.parse(todo.periodStartsAt), Date.parse(todo.periodEndsAt)])
    .filter(([start, end]) => Number.isFinite(start) && Number.isFinite(end) && start < end)
  if (!Number.isFinite(updatedAt) || windows.length === 0) return 'unknown'
  return windows.some(([start, end]) => updatedAt >= start && updatedAt < end) ? state : 'not_started'
}

export function todayRuntimeNextAction(state: string): string | undefined {
  if (activeStates.has(state)) return '等待自动执行完成'
  if (state === 'cancelling') return '等待安全停止完成'
  if (state === 'failed' || state === 'crashed') return '查看本次失败详情，处理后重试'
  if (issueStates.has(state)) return '查看本次问题并按顶部提示处理'
  if (state === 'human_required' || state === 'human_takeover') return '查看阻塞原因，处理后释放接管'
  if (state === 'review_required') return '确认当前结果后继续'
  if (state === 'queued' || state === 'pending_execution') return '等待队列调度'
  if (state === 'done' || state === 'terminal') return '等待完成确认'
  if (state === 'cancelled') return '已停止，可重新开始'
  return undefined
}

export function todayRuntimeIsIssue(state: string): boolean {
  return issueStates.has(state)
}

export function todayGameSituationState(game: GameState, todos: readonly TodoInstance[]): string {
  const runtime = todayRuntimeState(game, todos)
  // A technical stop can also queue an evidence review; it is not an approval gate.
  if (todayRuntimeIsIssue(runtime)) return runtime
  if (humanGateStates.has(runtime)) return runtime
  if (humanGateStates.has(game.runtimeState)) return runtime === 'not_started' ? 'none' : 'unknown'
  return reviewDisplayStates.has(game.reviewState) ? game.reviewState : 'unknown'
}

export function todayGameNextAction(game: GameState, todos: readonly TodoInstance[]): string {
  if (game.acceptanceState === 'accepted_done') return '今天已经完成'
  const runtime = todayRuntimeState(game, todos)
  if (todayRuntimeIsIssue(runtime)) return todayRuntimeNextAction(runtime)!
  if (humanGateStates.has(runtime)) return todayRuntimeNextAction(runtime)!
  if (humanGateStates.has(game.runtimeState)) return runtime === 'not_started' ? '等待开始' : '等待当前周期状态同步'
  if (todos.some((todo) => todo.status === 'human_required')) return '查看阻塞原因，处理后释放接管'
  if (game.acceptanceState === 'evidence_pending' || game.reviewState === 'agent_queued' || game.reviewState === 'agent_in_progress') return '确认本次完成截图'
  if (todos.some((todo) => todo.status === 'review_required') || game.reviewState === 'approval_required' || game.reviewState === 'human_required') return '确认当前结果后继续'
  const runtimeNextAction = todayRuntimeNextAction(runtime)
  if (runtimeNextAction) return runtimeNextAction
  if (todos.some((todo) => todo.status === 'blocked')) return '查看本次问题并按顶部提示处理'
  if (todos.some((todo) => todo.status === 'completed')) return '等待完成确认'
  return '等待开始'
}

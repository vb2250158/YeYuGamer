import { recordOf, type BatchRun, type RunAttempt } from '../api/contracts'

export interface QueueCleanupTarget {
  batchId: string
  runId: string
  attemptId: string
  gameId: string
  candidateGameIds: string[]
}

function uniqueStrings(value: unknown): string[] | undefined {
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string' && item.length > 0)) return undefined
  return new Set(value).size === value.length ? value : undefined
}

function sameScope(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id) => right.includes(id))
}

/** Only the displayed batch's active member or explicit human/resume target owns this view. */
export function queueCleanupTarget(batch?: BatchRun): QueueCleanupTarget | undefined {
  if (!batch || batch.mode !== 'execute' || batch.result?.sealVersion != null) return undefined
  const gameId = batch.currentGameId ?? batch.result?.currentGameId
  const candidateGameIds = uniqueStrings(batch.result?.candidateGameIds)
  if (!gameId || !candidateGameIds?.includes(gameId)) return undefined
  const members = (batch.runMemberships ?? []).filter((member) => member.batchId === batch.batchId)
  let candidates = members.filter((member) => member.state === 'active')
  if (candidates.length === 0) {
    const actions = batch.result?.batchActionAvailability
    const targets = [
      ...(actions?.humanTakeover === true ? actions.humanTakeoverTargets ?? [] : []),
      ...(actions?.runResume === true ? actions.runResumeTargets ?? [] : []),
    ].filter((target) => target.gameId === gameId)
    candidates = members.filter((member) => targets.some((target) => target.runId === member.runId))
  }
  if (candidates.length !== 1 || !candidates[0]?.latestRunAttemptId) return undefined
  const member = candidates[0]
  return { batchId: batch.batchId, runId: member.runId, attemptId: member.latestRunAttemptId!, gameId, candidateGameIds }
}

export interface QueueCleanupView {
  title: string
  summary: string
  inProgress: boolean
}

/** Cleanup receipts describe process closure only; they never imply daily completion. */
export function queueCleanupView(
  target: QueueCleanupTarget | undefined,
  attempt: RunAttempt | undefined,
  displayNames: Record<string, string>,
): QueueCleanupView | undefined {
  if (!target || !attempt || attempt.runAttemptId !== target.attemptId
    || attempt.runId !== target.runId || attempt.gameId !== target.gameId) return undefined
  const report = recordOf(attempt.result.queueGameCleanup)
  const scope = uniqueStrings(report.candidateGameIds)
  const targets = uniqueStrings(report.targetGameIds)
  if (report.batchId !== target.batchId || report.currentGameId !== target.gameId
    || !scope || !sameScope(scope, target.candidateGameIds)
    || !targets || !sameScope(targets, scope.filter((id) => id !== target.gameId))
    || !Array.isArray(report.games) || targets.length === 0) return undefined
  const states = ['checking', 'closing', 'closed', 'not-needed', 'human_required', 'cancelled', 'failed']
  if (typeof report.state !== 'string' || !states.includes(report.state)) return undefined
  const games = report.games.map(recordOf)
  const seen = new Set<string>()
  const labels: string[] = []
  let incomplete = false
  for (const game of games) {
    if (typeof game.gameId !== 'string' || !targets.includes(game.gameId) || seen.has(game.gameId)) return undefined
    seen.add(game.gameId)
    const name = displayNames[game.gameId] ?? game.gameId
    const residuals = [game.remainingProcessIds, game.zombieProcessIds, game.unverifiedProcessIds]
    const verifiedArrays = residuals.every((ids) => Array.isArray(ids) && ids.every((id) => Number.isInteger(id) && id > 0))
    const hasResiduals = residuals.some((ids) => Array.isArray(ids) && ids.length > 0)
    if (hasResiduals) {
      labels.push(`${name} 仍有残留`)
      incomplete = true
    } else if (verifiedArrays && game.state === 'closed') {
      labels.push(`${name} 已关闭`)
    } else if (verifiedArrays && game.state === 'already-closed') {
      labels.push(`${name} 无需关闭`)
    } else {
      labels.push(`${name} 未能确认关闭`)
      incomplete = true
    }
  }
  const pending = targets.filter((id) => !seen.has(id))
  const inProgress = ['checking', 'closing'].includes(report.state) && attempt.completedAt == null
  if (pending.length) labels.push(`${pending.map((id) => displayNames[id] ?? id).join('、')} ${inProgress ? '等待处理' : '尚未处理'}`)
  let title = '启动前清理记录'
  if (inProgress) title = '启动前正在关闭其他游戏'
  else if (report.state === 'cancelled') title = '启动前清理已停止'
  else if (report.state === 'human_required' || report.state === 'failed') title = '启动前清理未完成，已暂停启动'
  else if (incomplete || pending.length) title = '启动前清理结果待确认'
  else if (report.state === 'closed') title = '启动前清理已结束'
  return { title, summary: labels.join('；'), inProgress }
}

import { describe, expect, it } from 'vitest'
import type { BatchRun, RunAttempt } from '../api/contracts'
import { queueCleanupTarget, queueCleanupView } from './queueCleanup'

function batch(): BatchRun {
  return {
    batchId: 'batch', mode: 'execute', state: 'running',
    result: { currentGameId: 'WW', candidateGameIds: ['WW', 'PGR', 'GF2'] },
    runMemberships: [{ batchId: 'batch', runId: 'run', state: 'active', latestRunAttemptId: 'attempt' }],
  }
}
const names = { WW: '鸣潮', PGR: '战双', GF2: '少女前线2' }
function attempt(state = 'closing', games: unknown[] = []): RunAttempt {
  return {
    runAttemptId: 'attempt', runId: 'run', gameId: 'WW', cadence: 'daily', state: 'running',
    executableTodoInstanceIds: [], startedAt: '', createdAt: '', updatedAt: '',
    result: { queueGameCleanup: { batchId: 'batch', currentGameId: 'WW', candidateGameIds: ['WW', 'PGR', 'GF2'], targetGameIds: ['PGR', 'GF2'], state, games } },
  }
}
const closed = { gameId: 'GF2', state: 'closed', remainingProcessIds: [], zombieProcessIds: [], unverifiedProcessIds: [] }

describe('current attempt queue cleanup presentation', () => {
  it('shows progress and all remaining frozen targets without inferring daily completion', () => {
    expect(queueCleanupView(queueCleanupTarget(batch()), attempt('closing', [closed]), names)).toEqual({
      title: '启动前正在关闭其他游戏', summary: '少女前线2 已关闭；战双 等待处理', inProgress: true,
    })
  })

  it('shows successful closures alongside residuals when startup pauses', () => {
    const receipt = attempt('human_required', [{ ...closed, gameId: 'PGR', state: 'close-failed', remainingProcessIds: [42] }, closed])
    receipt.completedAt = '2026-09-05T20:00:00Z'
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)).toEqual({
      title: '启动前清理未完成，已暂停启动', summary: '战双 仍有残留；少女前线2 已关闭', inProgress: false,
    })
  })

  it('keeps the exact human or released resume member and rejects its superseded attempt', () => {
    const current = batch()
    current.runMemberships![0]!.state = 'terminal'
    current.result!.batchActionAvailability = {
      resume: false, cancel: true, review: false, humanTakeover: true,
      humanTakeoverTargets: [{ runId: 'run', gameId: 'WW' }], nextAction: '', reason: '', reasonCode: '',
    }
    expect(queueCleanupTarget(current)?.attemptId).toBe('attempt')
    current.result!.batchActionAvailability.humanTakeover = false
    current.result!.batchActionAvailability.runResume = true
    current.result!.batchActionAvailability.runResumeTargets = [{ runId: 'run', gameId: 'WW' }]
    expect(queueCleanupTarget(current)?.attemptId).toBe('attempt')
    current.runMemberships![0]!.latestRunAttemptId = 'next-attempt'
    expect(queueCleanupView(queueCleanupTarget(current), attempt(), names)).toBeUndefined()
  })

  it('does not borrow a historical member, sealed batch, or unrelated response', () => {
    const current = batch()
    current.runMemberships![0]!.state = 'terminal'
    expect(queueCleanupTarget(current)).toBeUndefined()
    current.runMemberships![0]!.state = 'active'
    current.result!.sealVersion = 1
    expect(queueCleanupTarget(current)).toBeUndefined()
    const receipt = attempt()
    receipt.runId = 'other-run'
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)).toBeUndefined()
  })

  it('rejects expanded report scope, duplicate games and unrelated game entries', () => {
    const receipt = attempt('closed', [closed])
    const report = receipt.result.queueGameCleanup as Record<string, unknown>
    report.candidateGameIds = ['WW', 'PGR', 'GF2', 'NTE']
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)).toBeUndefined()
    report.candidateGameIds = ['WW', 'PGR', 'GF2']
    report.games = [closed, closed]
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)).toBeUndefined()
    report.games = [{ ...closed, gameId: 'NTE' }]
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)).toBeUndefined()
  })

  it('never calls missing receipt fields or unverified PIDs closed, even under a closed aggregate', () => {
    const receipt = attempt('closed', [{ gameId: 'PGR', state: 'closed' }, { ...closed, unverifiedProcessIds: [42] }])
    const view = queueCleanupView(queueCleanupTarget(batch()), receipt, names)
    expect(view?.summary).toBe('战双 未能确认关闭；少女前线2 仍有残留')
    expect(view?.title).toBe('启动前清理结果待确认')
  })

  it('does not leave a cancelled or completed attempt labeled as actively closing', () => {
    const receipt = attempt('cancelled', [closed])
    expect(queueCleanupView(queueCleanupTarget(batch()), receipt, names)?.title).toBe('启动前清理已停止')
    const stopped = attempt()
    stopped.completedAt = '2026-09-05T20:00:00Z'
    expect(queueCleanupView(queueCleanupTarget(batch()), stopped, names)?.inProgress).toBe(false)
  })
})

import { describe, expect, it } from 'vitest'
import type { GameState, TodoInstance } from '../api/contracts'
import { humanize, statusTone } from './format'
import { todayGameNextAction, todayGameSituationState, todayRuntimeNextAction, todayRuntimeState } from './todayRuntime'

const currentTodo = {
  gameId: 'WW',
  cadence: 'daily',
  periodKey: 'daily:2026-09-05',
  periodStartsAt: '2026-09-05T04:00:00+08:00',
  periodEndsAt: '2026-09-06T04:00:00+08:00',
  status: 'pending',
  runId: null,
} as TodoInstance

function game(runtimeState: string, updatedAt = '2026-09-05T15:03:51+08:00'): GameState {
  return { gameId: 'WW', displayName: 'WW', runtimeState, updatedAt, acceptanceState: 'not_started', reviewState: 'none' }
}

describe('Today runtime presentation', () => {
  it('prioritizes a pre-Adapter human gate with pending Todos over evidence review', () => {
    const gate = { ...game('human_required'), acceptanceState: 'evidence_pending', reviewState: 'human_required' }
    expect(todayGameSituationState(gate, [currentTodo])).toBe('human_required')
    expect(todayGameNextAction(gate, [currentTodo])).toBe('查看阻塞原因，处理后释放接管')
    expect(todayGameNextAction(gate, [currentTodo])).not.toContain('截图')
  })

  it('does not carry an old human gate into the current Todo window', () => {
    const old = { ...game('human_required', '2026-09-05T03:59:59+08:00'), acceptanceState: 'evidence_pending', reviewState: 'human_required' }
    expect(todayRuntimeState(old, [currentTodo])).toBe('not_started')
    expect(todayGameSituationState(old, [currentTodo])).toBe('none')
    expect(todayGameNextAction(old, [currentTodo])).toBe('等待开始')
  })
  it.each(['blocked', 'failed', 'crashed'])('shows current %s before queued evidence review', (state) => {
    const stopped = { ...game(state), acceptanceState: 'evidence_pending', reviewState: 'approval_required' }
    expect(todayGameSituationState(stopped, [currentTodo])).toBe(state)
    expect(todayGameNextAction(stopped, [currentTodo])).toBe(todayRuntimeNextAction(state))
    expect(todayGameNextAction(stopped, [currentTodo])).not.toContain('截图')
    expect(stopped.reviewState).toBe('approval_required')
    expect(stopped.acceptanceState).toBe('evidence_pending')
  })

  it('keeps actual review and accepted completion wording when execution has no current issue', () => {
    const review = { ...game('terminal'), acceptanceState: 'evidence_pending', reviewState: 'approval_required' }
    expect(todayGameSituationState(review, [currentTodo])).toBe('approval_required')
    expect(todayGameNextAction(review, [currentTodo])).toBe('确认本次完成截图')
    expect(todayGameNextAction({ ...review, acceptanceState: 'accepted_done' }, [currentTodo])).toBe('今天已经完成')
  })

  it('shows a current startup failure before any Todo has started', () => {
    const failed = game('failed')
    const state = todayRuntimeState(failed, [currentTodo])
    expect(state).toBe('failed')
    expect(humanize(state)).toBe('失败')
    expect(statusTone(state)).toBe('danger')
    expect(todayRuntimeNextAction(state)).toBe('查看本次失败详情，处理后重试')
    expect(failed.acceptanceState).toBe('not_started')
    expect(currentTodo.status).toBe('pending')
  })

  it.each(['failed', 'crashed', 'blocked', 'blocked_technical', 'retry_wait', 'partial'])(
    'keeps a previous-period %s out of Today', (runtimeState) => {
      const state = todayRuntimeState(game(runtimeState, '2026-09-05T03:59:59+08:00'), [currentTodo])
      expect(state).toBe('not_started')
      expect(todayRuntimeNextAction(state)).toBeUndefined()
    },
  )

  it('uses the Manager period boundaries rather than calendar midnight', () => {
    expect(todayRuntimeState(game('failed', '2026-09-05T20:00:00Z'), [currentTodo])).toBe('not_started')
    expect(todayRuntimeState(game('failed', '2026-09-05T00:00:00+00:00'), [currentTodo])).toBe('failed')
    const customWindow = { ...currentTodo, periodStartsAt: '2026-09-05T06:00:00+08:00' }
    expect(todayRuntimeState(game('failed', '2026-09-05T05:00:00+08:00'), [customWindow])).toBe('not_started')
  })

  it('does not use another game window or an invalid timestamp to claim a current failure', () => {
    expect(todayRuntimeState(game('failed'), [{ ...currentTodo, gameId: 'PGR' }])).toBe('unknown')
    expect(todayRuntimeState(game('failed', 'invalid'), [currentTodo])).toBe('unknown')
    expect(todayRuntimeState(game('failed'), [])).toBe('unknown')
  })

  it.each([
    ['running', '运行中', '等待自动执行完成'],
    ['queued', '排队中', '等待队列调度'],
    ['pending_execution', '等待执行', '等待队列调度'],
    ['cancelling', '正在安全停止', '等待安全停止完成'],
    ['human_required', '需要人工', '查看阻塞原因，处理后释放接管'],
    ['review_required', '待复核', '确认当前结果后继续'],
  ])('displays Manager state %s without falling back to unknown', (runtimeState, label, action) => {
    const state = todayRuntimeState(game(runtimeState), [currentTodo])
    expect(state).toBe(runtimeState)
    expect(humanize(state)).toBe(label)
    expect(todayRuntimeNextAction(state)).toBe(action)
  })

  it.each(['done', 'completed'])('keeps runtime %s separate from accepted completion', (runtimeState) => {
    const state = todayRuntimeState(game(runtimeState), [currentTodo])
    expect(state).not.toBe('unknown')
    expect(state).not.toBe('accepted_done')
    expect(todayRuntimeNextAction(state)).toBe('等待完成确认')
  })

  it('keeps unknown Manager states visibly unknown', () => {
    expect(todayRuntimeState(game('future-state'), [currentTodo])).toBe('unknown')
  })
})

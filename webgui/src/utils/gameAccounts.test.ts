import { describe, expect, it } from 'vitest'
import type { AccountTarget, BatchRun, CompletionContractDecision, GameAccount, GameDetail, GameRunRecord, GameState, RunAttempt, TodoAttempt, TodoInstance } from '../api/contracts'
import { accountBindingLabel, accountCompletionEvidence, accountGameState, accountRunLocation, accountTodos, configuredGameAccounts, gameAccountsPatch, loadedAttemptCounts, moveGameAccount, reviewRunContextError, scopeGameDetailToAccount, todayAccountTargets } from './gameAccounts'
import { currentStepEvidence } from './todayEvidence'
import { buildTodayScope } from './todayScope'

const day = 'daily:2026-09-05'
const game: GameState = { gameId: 'WW', displayName: '鸣潮', enabled: true, runtimeState: 'completed', acceptanceState: 'accepted_done', reviewState: 'none', runId: 'run-A' }
const accounts: GameAccount[] = [
  { account_id: 'default', label: 'A', enabled: true, saved_account_label: 'saved-A' },
  { account_id: 'B', label: 'B', enabled: true, saved_account_label: 'saved-B' },
]
function todo(accountId = 'default', status: TodoInstance['status'] = 'pending', periodKey = day): TodoInstance {
  return { gameId: 'WW', accountId, todoInstanceId: `${accountId}-${periodKey}`, todoDefinitionId: 'daily', cadence: 'daily',
    periodKey, required: true, status, runId: accountId === 'default' ? 'run-A' : `run-${accountId}`, updatedAt: '2026-09-05T12:00:00Z' } as TodoInstance
}
function run(accountId = 'default', acceptedDone = true): GameRunRecord {
  const runId = accountId === 'default' ? 'run-A' : `run-${accountId}`
  return { runId, gameId: 'WW', accountId, state: 'completed', updatedAt: '2026-09-05T12:00:00Z',
    accountSnapshot: { label: `冻结${accountId}`, saved_account_label: `saved-${accountId}` },
    completionContract: { gameId: 'WW', accountId, runId, gameDayKey: day, acceptedDone,
      outcome: acceptedDone ? 'accepted_done' : 'review_required' } as CompletionContractDecision }
}
const target = (accountId = 'default'): AccountTarget => ({ targetId: accountId === 'default' ? 'WW' : `WW::${accountId}`, gameId: 'WW', accountId, accountLabel: accountId })

describe('WW account configuration', () => {
  it('keeps the implicit original account when adding the first new row', () => {
    const original = configuredGameAccounts({}, 'WW')
    expect(original).toEqual([{ account_id: 'default', label: '当前账号', enabled: true, saved_account_label: '' }])
    const patch = gameAccountsPatch([...original, { label: '新账号', enabled: true, saved_account_label: '' }])
    expect(patch[0]?.account_id).toBe('default')
    expect(Object.hasOwn(patch[1]!, 'account_id')).toBe(false)
  })

  it('round-trips server-assigned identities and list order after saving', () => {
    const newRow = { label: 'C', enabled: true, saved_account_label: 'saved-C' }
    const patch = gameAccountsPatch(moveGameAccount([...accounts, newRow], 2, -1))
    expect(patch.map((item) => item.label)).toEqual(['A', 'C', 'B'])
    expect(patch.map((item) => item.account_id)).toEqual(['default', undefined, 'B'])
    const saved = configuredGameAccounts({ game_accounts: { WW: patch.map((item) => ({ ...item, account_id: item.account_id ?? 'server-C' })) } }, 'WW')
    expect(gameAccountsPatch(saved).map((item) => item.account_id)).toEqual(['default', 'server-C', 'B'])
    expect(accounts.map((item) => item.account_id)).toEqual(['default', 'B'])
  })

  it('keeps disabled accounts and identity when editing labels or order', () => {
    const edited = [{ ...accounts[0]!, enabled: false, label: ' A renamed ' }, accounts[1]!]
    const patch = gameAccountsPatch(moveGameAccount(edited, 0, 1))
    expect(patch.map((item) => item.account_id)).toEqual(['B', 'default'])
    expect(patch[1]).toMatchObject({ label: 'A renamed', enabled: false })
    expect(moveGameAccount(edited, 0, -1)).toEqual(edited)
  })

  it('distinguishes an unbound selector from a verified account switch', () => {
    expect(accountBindingLabel({ ...accounts[0]!, saved_account_label: ' ' })).toBe('待绑定')
    expect(accountBindingLabel(accounts[0]!)).toBe('已填写匹配标签，执行时核对')
    expect(accountBindingLabel({ ...accounts[1]!, enabled: false })).toBe('已停用')
  })

  it('does not invent default identities for malformed saved rows or empty explicit lists', () => {
    expect(configuredGameAccounts({ game_accounts: { WW: [] } }, 'WW')).toEqual([])
    const malformed = configuredGameAccounts({ game_accounts: { WW: [{ label: 'unknown', enabled: true }] } }, 'WW')
    expect(todayAccountTargets('WW', malformed)).toEqual([])
  })
})

describe('account-scoped Today progress', () => {
  it('does not let completed account A or game aggregate accept B', () => {
    const todos = [todo('default', 'completed'), todo('B')]
    expect(accountGameState(game, target(), todos, [run()]).acceptanceState).toBe('accepted_done')
    const b = accountGameState(game, target('B'), todos, [run()])
    expect(b.acceptanceState).toBe('not_started')
    expect(b.runId).toBeUndefined()
    expect(b.runtimeState).toBe('planned')
  })

  it('requires each account own completion decision, not merely completed Todos or exit success', () => {
    const todos = [todo('B', 'completed')]
    const withoutDecision = { ...run('B'), completionContract: undefined }
    expect(accountGameState(game, target('B'), todos, [withoutDecision]).acceptanceState).toBe('not_started')
    expect(accountGameState(game, target('B'), todos, [run('B', false)]).acceptanceState).toBe('evidence_pending')
    expect(accountGameState(game, target('B'), todos, [run('B')]).acceptanceState).toBe('accepted_done')
  })

  it.each(['account', 'game', 'run', 'day'])('rejects a mismatched completion %s identity', (field) => {
    const record = run('B')
    record.completionContract = { ...record.completionContract!, ...({ account: { accountId: 'default' }, game: { gameId: 'PGR' }, run: { runId: 'other' }, day: { gameDayKey: 'daily:2026-09-04' } }[field]) }
    expect(accountGameState(game, target('B'), [todo('B', 'completed')], [record]).acceptanceState).toBe('not_started')
  })

  it('requires explicit Run identity on the frozen target even when another account Run is newer', () => {
    const record = run('B')
    expect(accountGameState(game, { ...target('B'), runId: 'expected' }, [todo('B')], [record]).acceptanceState).toBe('not_started')
  })

  it('uses a frozen target decision with a matching account, Run and period', () => {
    const record = run('B')
    const frozen = { ...target('B'), runId: record.runId, completionContract: record.completionContract }
    expect(accountGameState(game, frozen, [todo('B', 'completed')], []).acceptanceState).toBe('accepted_done')
  })

  it('isolates old evidence after reset or rollover', () => {
    expect(accountGameState(game, target('B'), [{ ...todo('B'), runId: undefined }], [run('B')]).acceptanceState).toBe('not_started')
    expect(accountGameState(game, target('B'), [todo('B', 'pending', 'daily:2026-09-06')], [run('B')]).acceptanceState).toBe('not_started')
  })

  it('maps missing legacy accountId only to default, never B', () => {
    const legacy = { ...todo(), accountId: undefined }
    expect(accountTodos([legacy], 'WW', 'default')).toEqual([legacy])
    expect(accountTodos([legacy], 'WW', 'B')).toEqual([])
    const oldRun = { ...run(), accountId: undefined, completionContract: { ...run().completionContract!, accountId: undefined } }
    expect(accountGameState(game, target(), [legacy], [oldRun]).acceptanceState).toBe('accepted_done')
  })

  it('retains frozen alias, order and disabled targets despite later config edits', () => {
    const frozen = [{ ...target('B'), accountLabel: 'Old B', runId: 'run-B' }, { ...target(), accountLabel: 'Old A' }]
    const batch: BatchRun = { batchId: 'batch', cadence: 'daily', state: 'running', gameIds: ['WW'], result: { accountTargets: frozen } }
    expect(todayAccountTargets('WW', [], batch)).toEqual(frozen)
    expect(todayAccountTargets('WW', accounts.map((account) => ({ ...account, enabled: false })), batch)).toEqual(frozen)
    expect(todayAccountTargets('WW', accounts, { ...batch, result: {} }).map((item) => item.accountId)).toEqual(['default'])
  })

  it('excludes disabled B from saved scope while counting one shared definition once per enabled account', () => {
    const todos = [todo('default', 'completed'), todo('B')]
    const input = { games: [game], todos, selectedTodoDefinitionIds: { WW: ['daily'] } }
    const both = buildTodayScope({ ...input, enabledAccountIds: { WW: ['default', 'B'] } })
    expect([both.requiredCompleted, both.requiredTotal]).toEqual([1, 2])
    expect(buildTodayScope({ ...input, enabledAccountIds: { WW: ['default'] } }).todos).toEqual([todos[0]])
    expect(buildTodayScope({ ...input, enabledAccountIds: { WW: [] } }).gameIds).toEqual([])
  })

  it('resolves internal target plan keys to gameId without expanding frozen Todo scope', () => {
    const b = todo('B')
    const batch: BatchRun = { batchId: 'batch', cadence: 'daily', state: 'running', gameIds: ['WW'], result: {
      accountTargets: [target('B')], todoPlans: { 'WW::B': { gameId: 'WW', accountId: 'B', completionTodoInstanceIds: [b.todoInstanceId], periodKeys: [day] } },
    } }
    const scope = buildTodayScope({ activeBatch: batch, games: [game], todos: [todo('default', 'completed'), b], selectedTodoDefinitionIds: { WW: [] }, enabledAccountIds: { WW: [] } })
    expect(scope.gameIds).toEqual(['WW'])
    expect(scope.todos).toEqual([b])
    expect([scope.requiredCompleted, scope.requiredTotal]).toEqual([0, 1])
  })

  it('shows no A step screenshots when opening B', () => {
    const a = todo()
    const artifacts = [{ artifactId: 'a', gameId: 'WW', runId: a.runId!, todoInstanceId: a.todoInstanceId,
      gameDayKey: day, todoAttemptId: 'attempt-a', contentType: 'image/png', kind: 'game-ui-step-after-watermarked' }]
    expect(currentStepEvidence(artifacts, a)).toHaveLength(1)
    expect(currentStepEvidence(artifacts, todo('B'))).toEqual([])
  })

  it('shows only the account own sealed completion pair', () => {
    const record = run()
    record.completionContract!.screenshotArtifactRefs = ['raw-A', 'marked-A']
    const base = { gameId: 'WW', runId: 'run-A', gameDayKey: day, contentType: 'image/png', todoAttemptId: 'attempt-A' }
    const artifacts = [{ ...base, artifactId: 'raw-A', kind: 'game-ui-daily-reward-raw', raw: true },
      { ...base, artifactId: 'marked-A', kind: 'game-ui-daily-reward-watermarked', raw: false }]
    const todos = [todo('default', 'completed'), todo('B')]
    expect(accountCompletionEvidence(game, target(), todos, [record], artifacts)).toEqual(artifacts)
    expect(accountCompletionEvidence(game, target('B'), todos, [record], artifacts)).toEqual([])
  })
})

describe('account Run details', () => {
  const mixed: GameDetail = { ...game, todoSummary: undefined, todoInstances: [todo('default', 'completed'), todo('B')],
    artifacts: [{ artifactId: 'a', gameId: 'WW', runId: 'run-A' }, { artifactId: 'b', gameId: 'WW', runId: 'run-B' }],
    controllerLease: { runId: 'run-A' }, windowBinding: { runId: 'run-A' } }

  it('uses the exact Run account and frozen label, and removes another account evidence and controls', () => {
    const result = scopeGameDetailToAccount(mixed, 'B', run('B', false))
    expect(result.displayName).toBe('鸣潮 · 冻结B')
    expect(result.todoInstances?.map((item) => item.accountId)).toEqual(['B'])
    expect(result.artifacts?.map((item) => item.artifactId)).toEqual(['b'])
    expect(result.controllerLease).toBeUndefined()
    expect(result.windowBinding).toBeUndefined()
    expect(result.todoSummary).toBeUndefined()
    expect(result.acceptanceState).toBe('evidence_pending')
  })

  it('fails closed on a mismatched Run and never displays an aggregate when the account has no Run', () => {
    expect(() => scopeGameDetailToAccount(mixed, 'B', run())).toThrow('账号不一致')
    const result = scopeGameDetailToAccount(mixed, 'B')
    expect(result.runId).toBeUndefined()
    expect(result.artifacts).toEqual([])
    expect(result.acceptanceState).toBe('not_started')
  })

  it('preserves Run context in links rather than selecting the latest whole-game run', () => {
    expect(accountRunLocation('WW', 'run-B', 'B')).toEqual({ path: '/games/WW', query: { runId: 'run-B', accountId: 'B' } })
  })

  it('shows a pre-Todo human gate from the exact account Run and its blocked completion contract', () => {
    const record = { ...run('B', false), state: 'human_required' }
    record.completionContract = { ...record.completionContract!, outcome: 'blocked' }
    const attempt = { runAttemptId: 'attempt-B', runId: record.runId, accountId: 'B', gameId: 'WW', state: 'human_required' } as RunAttempt
    const result = scopeGameDetailToAccount({ ...mixed, runAttempts: [
      attempt, { ...attempt, runAttemptId: 'attempt-A', runId: 'run-A', accountId: 'default' },
      { ...attempt, runAttemptId: 'foreign-account', accountId: 'default' },
      { ...attempt, runAttemptId: 'foreign-game', gameId: 'PGR' },
    ], todoAttempts: [] }, 'B', record)
    expect(result.runAttempts).toEqual([attempt])
    expect(loadedAttemptCounts(result)).toEqual({ runAttemptCount: 1, todoAttemptCount: 0, humanRequiredAttemptCount: 1 })
    expect(result.runtimeState).toBe('human_required')
    expect(result.acceptanceState).toBe('evidence_pending')
    expect(result.attemptAnalysis).toBeUndefined()
  })

  it('keeps unread attempt counts unknown and counts one human gate once across Run and Todo records', () => {
    const record = run('B', false)
    const unknown = scopeGameDetailToAccount({ ...mixed, runAttempts: undefined, todoAttempts: [] }, 'B', record)
    expect(loadedAttemptCounts(unknown)).toEqual({ runAttemptCount: undefined, todoAttemptCount: undefined, humanRequiredAttemptCount: undefined })
    const attempt = { runAttemptId: 'attempt-B', runId: record.runId, accountId: 'B', gameId: 'WW', state: 'human_required' } as RunAttempt
    const step = { todoAttemptId: 'step-B', todoInstanceId: todo('B').todoInstanceId, runAttemptId: attempt.runAttemptId, state: 'human_required' } as TodoAttempt
    const result = scopeGameDetailToAccount({ ...mixed, runAttempts: [attempt], todoAttempts: [step,
      { ...step, todoAttemptId: 'foreign-todo', todoInstanceId: todo().todoInstanceId },
      { ...step, todoAttemptId: 'foreign-attempt', runAttemptId: 'attempt-A' },
    ] }, 'B', record)
    expect(result.todoAttempts).toEqual([step])
    expect(loadedAttemptCounts(result)).toEqual({ runAttemptCount: 1, todoAttemptCount: 1, humanRequiredAttemptCount: 1 })
  })

  it('validates a review against its exact Run even when another account owns the game snapshot', () => {
    const item = { workItemId: 'work-B', kind: 'evidence_review', gameId: 'WW', runId: 'run-B', result: { completionReviewScope: { gameId: 'WW', runId: 'run-B', accountId: 'B' } } }
    expect(reviewRunContextError(item, run('B'))).toBeUndefined()
    expect(reviewRunContextError(item, run())).toContain('运行不一致')
    expect(reviewRunContextError(item, undefined)).toContain('等待核验')
    expect(reviewRunContextError(item, { ...run('B'), accountId: 'default' })).toContain('账号不一致')
    expect(reviewRunContextError({ ...item, result: { completionReviewScope: { gameId: 'WW', runId: 'run-B' } } }, run('B'))).toContain('账号不一致')
  })
})

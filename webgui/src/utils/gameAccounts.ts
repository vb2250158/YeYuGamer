import { recordOf, type AccountTarget, type AgentWorkItem, type BatchRun, type EvidenceArtifact, type GameAccount, type GameDetail, type GameRunRecord, type GameState, type OKWWProfile, type TodoDefinition, type TodoInstance } from '../api/contracts'
import { acceptedCompletionEvidence } from './todayEvidence'

export const defaultAccountId = 'default'
export const accountIdOf = (value: { accountId?: string }) => value.accountId ?? defaultAccountId
export const accountTargetId = (gameId: string, accountId: string) => accountId === defaultAccountId ? gameId : `${gameId}::${accountId}`

export function readOKWWProfile(value: unknown): OKWWProfile {
  const raw = recordOf(value)
  const farm = raw.whichToFarm ?? raw.which_to_farm
  const material = raw.materialSelection ?? raw.material_selection
  const tacet = raw.tacetSuppressionNumber ?? raw.tacet_suppression_number
  const forgery = raw.forgeryChallengeNumber ?? raw.forgery_challenge_number
  const echo = raw.farmNightmareNestForDailyEcho ?? raw.farm_nightmare_nest_for_daily_echo
  return {
    whichToFarm: farm === 'Forgery Challenge' || farm === 'Simulation Challenge' ? farm : 'Tacet Suppression',
    tacetSuppressionNumber: typeof tacet === 'number' ? tacet : 1,
    forgeryChallengeNumber: typeof forgery === 'number' ? forgery : 1,
    materialSelection: material === 'Resonator EXP' || material === 'Weapon EXP' ? material : 'Shell Credit',
    farmNightmareNestForDailyEcho: typeof echo === 'boolean' ? echo : true,
  }
}

/** Legacy shared values seed a new account only; saved account values win. */
export function newWWAccount(config: unknown, label = '新账号'): GameAccount {
  const raw = recordOf(config)
  const selection = recordOf(raw.daily_todo_selection ?? raw.dailyTodoSelection).WW
  const profiles = recordOf(raw.daily_tool_profiles ?? raw.dailyToolProfiles)
  return {
    label, enabled: true, saved_account_label: '',
    daily_todo_selection: Array.isArray(selection) ? selection.filter((item): item is string => typeof item === 'string') : [],
    daily_tool_profiles: { ok_ww: readOKWWProfile(profiles.ok_ww ?? profiles.okWw) },
  }
}

export const accountDailySelection = (account: GameAccount): string[] => account.daily_todo_selection ?? []
export const accountOKWWProfile = (account: GameAccount): OKWWProfile => readOKWWProfile(account.daily_tool_profiles?.ok_ww)

export function configuredGameAccounts(config: unknown, gameId: string): GameAccount[] {
  const raw = recordOf(config)
  const accounts = recordOf(raw.game_accounts ?? raw.gameAccounts)[gameId]
  if (!Array.isArray(accounts)) return [{ ...(gameId === 'WW' ? newWWAccount(config, '当前账号') : { label: '当前账号', enabled: true, saved_account_label: '' }), account_id: defaultAccountId }]
  return accounts.map((value) => {
    const row = recordOf(value)
    const initial = gameId === 'WW' ? newWWAccount(config) : undefined
    const selection = row.daily_todo_selection ?? row.dailyTodoSelection
    const profiles = row.daily_tool_profiles ?? row.dailyToolProfiles
    return {
      account_id: typeof row.account_id === 'string' ? row.account_id : undefined,
      label: typeof row.label === 'string' ? row.label : '',
      enabled: row.enabled === true,
      saved_account_label: typeof row.saved_account_label === 'string' ? row.saved_account_label : '',
      ...(initial ? {
        daily_todo_selection: Array.isArray(selection) ? selection.filter((item): item is string => typeof item === 'string') : initial.daily_todo_selection,
        daily_tool_profiles: profiles == null ? initial.daily_tool_profiles : { ok_ww: readOKWWProfile(recordOf(profiles).ok_ww ?? recordOf(profiles).okWw) },
      } : {}),
    }
  })
}

/** Omit local row identities; never derive persistent IDs from labels or positions. */
export function gameAccountsPatch(accounts: readonly GameAccount[]): GameAccount[] {
  return accounts.map((account) => ({
    ...(account.account_id ? { account_id: account.account_id } : {}),
    label: account.label.trim(), enabled: account.enabled,
    saved_account_label: account.saved_account_label.trim(),
    ...(account.daily_todo_selection != null ? { daily_todo_selection: [...account.daily_todo_selection] } : {}),
    ...(account.daily_tool_profiles != null ? { daily_tool_profiles: { ok_ww: accountOKWWProfile(account) } } : {}),
  }))
}

export function moveGameAccount(accounts: readonly GameAccount[], index: number, direction: -1 | 1): GameAccount[] {
  const result = accounts.map((account) => ({ ...account }))
  const next = index + direction
  if (index >= 0 && index < result.length && next >= 0 && next < result.length) {
    ;[result[index], result[next]] = [result[next]!, result[index]!]
  }
  return result
}

export function accountBindingLabel(account: GameAccount): string {
  if (!account.enabled) return '已停用'
  return account.saved_account_label.trim() ? '已填写匹配标签，执行时核对' : '待绑定'
}

export function accountTodos(todos: readonly TodoInstance[], gameId: string, accountId: string): TodoInstance[] {
  return todos.filter((todo) => todo.gameId === gameId && accountIdOf(todo) === accountId)
}

/** Catalog entries configure a new account; only its own instances supply state. */
export function accountDailyConfigRows(account: GameAccount, definitions: readonly TodoDefinition[], todos: readonly TodoInstance[]) {
  const ownTodos = account.account_id ? accountTodos(todos, 'WW', account.account_id).filter((todo) => todo.cadence === 'daily') : []
  const catalog = definitions.filter((definition) => definition.gameId === 'WW' && definition.cadence === 'daily' && definition.active)
  const rows = new Map([...catalog, ...ownTodos].map((item) => [item.todoDefinitionId, {
    todoDefinitionId: item.todoDefinitionId, title: item.title, operation: item.operation, orderIndex: item.orderIndex,
    todo: ownTodos.find((todo) => todo.todoDefinitionId === item.todoDefinitionId),
  }]))
  return [...rows.values()].sort((a, b) => a.orderIndex - b.orderIndex)
}

export function todayAccountTargets(gameId: string, accounts: readonly GameAccount[], batch?: BatchRun | null): AccountTarget[] {
  if (batch?.cadence === 'daily') {
    const targets = batch.result?.accountTargets
    if (Array.isArray(targets)) return targets.filter((target) => target.gameId === gameId)
    // Legacy batches contain only the original account, regardless of later config.
    return (batch.gameIds ?? []).includes(gameId)
      ? [{ targetId: gameId, gameId, accountId: defaultAccountId, accountLabel: '当前账号' }]
      : []
  }
  return accounts.filter((account) => account.enabled && account.account_id
    && (account.daily_todo_selection == null || account.daily_todo_selection.length > 0)).map((account) => ({
    targetId: accountTargetId(gameId, account.account_id!), gameId,
    accountId: account.account_id!, accountLabel: account.label || '未命名账号',
  }))
}

/** An account row can only consume its own current Run's completion decision. */
export function accountGameState(game: GameState, target: AccountTarget, todos: readonly TodoInstance[], runs: readonly GameRunRecord[]): GameState {
  const scoped = accountTodos(todos, target.gameId, target.accountId)
  const runIds = new Set(scoped.map((todo) => todo.runId).filter(Boolean))
  const run = runs.filter((item) => item.gameId === target.gameId && accountIdOf(item) === target.accountId
    && (target.runId ? item.runId === target.runId : runIds.has(item.runId)))
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))[0]
  const runId = target.runId ?? run?.runId
  const decision = run?.completionContract ?? target.completionContract
  const validDecision = decision && decision.gameId === target.gameId && decision.runId === runId
    && accountIdOf(decision) === target.accountId && scoped.some((todo) => todo.periodKey === decision.gameDayKey)
  const runtimeState = run?.state ?? target.state ?? 'planned'
  const blockedAttempt = decision?.outcome === 'blocked' && Boolean(decision.runAttemptId)
    && !['planned', 'queued'].includes(runtimeState)
  const acceptanceState = validDecision && decision.acceptedDone === true ? 'accepted_done'
    : validDecision && (decision.outcome === 'review_required' || blockedAttempt) ? 'evidence_pending' : 'not_started'
  return {
    ...game, displayName: `${game.displayName} · ${target.accountLabel}`,
    runId: runId ?? undefined, runtimeState,
    acceptanceState, reviewState: 'none', policy: {},
    updatedAt: run?.updatedAt ?? target.updatedAt,
  }
}

export function accountRunLocation(gameId: string, runId?: string | null, accountId?: string) {
  return { path: `/games/${encodeURIComponent(gameId)}`, query: { ...(runId ? { runId } : {}), ...(accountId ? { accountId } : {}) } }
}

export function reviewRunContextError(workItem: AgentWorkItem | undefined, run: GameRunRecord | undefined): string | undefined {
  if (workItem?.kind !== 'evidence_review') return undefined
  if (!run) return '等待核验工作项对应的运行与账号'
  const scope = recordOf(workItem.result?.completionReviewScope)
  if (run.runId !== workItem.runId || run.runId !== scope.runId || run.gameId !== workItem.gameId || run.gameId !== scope.gameId) return '工作项与查询到的游戏运行不一致'
  if ((scope.accountId ?? defaultAccountId) !== accountIdOf(run)
    || (workItem.accountId !== undefined && workItem.accountId !== accountIdOf(run))) return '工作项与查询到的账号不一致'
  return undefined
}

export function accountCompletionEvidence(game: GameState, target: AccountTarget, todos: readonly TodoInstance[], runs: readonly GameRunRecord[], artifacts: EvidenceArtifact[]): EvidenceArtifact[] {
  const scoped = accountTodos(todos, target.gameId, target.accountId)
  const state = accountGameState(game, target, scoped, runs)
  const decision = runs.find((run) => run.runId === state.runId && run.gameId === target.gameId && accountIdOf(run) === target.accountId)?.completionContract
    ?? target.completionContract
  return acceptedCompletionEvidence(artifacts, {
    gameId: target.gameId, runId: state.runId, acceptanceState: state.acceptanceState,
    gameDayKeys: scoped.map((todo) => todo.periodKey), acceptedArtifactIds: decision?.screenshotArtifactRefs ?? [],
  })
}

/** Strip game-wide diagnostics and completion when opening one account/Run. */
export function scopeGameDetailToAccount(detail: GameDetail, accountId: string, run?: GameRunRecord): GameDetail {
  if (run && (run.gameId !== detail.gameId || accountIdOf(run) !== accountId)) throw new Error('运行与所选游戏账号不一致')
  const todoIds = new Set(run?.completionTodoInstanceIds ?? [])
  const todos = accountTodos(detail.todoInstances ?? [], detail.gameId, accountId)
    .filter((todo) => !run || todo.runId === run.runId || (!todo.runId && todoIds.has(todo.todoInstanceId)))
  const label = typeof run?.accountSnapshot?.label === 'string' ? run.accountSnapshot.label
    : accountId === defaultAccountId ? '当前账号' : '所选账号'
  const target: AccountTarget = { targetId: accountTargetId(detail.gameId, accountId), gameId: detail.gameId, accountId, accountLabel: label, runId: run?.runId }
  const projected = accountGameState(detail, target, todos, run ? [run] : [])
  const runAttempts = detail.runAttempts?.filter((attempt) => run && attempt.runId === run.runId
    && attempt.gameId === run.gameId && accountIdOf(attempt) === accountId)
  const attemptIds = new Set(runAttempts?.map((attempt) => attempt.runAttemptId))
  const scopedTodoIds = new Set(todos.map((todo) => todo.todoInstanceId))
  const artifacts = (detail.artifacts ?? []).filter((artifact) => run && artifact.runId === run.runId && artifact.gameId === run.gameId)
  const sameCurrentRun = Boolean(run && detail.runId === run.runId)
  return {
    ...detail, ...projected, accountId, accountSnapshot: run?.accountSnapshot,
    stage: projected.runtimeState, bootstrapStage: undefined, nextAction: run?.message,
    activeRun: undefined, todoInstances: todos, todoSummary: undefined,
    progress: undefined, nextResetAt: undefined, unresolvedRequiredTodoIds: undefined,
    attempts: run ? [{ ...run }] : [], runAttempts,
    todoAttempts: runAttempts && detail.todoAttempts?.filter((attempt) => attemptIds.has(attempt.runAttemptId) && scopedTodoIds.has(attempt.todoInstanceId)),
    attemptAnalysis: undefined, artifacts, evidenceCount: artifacts.length,
    controllerLease: sameCurrentRun ? detail.controllerLease : undefined,
    windowBinding: sameCurrentRun ? detail.windowBinding : undefined,
    checkpoints: sameCurrentRun ? detail.checkpoints : [],
    findings: [],
  }
}

/** Count loaded attempts, including human gates reached before any Todo starts. */
export function loadedAttemptCounts(detail?: Pick<GameDetail, 'runAttempts' | 'todoAttempts'>) {
  const runAttempts = detail?.runAttempts
  const todoAttempts = detail?.todoAttempts
  const humanAttempts = runAttempts && new Set([
    ...runAttempts.filter((attempt) => attempt.state === 'human_required').map((attempt) => attempt.runAttemptId),
    ...(todoAttempts ?? []).filter((attempt) => attempt.state === 'human_required'
      && runAttempts.some((runAttempt) => runAttempt.runAttemptId === attempt.runAttemptId)).map((attempt) => attempt.runAttemptId),
  ])
  return {
    runAttemptCount: runAttempts?.length,
    todoAttemptCount: todoAttempts?.length,
    humanRequiredAttemptCount: humanAttempts?.size,
  }
}

import type {
  AutomationAssessment,
  JsonObject,
  TodoActionAvailability,
  TodoAttempt,
  TodoDefinition,
  TodoDifficulty,
  TodoDispatchDisposition,
  TodoInstance,
  TodoResetRule,
  TodoStatus,
  TodoSummary,
  TodoSummaries,
} from '../api/contracts'

const todoStatuses: TodoStatus[] = [
  'pending',
  'in_progress',
  'completed',
  'skipped',
  'blocked',
  'review_required',
  'human_required',
]

export function completedTodo(item: TodoInstance): boolean {
  return item.status === 'completed'
}

export function schedulableTodo(item: TodoInstance): boolean {
  return item.dispatchDisposition === 'eligible'
}

export function deferredTodo(item: TodoInstance): boolean {
  return [
    'deferred_review',
    'deferred_human',
    'deferred_forbidden',
    'reconcile_required',
    'unsupported',
  ].includes(item.dispatchDisposition)
}

const dispatchLabels: Record<TodoDispatchDisposition, string> = {
  eligible: '可调度',
  completed_skip: '已完成跳过',
  terminal_skip: '终态跳过',
  deferred_review: '等待复核',
  deferred_human: '等待人工',
  deferred_forbidden: '禁止自动执行',
  reconcile_required: '需要对账',
  unsupported: '暂不支持',
}

export function dispatchDispositionLabel(disposition: TodoDispatchDisposition): string {
  return dispatchLabels[disposition]
}

export function latestAutomationAssessment(
  todoInstanceId: string,
  assessments: AutomationAssessment[],
): AutomationAssessment | undefined {
  return assessments
    .filter((item) => item.todoInstanceId === todoInstanceId)
    .sort((left, right) => {
      const timestampDelta = Date.parse(right.createdAt) - Date.parse(left.createdAt)
      return timestampDelta || right.assessmentId.localeCompare(left.assessmentId)
    })[0]
}

function uniqueSorted(values: string[]): string[] {
  return [...new Set(values)].sort()
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function presentationFingerprint(value: unknown): string {
  const input = stableJson(value)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    first = Math.imul(first ^ code, 0x01000193)
    second = Math.imul(second ^ code, 0x85ebca6b)
  }
  return `webgui-fallback-v1:${(first >>> 0).toString(16).padStart(8, '0')}${(second >>> 0).toString(16).padStart(8, '0')}`
}

export function todoSummaryFromItems(items: TodoInstance[], cadence: TodoInstance['cadence']): TodoSummary {
  const scoped = items.filter((item) => item.cadence === cadence)
  const required = scoped.filter((item) => item.required)
  const outstanding = required.filter((item) => !completedTodo(item))
  const completed = required.length - outstanding.length
  const counts = Object.fromEntries(todoStatuses.map((status) => [
    status,
    scoped.filter((item) => item.status === status).length,
  ])) as Record<TodoStatus, number>
  const nextResetAt = scoped
    .map((item) => item.periodEndsAt)
    .filter(Boolean)
    .sort()[0]
  const periodKeys = uniqueSorted(scoped.map((item) => item.periodKey))
  const periodStartsAt = uniqueSorted(scoped.map((item) => item.periodStartsAt))
  const periodEndsAt = uniqueSorted(scoped.map((item) => item.periodEndsAt))
  const resetRules = [...new Map(scoped
    .map((item) => [stableJson(item.resetRule), item.resetRule] as const)
    .sort(([left], [right]) => left.localeCompare(right))).values()]
  const scopeFingerprint = presentationFingerprint({
    schemaVersion: 1,
    cadence,
    instances: [...scoped]
      .sort((left, right) => left.todoInstanceId.localeCompare(right.todoInstanceId))
      .map((item) => ({
        todoInstanceId: item.todoInstanceId,
        todoDefinitionId: item.todoDefinitionId,
        definitionVersion: item.definitionVersion,
        sourceHash: item.sourceHash,
        periodKey: item.periodKey,
        periodStartsAt: item.periodStartsAt,
        periodEndsAt: item.periodEndsAt,
        resetRule: item.resetRule,
      })),
  })
  const scopeKey = periodKeys.length === 1 && periodStartsAt.length === 1 && periodEndsAt.length === 1
    ? `${cadence}:${periodKeys[0]}:${scopeFingerprint.slice(-12)}`
    : `${cadence}:mixed:${scopeFingerprint.slice(-16)}`

  return {
    cadence,
    scopeKey,
    scopeFingerprint,
    periodKeys,
    periodStartsAt,
    periodEndsAt,
    resetRules,
    definitionCount: scoped.length,
    requiredTotal: required.length,
    requiredCompleted: completed,
    requiredRemaining: outstanding.length,
    allRequiredCompleted: required.length > 0 && outstanding.length === 0,
    progress: {
      completed,
      total: required.length,
      percent: required.length ? Math.round(completed * 10_000 / required.length) / 100 : 0,
    },
    nextResetAt,
    counts,
    outstandingTodoInstanceIds: outstanding.map((item) => item.todoInstanceId),
    unresolvedRequiredTodoIds: outstanding.map((item) => item.todoInstanceId),
    difficultOperations: outstanding
      .filter((item) => item.automationDifficulty === 'high'
        || item.automationDifficulty === 'unknown'
         || item.status === 'blocked'
         || item.status === 'review_required'
         || item.status === 'human_required')
      .map((item) => ({
        todoInstanceId: item.todoInstanceId,
        operation: item.operation,
        title: item.title,
        status: item.status,
        automationDifficulty: item.automationDifficulty,
        automationState: item.automationState,
        reason: item.reason,
      })),
  }
}

export function todosByGame(items: TodoInstance[]): Map<string, TodoInstance[]> {
  const grouped = new Map<string, TodoInstance[]>()
  for (const item of [...items].sort((left, right) => left.orderIndex - right.orderIndex)) {
    const gameItems = grouped.get(item.gameId) ?? []
    gameItems.push(item)
    grouped.set(item.gameId, gameItems)
  }
  return grouped
}

export function cadenceSummary(value: TodoSummary | TodoSummaries | undefined, cadence: TodoInstance['cadence']): TodoSummary | undefined {
  if (!value) return undefined
  return 'cadence' in value ? value : value[cadence]
}

export function resetCountdown(resetAt?: string | null, now = Date.now()): string {
  if (!resetAt) return '尚无当前周期边界'
  const target = Date.parse(resetAt)
  if (!Number.isFinite(target)) return resetAt
  const remaining = Math.max(0, target - now)
  if (remaining === 0) return '已到重置边界，等待显式对账'
  const totalMinutes = Math.ceil(remaining / 60_000)
  const days = Math.floor(totalMinutes / 1_440)
  const hours = Math.floor((totalMinutes % 1_440) / 60)
  const minutes = totalMinutes % 60
  return [days ? `${days}天` : '', hours ? `${hours}小时` : '', `${minutes}分钟`].filter(Boolean).join(' ')
}

export function resetRuleLabel(rule?: JsonObject | null): string {
  if (!rule) return '等待 Manager reset rule'
  const timezone = typeof rule.timezone === 'string' ? rule.timezone : '时区未指定'
  const time = typeof rule.time === 'string' ? rule.time : '时间未指定'
  const rawWeekday = rule.weekStartDay ?? rule.weekday ?? rule.week_start_day
  const weekday = typeof rawWeekday === 'string' ? `${rawWeekday} ` : ''
  return `${timezone} · ${weekday}${time}`
}

export interface ResetRulePresentation {
  mode: 'frozen' | 'baseline'
  rules: TodoResetRule[]
  definitionRules: TodoResetRule[]
}

function uniqueResetRules(rules: TodoResetRule[]): TodoResetRule[] {
  return [...new Map(rules.map((rule) => [JSON.stringify(rule), rule])).values()]
}

export function resetRulePresentation(
  instances: TodoInstance[],
  definitions: TodoDefinition[],
  configurationBaseline: TodoResetRule,
): ResetRulePresentation {
  const frozen = uniqueResetRules(instances.map((item) => item.resetRule))
  if (frozen.length) return { mode: 'frozen', rules: frozen, definitionRules: [] }
  return {
    mode: 'baseline',
    rules: [configurationBaseline],
    definitionRules: uniqueResetRules(definitions.map((item) => item.resetRule)),
  }
}

export interface TodoDiagnosticItem {
  todoInstanceId: string
  gameId?: string
  operation?: string
  title?: string
  status?: TodoStatus
  automationDifficulty?: TodoDifficulty
  automationState?: string
  attempts?: number
  blockedCount?: number
  reviewRequiredCount?: number
  humanRequiredCount?: number
  retryableFailureCount?: number
  latestState?: string
  latestReasonCode?: string
  latestReason?: string
  reason?: string
  evidenceRefs?: string[]
  dispatchDisposition?: TodoDispatchDisposition
  dispatchReasonCode?: string
  dispatchReason?: string
  actionAvailability?: TodoActionAvailability
  latestTodoAttempt?: TodoAttempt
  activeBlocker?: JsonObject
  latestAutomationAssessment?: AutomationAssessment
  recentExecutionFacts?: JsonObject[]
}

function objectValue(value: unknown): JsonObject | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : undefined
}

export function todoDiagnostics(result?: JsonObject): TodoDiagnosticItem[] {
  const difficulty = objectValue(result?.todoDifficulty)
  const snapshot = objectValue(result?.todoSnapshot)
  const attemptAnalysis = objectValue(result?.attemptAnalysis)
  const snapshotItems = (Array.isArray(snapshot?.items) ? snapshot.items : [])
    .map(objectValue)
    .filter((item): item is JsonObject => !!item && typeof item.todoInstanceId === 'string')
  const snapshotsById = new Map(snapshotItems.map((item) => [item.todoInstanceId as string, item]))
  const diagnosticCandidates = [
    ...(Array.isArray(difficulty?.items) ? difficulty.items : []),
    ...(Array.isArray(attemptAnalysis?.problemSignals) ? attemptAnalysis.problemSignals : []),
  ]
  const candidatesById = new Map<string, JsonObject>(snapshotItems.map((item) => [
    item.todoInstanceId as string,
    item,
  ]))
  diagnosticCandidates.map(objectValue).forEach((item) => {
    if (!item || typeof item.todoInstanceId !== 'string') return
    candidatesById.set(item.todoInstanceId, { ...candidatesById.get(item.todoInstanceId), ...item })
  })
  const candidates = [...candidatesById.values()]
  return candidates
    .map(objectValue)
    .filter((item): item is JsonObject => !!item && typeof item.todoInstanceId === 'string')
    .map((candidate) => {
      const item = { ...snapshotsById.get(candidate.todoInstanceId as string), ...candidate }
      const dispatch = objectValue(item.dispatch)
      const latestTodoAttempt = objectValue(item.latestTodoAttempt)
      const activeBlocker = objectValue(item.activeBlocker)
      const latestAssessment = objectValue(item.latestAutomationAssessment)
      const recentExecutionFacts = Array.isArray(item.recentExecutionFacts)
        ? item.recentExecutionFacts.map(objectValue).filter((value): value is JsonObject => !!value)
        : undefined
      const actionAvailability = objectValue(item.actionAvailability ?? dispatch?.actionAvailability)
      const rawDispatchDisposition = item.dispatchDisposition ?? dispatch?.disposition
      const rawDispatchReasonCode = item.dispatchReasonCode ?? dispatch?.reasonCode
      const rawDispatchReason = item.dispatchReason ?? dispatch?.reason
      const rawStatus = item.status ?? item.currentStatus
      return {
        todoInstanceId: item.todoInstanceId as string,
        gameId: typeof item.gameId === 'string' ? item.gameId : undefined,
        operation: typeof item.operation === 'string' ? item.operation : undefined,
        title: typeof item.title === 'string' ? item.title : undefined,
        status: typeof rawStatus === 'string' ? rawStatus as TodoStatus : undefined,
        automationDifficulty: typeof item.automationDifficulty === 'string'
          ? item.automationDifficulty as TodoDifficulty
          : undefined,
        automationState: typeof item.automationState === 'string' ? item.automationState : undefined,
        attempts: typeof item.attemptCount === 'number'
          ? item.attemptCount
          : typeof item.attempts === 'number' ? item.attempts : undefined,
        blockedCount: typeof item.blockedCount === 'number' ? item.blockedCount : undefined,
        reviewRequiredCount: typeof item.reviewRequiredCount === 'number' ? item.reviewRequiredCount : undefined,
        humanRequiredCount: typeof item.humanRequiredCount === 'number' ? item.humanRequiredCount : undefined,
        retryableFailureCount: typeof item.retryableFailureCount === 'number' ? item.retryableFailureCount : undefined,
        latestState: typeof item.latestState === 'string' ? item.latestState : undefined,
        latestReasonCode: typeof item.latestReasonCode === 'string' ? item.latestReasonCode : undefined,
        latestReason: typeof item.latestReason === 'string' ? item.latestReason : undefined,
        reason: typeof item.latestReason === 'string'
          ? item.latestReason
          : typeof item.reason === 'string' ? item.reason : undefined,
        evidenceRefs: Array.isArray(item.evidenceRefs)
          ? item.evidenceRefs.filter((value): value is string => typeof value === 'string')
          : undefined,
        dispatchDisposition: typeof rawDispatchDisposition === 'string'
          ? rawDispatchDisposition as TodoDispatchDisposition
          : undefined,
        dispatchReasonCode: typeof rawDispatchReasonCode === 'string' ? rawDispatchReasonCode : undefined,
        dispatchReason: typeof rawDispatchReason === 'string' ? rawDispatchReason : undefined,
        actionAvailability: actionAvailability as TodoActionAvailability | undefined,
        latestTodoAttempt: latestTodoAttempt as TodoAttempt | undefined,
        activeBlocker,
        latestAutomationAssessment: latestAssessment as AutomationAssessment | undefined,
        recentExecutionFacts,
      }
    })
}

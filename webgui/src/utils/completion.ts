import type {
  AgentWorkItem,
  BatchResult,
  BatchRun,
  CompletionReviewContract,
  CompletionReviewRequiredTodo,
  CompletionReviewMetricConstraint,
  CompletionPredicateReview,
  CompletionReviewPredicateContract,
  CompletionReviewScope,
  CompletionReviewSubmission,
  EvidenceArtifact,
  JsonObject,
  StrictMetricValue,
  TodoReviewVerdict,
  TodoInstance,
  TodoStatus,
} from '../api/contracts'
import type { TodoDiagnosticItem } from './todos'

export interface ValidationResult<T> {
  value?: T
  errors: string[]
}

export type CompletionReviewDecision = 'accepted' | 'rejected' | 'review_required'

export interface CompletionPredicateForm {
  metrics: Record<string, StrictMetricValue | null>
  artifactRefs: string[]
}

export interface CompletionTodoReviewForm {
  verdict: TodoReviewVerdict
  reasonCode: string
  artifactRefs: string[]
}

export interface CompletionReviewForm {
  evidenceIds: string[]
  predicates: Record<string, CompletionPredicateForm>
  todoReviews: Record<string, CompletionTodoReviewForm>
}

export interface CompletionMetricField {
  metric: string
  valueType: 'number' | 'string' | 'boolean'
  constraints: CompletionReviewMetricConstraint[]
}

export interface CompletionArtifactOption {
  artifactId: string
  kind: string
  todoInstanceId: string
  runAttemptId: string
  contentType: 'image/jpeg' | 'image/png'
  capturedAt?: string
}

export interface CompletionBlockerView {
  blockerId: string
  source: 'todo' | 'diagnosis'
  gameId?: string
  title: string
  status: string
  reason?: string
  evidenceRefs: string[]
  humanRequiredCount?: number
}

const blockerStatuses = new Set<TodoStatus>(['blocked', 'review_required', 'human_required'])
const todoStatuses = new Set<TodoStatus>([
  'pending', 'in_progress', 'completed', 'skipped', 'blocked', 'review_required', 'human_required',
])
const completionEvidenceContentTypes = new Set(['image/jpeg', 'image/png'])

function objectValue(value: unknown): JsonObject | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : undefined
}

function opaqueId(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= 160
    && value.trim() === value
    && !value.includes('/')
    && !value.includes('\\')
}

function boundedText(value: unknown, maximum: number, allowEmpty = false): value is string {
  return typeof value === 'string'
    && value.length <= maximum
    && value.trim() === value
    && (allowEmpty || value.length > 0)
    && !/[\u0000-\u001f\u007f]/.test(value)
}

function safeMetricString(value: unknown): value is string {
  return boundedText(value, 200)
    && !value.includes('://')
    && !value.includes('/')
    && !value.includes('\\')
    && !/[`;$]/.test(value)
}

function artifactKind(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= 120
    && /^[A-Za-z0-9._:-]+$/.test(value)
}

function exactKeys(value: JsonObject, allowed: readonly string[], label: string, errors: string[]): void {
  const extras = Object.keys(value).filter((key) => !allowed.includes(key))
  if (extras.length) errors.push(`${label} 包含未登记字段：${extras.join('、')}`)
}

function awareIso(value: unknown): value is string {
  return boundedText(value, 80)
    && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value))
}

function requiredOpaque(scope: JsonObject, key: string, errors: string[]): string | undefined {
  const value = scope[key]
  if (!opaqueId(value)) errors.push(`${key} 缺失，或不是 Manager opaque ID`)
  return opaqueId(value) ? value : undefined
}

function opaqueList(
  value: unknown,
  label: string,
  errors: string[],
  maximum = 100,
): string[] {
  const raw = Array.isArray(value) ? value : []
  const values = raw.filter(opaqueId)
  if (!Array.isArray(value) || raw.length > maximum || values.length !== raw.length
    || new Set(values).size !== values.length) {
    errors.push(`${label} 必须是最多 ${maximum} 个唯一 opaque ID`)
  }
  return values
}

function parseRequiredTodo(
  value: unknown,
  index: number,
  workItemArtifactIds: Set<string>,
  errors: string[],
): CompletionReviewRequiredTodo | undefined {
  const label = `completionReviewScope.requiredTodos[${index}]`
  const raw = objectValue(value)
  if (!raw) {
    errors.push(`${label} 必须是对象`)
    return undefined
  }
  exactKeys(raw, ['todoInstanceId', 'operation', 'status', 'artifactRefs'], label, errors)
  const todoInstanceId = opaqueId(raw.todoInstanceId) ? raw.todoInstanceId : undefined
  const operation = opaqueId(raw.operation) ? raw.operation : undefined
  const status = typeof raw.status === 'string' && todoStatuses.has(raw.status as TodoStatus)
    ? raw.status as TodoStatus
    : undefined
  if (!todoInstanceId) errors.push(`${label}.todoInstanceId 必须是 opaque ID`)
  if (!operation) errors.push(`${label}.operation 必须是登记的 operation ID`)
  if (!status) errors.push(`${label}.status 不受支持`)
  const artifactRefs = opaqueList(raw.artifactRefs, `${label}.artifactRefs`, errors, 20)
  if (artifactRefs.some((artifactId) => !workItemArtifactIds.has(artifactId))) {
    errors.push(`${label}.artifactRefs 只能引用当前工作项 artifact`)
  }
  if (!todoInstanceId || !operation || !status) return undefined
  return { todoInstanceId, operation, status, artifactRefs }
}

export function parseCompletionReviewScope(
  workItem?: AgentWorkItem,
  currentSnapshotRunId?: string,
): ValidationResult<CompletionReviewScope> {
  const errors: string[] = []
  if (!workItem || workItem.kind !== 'evidence_review') {
    return { errors: ['只有 evidence_review 工作项可以提交 CompletionReview'] }
  }
  const scope = objectValue(workItem.result?.completionReviewScope)
  if (!scope) return { errors: ['工作项没有 Manager 签发的 completionReviewScope'] }
  exactKeys(scope, [
    'schemaVersion', 'batchId', 'gameId', 'runId', 'runAttemptId', 'attemptLineageIds',
    'gameDayKey', 'periodStartsAt', 'periodEndsAt', 'requiredTodoInstanceIds', 'requiredTodos',
  ], 'completionReviewScope', errors)
  if (scope.schemaVersion !== 3) errors.push('completionReviewScope schemaVersion 必须为 3')

  const batchId = requiredOpaque(scope, 'batchId', errors)
  const gameId = requiredOpaque(scope, 'gameId', errors)
  const runId = requiredOpaque(scope, 'runId', errors)
  const runAttemptId = requiredOpaque(scope, 'runAttemptId', errors)
  const gameDayKey = requiredOpaque(scope, 'gameDayKey', errors)
  const attemptLineageIds = opaqueList(scope.attemptLineageIds, 'completionReviewScope.attemptLineageIds', errors)
  if (!attemptLineageIds.length) {
    errors.push('completionReviewScope.attemptLineageIds 必须是 1–100 个唯一 opaque attempt ID')
  }
  if (runAttemptId && !attemptLineageIds.includes(runAttemptId)) {
    errors.push('scope runAttemptId 必须属于 attemptLineageIds')
  }
  const periodStartsAt = awareIso(scope.periodStartsAt) ? scope.periodStartsAt : undefined
  const periodEndsAt = awareIso(scope.periodEndsAt) ? scope.periodEndsAt : undefined
  if (!periodStartsAt) errors.push('completionReviewScope.periodStartsAt 必须是带时区 ISO 时间')
  if (!periodEndsAt) errors.push('completionReviewScope.periodEndsAt 必须是带时区 ISO 时间')
  if (periodStartsAt && periodEndsAt && Date.parse(periodEndsAt) <= Date.parse(periodStartsAt)) {
    errors.push('completionReviewScope period 必须正向且非空')
  }
  const requiredTodoInstanceIds = opaqueList(
    scope.requiredTodoInstanceIds,
    'completionReviewScope.requiredTodoInstanceIds',
    errors,
  )
  const workItemArtifactIds = new Set((workItem.artifactRefs ?? []).filter(opaqueId))
  const rawRequiredTodos = Array.isArray(scope.requiredTodos) ? scope.requiredTodos : []
  if (!Array.isArray(scope.requiredTodos) || rawRequiredTodos.length > 100) {
    errors.push('completionReviewScope.requiredTodos 必须是最多 100 项的数组')
  }
  const requiredTodos = rawRequiredTodos.slice(0, 100)
    .map((item, index) => parseRequiredTodo(item, index, workItemArtifactIds, errors))
    .filter((item): item is CompletionReviewRequiredTodo => !!item)
  const requiredTodoIdsFromRecords = requiredTodos.map((item) => item.todoInstanceId)
  if (new Set(requiredTodoIdsFromRecords).size !== requiredTodoIdsFromRecords.length) {
    errors.push('completionReviewScope.requiredTodos.todoInstanceId 不能重复')
  }
  if (requiredTodoInstanceIds.length !== requiredTodoIdsFromRecords.length
    || requiredTodoInstanceIds.some((todoId) => !requiredTodoIdsFromRecords.includes(todoId))) {
    errors.push('completionReviewScope.requiredTodos 必须精确覆盖 requiredTodoInstanceIds')
  }

  if (workItem.batchId && batchId && workItem.batchId !== batchId) errors.push('scope batchId 与工作项不一致')
  if (workItem.gameId && gameId && workItem.gameId !== gameId) errors.push('scope gameId 与工作项不一致')
  if (workItem.runId && runId && workItem.runId !== runId) errors.push('scope runId 与工作项不一致')
  if (currentSnapshotRunId && runId && currentSnapshotRunId !== runId) errors.push('scope runId 与 Manager 当前 snapshot 不一致')
  if (errors.length || !batchId || !gameId || !runId || !runAttemptId || !gameDayKey
    || !periodStartsAt || !periodEndsAt) return { errors }
  return {
    value: {
      schemaVersion: 3,
      batchId,
      gameId,
      runId,
      runAttemptId,
      attemptLineageIds,
      gameDayKey,
      periodStartsAt,
      periodEndsAt,
      requiredTodoInstanceIds,
      requiredTodos,
    },
    errors: [],
  }
}

function parseConstraint(value: unknown, label: string, errors: string[]): CompletionReviewMetricConstraint | undefined {
  const raw = objectValue(value)
  if (!raw) {
    errors.push(`${label} 必须是对象`)
    return undefined
  }
  exactKeys(raw, ['metric', 'operator', 'expected'], label, errors)
  const metric = opaqueId(raw.metric) ? raw.metric : undefined
  if (!metric) errors.push(`${label}.metric 必须是有界 opaque 名称`)
  const operator = raw.operator === 'equals' || raw.operator === 'at_least' ? raw.operator : undefined
  if (!operator) errors.push(`${label}.operator 只能是 equals 或 at_least`)
  const expected = raw.expected
  const expectedIsNumber = typeof expected === 'number' && Number.isFinite(expected) && !Number.isNaN(expected)
  const expectedIsBoolean = typeof expected === 'boolean'
  const expectedIsString = safeMetricString(expected)
  if (!expectedIsNumber && !expectedIsBoolean && !expectedIsString) {
    errors.push(`${label}.expected 必须是有界 number/string/boolean`)
  }
  if (operator === 'at_least' && !expectedIsNumber) errors.push(`${label}.at_least 只支持 number expected`)
  if (!metric || !operator || (!expectedIsNumber && !expectedIsBoolean && !expectedIsString)) return undefined
  return { metric, operator, expected: expected as number | string | boolean }
}

function parsePredicate(value: unknown, index: number, errors: string[]): CompletionReviewPredicateContract | undefined {
  const label = `completionReviewContract.predicates[${index}]`
  const raw = objectValue(value)
  if (!raw) {
    errors.push(`${label} 必须是对象`)
    return undefined
  }
  exactKeys(raw, ['predicateId', 'label', 'description', 'constraints', 'allowedArtifactKinds'], label, errors)
  const predicateId = opaqueId(raw.predicateId) ? raw.predicateId : undefined
  if (!predicateId) errors.push(`${label}.predicateId 必须是有界 opaque ID`)
  const displayLabel = raw.label === undefined || boundedText(raw.label, 160) ? raw.label as string | undefined : undefined
  if (raw.label !== undefined && displayLabel === undefined) errors.push(`${label}.label 必须是有界文本`)
  const description = raw.description === undefined || boundedText(raw.description, 1000)
    ? raw.description as string | undefined
    : undefined
  if (raw.description !== undefined && description === undefined) errors.push(`${label}.description 必须是有界文本`)
  const rawConstraints = Array.isArray(raw.constraints) ? raw.constraints : []
  if (!Array.isArray(raw.constraints) || rawConstraints.length < 1 || rawConstraints.length > 20) {
    errors.push(`${label}.constraints 必须包含 1–20 条约束`)
  }
  const constraints = rawConstraints.slice(0, 20)
    .map((item, constraintIndex) => parseConstraint(item, `${label}.constraints[${constraintIndex}]`, errors))
    .filter((item): item is CompletionReviewMetricConstraint => !!item)
  const rawKinds = Array.isArray(raw.allowedArtifactKinds) ? raw.allowedArtifactKinds : []
  const kinds = rawKinds.filter(artifactKind)
  if (!Array.isArray(raw.allowedArtifactKinds) || rawKinds.length < 1 || rawKinds.length > 20
    || kinds.length !== rawKinds.length || new Set(kinds).size !== kinds.length) {
    errors.push(`${label}.allowedArtifactKinds 必须是 1–20 个唯一有界 kind`)
  }
  if (!predicateId || constraints.length !== rawConstraints.length || !kinds.length) return undefined
  const metricTypes = new Map<string, string>()
  for (const constraint of constraints) {
    const valueType = typeof constraint.expected
    const previous = metricTypes.get(constraint.metric)
    if (previous && previous !== valueType) errors.push(`${label}.${constraint.metric} 的 expected 类型不一致`)
    metricTypes.set(constraint.metric, valueType)
  }
  return { predicateId, label: displayLabel, description, constraints, allowedArtifactKinds: kinds }
}

export function parseCompletionReviewContract(
  workItem?: AgentWorkItem,
  scope?: CompletionReviewScope,
): ValidationResult<CompletionReviewContract> {
  const errors: string[] = []
  if (!workItem || workItem.kind !== 'evidence_review') {
    return { errors: ['只有 evidence_review 工作项可以读取 CompletionReview 合同'] }
  }
  const raw = objectValue(workItem.result?.completionReviewContract)
  if (!raw) return { errors: ['工作项没有 Manager 签发的 completionReviewContract'] }
  exactKeys(raw, [
    'schemaVersion', 'policyId', 'policyVersion', 'gameId', 'cadence', 'timezone', 'resetTime',
    'periodStartsAt', 'periodEndsAt', 'policyStatus', 'unsupportedReason', 'todoReviewContract', 'predicates',
  ], 'completionReviewContract', errors)
  if (raw.schemaVersion !== 'completion-review-contract/v2') {
    errors.push('completionReviewContract.schemaVersion 不受支持')
  }
  const policyId = opaqueId(raw.policyId) ? raw.policyId : undefined
  const policyVersion = opaqueId(raw.policyVersion) ? raw.policyVersion : undefined
  const gameId = opaqueId(raw.gameId) ? raw.gameId : undefined
  if (!policyId) errors.push('completionReviewContract.policyId 必须是有界 opaque ID')
  if (!policyVersion) errors.push('completionReviewContract.policyVersion 必须是有界 opaque 版本')
  if (!gameId) errors.push('completionReviewContract.gameId 必须是有界 opaque ID')
  const cadence = raw.cadence === 'daily' || raw.cadence === 'weekly' ? raw.cadence : undefined
  if (!cadence) errors.push('completionReviewContract.cadence 只能是 daily 或 weekly')
  const timezone = boundedText(raw.timezone, 80) && /^(?:UTC|[A-Za-z_+-]+(?:\/[A-Za-z0-9_+-]+)+)$/.test(raw.timezone)
    ? raw.timezone
    : undefined
  if (!timezone) errors.push('completionReviewContract.timezone 不是有界 IANA timezone')
  const resetTime = typeof raw.resetTime === 'string' && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(raw.resetTime)
    ? raw.resetTime
    : undefined
  if (!resetTime) errors.push('completionReviewContract.resetTime 必须是 HH:mm')
  const periodStartsAt = awareIso(raw.periodStartsAt) ? raw.periodStartsAt : undefined
  const periodEndsAt = awareIso(raw.periodEndsAt) ? raw.periodEndsAt : undefined
  if (!periodStartsAt) errors.push('completionReviewContract.periodStartsAt 必须是带时区 ISO 时间')
  if (!periodEndsAt) errors.push('completionReviewContract.periodEndsAt 必须是带时区 ISO 时间')
  if (periodStartsAt && periodEndsAt && Date.parse(periodEndsAt) <= Date.parse(periodStartsAt)) {
    errors.push('completionReviewContract period 必须正向且非空')
  }
  const policyStatus = raw.policyStatus === 'supported' || raw.policyStatus === 'unsupported'
    ? raw.policyStatus
    : undefined
  if (!policyStatus) errors.push('completionReviewContract.policyStatus 不受支持')
  const unsupportedReason = raw.unsupportedReason === null && policyStatus === 'supported'
    ? ''
    : boundedText(raw.unsupportedReason, 1000, true) ? raw.unsupportedReason : undefined
  if (unsupportedReason === undefined) {
    errors.push('completionReviewContract.unsupportedReason 必须是有界文本（supported 可为 null）')
  }
  if (policyStatus === 'unsupported' && !unsupportedReason) errors.push('unsupported policy 必须提供 unsupportedReason')

  const rawTodoReviewContract = objectValue(raw.todoReviewContract)
  if (!rawTodoReviewContract) {
    errors.push('completionReviewContract.todoReviewContract 缺失')
  } else {
    exactKeys(rawTodoReviewContract, [
      'requiredForAccepted', 'acceptedVerdict', 'reasonCodeRequired',
      'artifactRefsRequired', 'allowedEvidenceContentTypes',
    ], 'completionReviewContract.todoReviewContract', errors)
    if (rawTodoReviewContract.requiredForAccepted !== true
      || rawTodoReviewContract.acceptedVerdict !== 'confirmed'
      || rawTodoReviewContract.reasonCodeRequired !== true
      || rawTodoReviewContract.artifactRefsRequired !== true) {
      errors.push('completionReviewContract.todoReviewContract 的验收闸门不受支持')
    }
    const allowedTypes = Array.isArray(rawTodoReviewContract.allowedEvidenceContentTypes)
      ? rawTodoReviewContract.allowedEvidenceContentTypes
      : []
    if (allowedTypes.length !== completionEvidenceContentTypes.size
      || new Set(allowedTypes).size !== allowedTypes.length
      || allowedTypes.some((value) => typeof value !== 'string' || !completionEvidenceContentTypes.has(value))
      || [...completionEvidenceContentTypes].some((value) => !allowedTypes.includes(value))) {
      errors.push('completionReviewContract.todoReviewContract.allowedEvidenceContentTypes 不受支持')
    }
  }

  const rawPredicates = Array.isArray(raw.predicates) ? raw.predicates : []
  if (!Array.isArray(raw.predicates) || rawPredicates.length > 20) {
    errors.push('completionReviewContract.predicates 必须是最多 20 项的数组')
  }
  const predicates = rawPredicates.slice(0, 20)
    .map((item, index) => parsePredicate(item, index, errors))
    .filter((item): item is CompletionReviewPredicateContract => !!item)
  if (new Set(predicates.map((item) => item.predicateId)).size !== predicates.length) {
    errors.push('completionReviewContract.predicateId 不能重复')
  }
  if (workItem.gameId && gameId && workItem.gameId !== gameId) errors.push('contract gameId 与工作项不一致')
  if (scope?.gameId && gameId && scope.gameId !== gameId) errors.push('contract gameId 与 scope 不一致')
  if (workItem.cadence && cadence && workItem.cadence !== cadence) errors.push('contract cadence 与工作项不一致')
  if (scope?.periodStartsAt && periodStartsAt && scope.periodStartsAt !== periodStartsAt) {
    errors.push('contract periodStartsAt 与 scope 不一致')
  }
  if (scope?.periodEndsAt && periodEndsAt && scope.periodEndsAt !== periodEndsAt) {
    errors.push('contract periodEndsAt 与 scope 不一致')
  }

  if (errors.length || !policyId || !policyVersion || !gameId || !cadence || !timezone || !resetTime
    || !periodStartsAt || !periodEndsAt || !policyStatus || unsupportedReason === undefined) return { errors }
  return {
    value: {
      schemaVersion: 'completion-review-contract/v2',
      policyId,
      policyVersion,
      gameId,
      cadence,
      timezone,
      resetTime,
      periodStartsAt,
      periodEndsAt,
      policyStatus,
      unsupportedReason,
      todoReviewContract: {
        requiredForAccepted: true,
        acceptedVerdict: 'confirmed',
        reasonCodeRequired: true,
        artifactRefsRequired: true,
        allowedEvidenceContentTypes: ['image/jpeg', 'image/png'],
      },
      predicates,
    },
    errors: [],
  }
}

export function completionMetricFields(predicate: CompletionReviewPredicateContract): CompletionMetricField[] {
  const grouped = new Map<string, CompletionReviewMetricConstraint[]>()
  for (const constraint of predicate.constraints) {
    const values = grouped.get(constraint.metric) ?? []
    values.push(constraint)
    grouped.set(constraint.metric, values)
  }
  return [...grouped.entries()].map(([metric, constraints]) => ({
    metric,
    valueType: typeof constraints[0].expected as CompletionMetricField['valueType'],
    constraints,
  }))
}

export function emptyCompletionReviewForm(
  contract?: CompletionReviewContract,
  scope?: CompletionReviewScope,
): CompletionReviewForm {
  return {
    evidenceIds: [],
    predicates: Object.fromEntries((contract?.predicates ?? []).map((predicate) => [
      predicate.predicateId,
      {
        metrics: Object.fromEntries(completionMetricFields(predicate).map((field) => [field.metric, null])),
        artifactRefs: [],
      },
    ])),
    todoReviews: Object.fromEntries((scope?.requiredTodos ?? []).map((todo) => [
      todo.todoInstanceId,
      {
        verdict: 'review_required',
        reasonCode: 'visual_confirmation_required',
        artifactRefs: [],
      },
    ])),
  }
}

export function workItemCompletionArtifacts(
  workItem: AgentWorkItem | undefined,
  artifacts: EvidenceArtifact[],
  scope: CompletionReviewScope | undefined,
): CompletionArtifactOption[] {
  if (!workItem || !scope) return []
  const claimed = new Set((workItem.artifactRefs ?? []).filter(opaqueId))
  const required = new Map(scope.requiredTodos.map((todo) => [todo.todoInstanceId, new Set(todo.artifactRefs)]))
  const lineage = new Set(scope.attemptLineageIds)
  const unique = new Map<string, CompletionArtifactOption>()
  for (const artifact of artifacts) {
    if (!claimed.has(artifact.artifactId) || !opaqueId(artifact.artifactId) || !artifactKind(artifact.kind)) continue
    if (artifact.gameId !== scope.gameId || artifact.runId !== scope.runId) continue
    if (!artifact.todoInstanceId || !required.get(artifact.todoInstanceId)?.has(artifact.artifactId)) continue
    if (!artifact.runAttemptId || !lineage.has(artifact.runAttemptId)) continue
    const contentType = typeof artifact.contentType === 'string' ? artifact.contentType.toLowerCase() : ''
    if (artifact.raw !== true || !completionEvidenceContentTypes.has(contentType)) continue
    unique.set(artifact.artifactId, {
      artifactId: artifact.artifactId,
      kind: artifact.kind,
      todoInstanceId: artifact.todoInstanceId,
      runAttemptId: artifact.runAttemptId,
      contentType: contentType as 'image/jpeg' | 'image/png',
      capturedAt: artifact.capturedAt,
    })
  }
  return [...unique.values()]
}

export function artifactOptionsForPredicate(
  predicate: CompletionReviewPredicateContract,
  artifacts: CompletionArtifactOption[],
): CompletionArtifactOption[] {
  const allowedKinds = new Set(predicate.allowedArtifactKinds)
  return artifacts.filter((artifact) => allowedKinds.has(artifact.kind))
}

export function completionReviewEvidenceIds(form: CompletionReviewForm): string[] {
  return [...new Set([
    ...form.evidenceIds,
    ...Object.values(form.predicates).flatMap((item) => item.artifactRefs),
    ...Object.values(form.todoReviews).flatMap((item) => item.artifactRefs),
  ])]
}

function reasonCode(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z][a-z0-9_.-]{0,159}$/.test(value)
}

function metricValue(
  value: StrictMetricValue | null | undefined,
  field: CompletionMetricField,
  label: string,
  errors: string[],
): StrictMetricValue | undefined {
  if (field.valueType === 'number' && typeof value === 'number' && Number.isFinite(value)) return value
  if (field.valueType === 'boolean' && typeof value === 'boolean') return value
  if (field.valueType === 'string' && safeMetricString(value)) return value
  errors.push(`${label}.${field.metric} 必须是有界 ${field.valueType}`)
  return undefined
}

function constraintSatisfied(value: StrictMetricValue, constraint: CompletionReviewMetricConstraint): boolean {
  if (constraint.operator === 'equals') return value === constraint.expected
  return typeof value === 'number' && typeof constraint.expected === 'number' && value >= constraint.expected
}

export function buildCompletionReviewSubmission(
  workItem: AgentWorkItem | undefined,
  form: CompletionReviewForm,
  artifacts: EvidenceArtifact[],
  decision: CompletionReviewDecision,
  currentSnapshotRunId?: string,
): ValidationResult<CompletionReviewSubmission> {
  const scopeResult = parseCompletionReviewScope(workItem, currentSnapshotRunId)
  const errors = [...scopeResult.errors]
  const scope = scopeResult.value
  const contractResult = parseCompletionReviewContract(workItem, scope)
  errors.push(...contractResult.errors)
  const contract = contractResult.value
  if (!scope || !contract) return { errors }
  const workItemArtifacts = workItemCompletionArtifacts(workItem, artifacts, scope)
  const workItemArtifactIds = new Set(workItemArtifacts.map((artifact) => artifact.artifactId))
  const evidenceIds = completionReviewEvidenceIds(form)
  for (const artifactId of evidenceIds) {
    if (!opaqueId(artifactId) || !workItemArtifactIds.has(artifactId)) {
      errors.push('CompletionReview evidenceIds 只能引用当前工作项的 opaque artifact')
    }
  }
  if (errors.length) return { errors }
  if (decision === 'review_required') {
    return {
      value: {
        gameId: scope.gameId,
        runId: scope.runId,
        runAttemptId: scope.runAttemptId,
        gameDayKey: scope.gameDayKey,
        predicates: [],
        todoReviews: [],
      },
      errors: [],
    }
  }

  const artifactById = new Map(workItemArtifacts.map((artifact) => [artifact.artifactId, artifact]))
  const todoReviews = scope.requiredTodos.flatMap((todo) => {
    const state = form.todoReviews[todo.todoInstanceId]
    if (!state) {
      if (decision === 'accepted') errors.push(`${todo.operation} 缺少逐 Todo 复核结论`)
      return []
    }
    const artifactRefs = [...new Set(state.artifactRefs)]
    if (!artifactRefs.length) {
      if (decision === 'accepted' || state.verdict === 'rejected') {
        errors.push(`${todo.operation} 至少选择一张属于本 Todo 的原始截图`)
      }
      return []
    }
    if (artifactRefs.length !== state.artifactRefs.length) errors.push(`${todo.operation} 不能重复选择截图`)
    if (!reasonCode(state.reasonCode)) errors.push(`${todo.operation} 的 reasonCode 必须是小写登记码`)
    for (const artifactId of artifactRefs) {
      const artifact = artifactById.get(artifactId)
      if (!artifact || artifact.todoInstanceId !== todo.todoInstanceId) {
        errors.push(`${todo.operation} 只能引用属于本 Todo 的原始截图`)
      }
    }
    if (decision === 'accepted') {
      if (todo.status !== 'completed') errors.push(`${todo.operation} 当前状态不是 completed`)
      if (state.verdict !== 'confirmed') errors.push(`${todo.operation} 必须明确标记为 confirmed`)
    }
    return [{
      todoInstanceId: todo.todoInstanceId,
      verdict: state.verdict,
      reasonCode: state.reasonCode,
      artifactRefs,
    }]
  })
  if (decision === 'accepted' && (!scope.requiredTodos.length || todoReviews.length !== scope.requiredTodos.length)) {
    errors.push('accepted CompletionReview 必须逐项覆盖全部 required Todo')
  }
  if (decision === 'rejected' && !todoReviews.some((item) => item.verdict === 'rejected')) {
    errors.push('rejected CompletionReview 至少需要一个带截图的 Todo rejected 结论')
  }
  if (contract.policyStatus === 'unsupported') {
    if (decision === 'accepted') return { errors: [`当前 policy 不支持完成验收：${contract.unsupportedReason}`] }
    if (errors.length) return { errors }
    return {
      value: {
        gameId: scope.gameId,
        runId: scope.runId,
        runAttemptId: scope.runAttemptId,
        gameDayKey: scope.gameDayKey,
        predicates: [],
        todoReviews,
      },
      errors: [],
    }
  }
  if (decision === 'accepted' && !evidenceIds.length) {
    return { errors: ['accepted CompletionReview 至少选择一个工作项 screenshot artifact'] }
  }

  if (decision === 'rejected') {
    if (errors.length) return { errors }
    return {
      value: {
        gameId: scope.gameId,
        runId: scope.runId,
        runAttemptId: scope.runAttemptId,
        gameDayKey: scope.gameDayKey,
        predicates: [],
        todoReviews,
      },
      errors: [],
    }
  }

  const predicates = contract.predicates.map((predicate) => {
    const label = predicate.label || predicate.predicateId
    const state = form.predicates[predicate.predicateId]
    if (!state) {
      errors.push(`${label} 缺少表单状态`)
      return undefined
    }
    const metrics: Record<string, StrictMetricValue> = {}
    for (const field of completionMetricFields(predicate)) {
      const value = metricValue(state.metrics[field.metric], field, label, errors)
      if (value === undefined) continue
      metrics[field.metric] = value
      if (decision === 'accepted') {
        for (const constraint of field.constraints) {
          if (!constraintSatisfied(value, constraint)) {
            errors.push(`${label}.${field.metric} 未满足 ${constraint.operator} ${String(constraint.expected)}`)
          }
        }
      }
    }
    const allowed = new Map(artifactOptionsForPredicate(predicate, workItemArtifacts)
      .map((artifact) => [artifact.artifactId, artifact]))
    const artifactRefs = [...new Set(state.artifactRefs)]
    if (!artifactRefs.length) errors.push(`${label} 至少选择一个合同允许的工作项 artifact`)
    if (artifactRefs.length !== state.artifactRefs.length) errors.push(`${label} 不能重复选择 artifact`)
    for (const artifactId of artifactRefs) {
      if (!opaqueId(artifactId) || !allowed.has(artifactId)) {
        errors.push(`${label} 只能引用 kind 受合同允许的工作项 opaque artifact`)
      }
      if (!evidenceIds.includes(artifactId)) {
        errors.push(`${label} 的 artifact 必须同时列入本次 CompletionReview evidenceIds`)
      }
    }
    return { predicateId: predicate.predicateId, metrics, artifactRefs }
  }).filter((item): item is CompletionPredicateReview => !!item)
  if (errors.length) return { errors }

  return {
    value: {
      gameId: scope.gameId,
      runId: scope.runId,
      runAttemptId: scope.runAttemptId,
      gameDayKey: scope.gameDayKey,
      predicates,
      todoReviews,
    },
    errors: [],
  }
}

export function blockersFromTodos(items: TodoInstance[]): CompletionBlockerView[] {
  return items
    .filter((item) => blockerStatuses.has(item.status) || item.risk === 'forbidden')
    .map((item) => ({
      blockerId: item.todoInstanceId,
      source: 'todo',
      gameId: item.gameId,
      title: item.title || item.operation,
      status: item.risk === 'forbidden' && !blockerStatuses.has(item.status) ? 'forbidden' : item.status,
      reason: item.reason,
      evidenceRefs: item.evidenceRefs,
    }))
}

export function blockersFromDiagnostics(items: TodoDiagnosticItem[]): CompletionBlockerView[] {
  return items
    .filter((item) => blockerStatuses.has(item.status ?? 'pending') || (item.humanRequiredCount ?? 0) > 0)
    .map((item) => ({
      blockerId: item.todoInstanceId,
      source: 'diagnosis',
      gameId: item.gameId,
      title: item.title ?? item.operation ?? item.todoInstanceId,
      status: (item.humanRequiredCount ?? 0) > 0 ? 'human_required' : item.status ?? 'blocked',
      reason: item.reason,
      evidenceRefs: item.evidenceRefs ?? [],
      humanRequiredCount: item.humanRequiredCount,
    }))
}

export function blockersFromBatchResult(result?: BatchResult): CompletionBlockerView[] {
  if (!result?.todoSnapshot || typeof result.todoSnapshot !== 'object') return []
  const instances = Object.values(result.todoSnapshot)
    .flatMap((snapshot) => Array.isArray(snapshot?.instances) ? snapshot.instances : [])
  return blockersFromTodos(instances)
}

export function awaitingCompletionReviewRunIds(batch?: BatchRun | null): string[] {
  const values = batch?.result?.awaitingCompletionReviewRunIds
  return Array.isArray(values) ? [...new Set(values.filter((value): value is string => typeof value === 'string'))] : []
}

export function batchRunIds(batch?: BatchRun | null): string[] {
  if (!batch?.result) return []
  const ids = new Set<string>(awaitingCompletionReviewRunIds(batch))
  for (const value of batch.result.finalGameRunIds ?? []) {
    if (typeof value === 'string') ids.add(value)
  }
  for (const contract of batch.result.completionContracts ?? []) {
    if (typeof contract?.runId === 'string') ids.add(contract.runId)
  }
  for (const value of Object.keys(batch.result.completionReviewWorkItemIds ?? {})) ids.add(value)
  return [...ids]
}

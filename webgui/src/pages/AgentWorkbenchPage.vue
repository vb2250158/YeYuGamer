<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import type {
  AgentWorkItem,
  AutomationAssessment,
  CapabilityDefinition,
  ClaimDecisionCreateRequest,
  CommandReceipt,
  CompletionAdjudication,
  CompletionReview,
  CompletionReviewPredicateContract,
  CompletionReviewRequiredTodo,
  EvidenceArtifact,
  JsonObject,
  WorkItemClaim,
} from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import JsonPanel from '../components/JsonPanel.vue'
import CompletionLedger from '../components/CompletionLedger.vue'
import { formatTime } from '../utils/format'
import {
  artifactContentPath,
  buildCapabilityArguments,
  capabilityClosure,
  capabilityInputFields,
} from '../utils/managerResources'
import { latestAutomationAssessment, todoDiagnostics } from '../utils/todos'
import type { TodoDiagnosticItem } from '../utils/todos'
import {
  buildTodoDiagnoses,
  createTodoDiagnosisDraft,
} from '../utils/agentDiagnostics'
import type { TodoDiagnosisDraft } from '../utils/agentDiagnostics'
import {
  blockersFromDiagnostics,
  artifactOptionsForPredicate,
  buildCompletionReviewSubmission,
  completionReviewEvidenceIds,
  completionMetricFields,
  emptyCompletionReviewForm,
  parseCompletionReviewContract,
  parseCompletionReviewScope,
  workItemCompletionArtifacts,
} from '../utils/completion'
import type {
  CompletionPredicateForm,
  CompletionReviewDecision,
  CompletionReviewForm,
  CompletionTodoReviewForm,
} from '../utils/completion'
import {
  canRecoverPageClaimSecret,
  createPageClaimSecretSession,
  newClaimIdempotencyKey,
} from '../utils/claimSecret'
import type { ClaimSecretContext } from '../utils/claimSecret'

const currentPrincipal = 'webgui'
const manager = useManagerStore()
const resource = useResource<AgentWorkItem>('/agent/work-items')
const claims = useResource<WorkItemClaim>('/claims?limit=100')
const capabilities = useResource<CapabilityDefinition>('/capabilities')
const assessments = useResource<AutomationAssessment>('/automation-assessments?limit=100')
const completionReviews = useResource<CompletionReview>('/completion-reviews?limit=500')
const completionAdjudications = useResource<CompletionAdjudication>('/completion-adjudications?limit=500')
const artifacts = useResource<EvidenceArtifact>('/artifacts?limit=500')
const selectedId = ref('')
const scene = ref('')
const delta = ref('')
const riskClass = ref('observe_only')
const evidenceVerdict = ref('needs_more_evidence')
const diagnosisDrafts = ref<TodoDiagnosisDraft[]>([])
const completionReviewForm = ref<CompletionReviewForm>(emptyCompletionReviewForm())
const capabilityRef = ref<string>()
const capabilityInputs = ref<Record<string, unknown>>({})
const lastCapabilityResult = ref<CommandReceipt | JsonObject>()
const busy = ref(false)
const now = ref(Date.now())
const claimSecretRevision = ref(0)
const claimSecret = createPageClaimSecretSession()
let clock: number | undefined

onMounted(() => { clock = window.setInterval(() => { now.value = Date.now() }, 10_000) })
onScopeDispose(() => {
  if (clock !== undefined) window.clearInterval(clock)
  clearClaimSecret()
})

watch(resource.items, (items) => {
  if (!items.some((item) => item.workItemId === selectedId.value)) selectedId.value = items[0]?.workItemId ?? ''
}, { immediate: true })

const selected = computed(() => resource.items.value.find((item) => item.workItemId === selectedId.value))
const selectedClaimId = computed(() => {
  const value = selected.value?.result?.activeClaimId
  return typeof value === 'string' ? value : undefined
})
const liveClaims = computed(() => claims.items.value.filter((claim) => {
  const expiry = claim.expiresAt ? Date.parse(claim.expiresAt) : Number.NaN
  return claim.workItemId === selectedId.value
    && claim.state === 'active'
    && Number.isFinite(expiry)
    && expiry > now.value
}))
const currentClaim = computed(() => {
  const byId = liveClaims.value.find((claim) => claim.claimId === selectedClaimId.value)
  return byId ?? liveClaims.value[0]
})
const activeClaim = computed(() => currentClaim.value?.claimant === currentPrincipal ? currentClaim.value : undefined)
const terminalWorkItem = computed(() => ['done', 'cancelled', 'resolved'].includes(selected.value?.state ?? ''))
const selectedTodoDiagnostics = computed(() => todoDiagnostics(selected.value?.result))
const isDiagnoseWorkItem = computed(() => selected.value?.kind === 'diagnose_game')
const diagnosticScopeKey = computed(() => selectedTodoDiagnostics.value.map((todo) => todo.todoInstanceId).join('\u0000'))
const diagnosisBuild = computed(() => buildTodoDiagnoses(diagnosisDrafts.value))
const selectedAssessments = computed(() => assessments.items.value.filter((item) =>
  item.workItemId === selectedId.value || selectedTodoDiagnostics.value.some((todo) => todo.todoInstanceId === item.todoInstanceId)))
const automatableItems = [
  { title: '可自动化', value: true },
  { title: '不可自动化', value: false },
]
const selectedCompletionReviews = computed(() => completionReviews.items.value.filter((item) =>
  item.workItemId === selectedId.value || (!!selected.value?.runId && item.runId === selected.value.runId)))
const selectedCompletionAdjudications = computed(() => completionAdjudications.items.value.filter((item) =>
  !!selected.value?.runId && item.runId === selected.value.runId))
const selectedBlockers = computed(() => blockersFromDiagnostics(selectedTodoDiagnostics.value))
const isCompletionReviewWorkItem = computed(() => selected.value?.kind === 'evidence_review')
const currentSnapshotRunId = computed(() => manager.snapshot.games
  .find((game) => game.gameId === selected.value?.gameId)?.runId)
const completionScope = computed(() => parseCompletionReviewScope(selected.value, currentSnapshotRunId.value))
const completionContract = computed(() => parseCompletionReviewContract(selected.value, completionScope.value.value))
const completionContextErrors = computed(() => [
  ...completionScope.value.errors,
  ...completionContract.value.errors,
])
const selectedCompletionArtifacts = computed(() => workItemCompletionArtifacts(
  selected.value,
  artifacts.items.value,
  completionScope.value.value,
))
const todoReviewVerdictItems = [
  { title: '画面明确证明完成', value: 'confirmed' },
  { title: '画面明确证明未完成', value: 'rejected' },
  { title: '画面不足，需要补证据', value: 'review_required' },
]

function assessmentFor(todo: TodoDiagnosticItem): AutomationAssessment | undefined {
  return todo.latestAutomationAssessment
    ?? latestAutomationAssessment(todo.todoInstanceId, selectedAssessments.value)
}

function diagnosisEvidenceItems(todoInstanceId: string) {
  const allowed = new Set(selected.value?.artifactRefs ?? [])
  return artifacts.items.value
    .filter((artifact) => allowed.has(artifact.artifactId) && artifact.todoInstanceId === todoInstanceId)
    .map((artifact) => ({
      title: `${artifact.artifactId} · ${artifact.kind ?? 'unknown'} · ${artifact.runAttemptId ?? 'no attempt'}`,
      value: artifact.artifactId,
    }))
}

function projectionText(value: JsonObject | undefined, key: string): string | undefined {
  const candidate = value?.[key]
  return typeof candidate === 'string' && candidate ? candidate : undefined
}
const selectedDecision = computed<CompletionReviewDecision>(() => evidenceVerdict.value === 'supports_completion'
  ? 'accepted'
  : evidenceVerdict.value === 'rejects_completion'
    ? 'rejected'
    : 'review_required')
const completionReviewBuild = computed(() => buildCompletionReviewSubmission(
  selected.value,
  completionReviewForm.value,
  artifacts.items.value,
  selectedDecision.value,
  currentSnapshotRunId.value,
))
const reviewRequiredCompletionBuild = computed(() => buildCompletionReviewSubmission(
  selected.value,
  completionReviewForm.value,
  artifacts.items.value,
  'review_required',
  currentSnapshotRunId.value,
))

function formForPredicate(predicate: CompletionReviewPredicateContract): CompletionPredicateForm {
  const existing = completionReviewForm.value.predicates[predicate.predicateId]
  if (existing) return existing
  const created = {
    metrics: Object.fromEntries(completionMetricFields(predicate).map((field) => [field.metric, null])),
    artifactRefs: [],
  }
  completionReviewForm.value.predicates[predicate.predicateId] = created
  return created
}

function booleanMetricValue(
  predicate: CompletionReviewPredicateContract,
  metric: string,
): boolean | null {
  const value = formForPredicate(predicate).metrics[metric]
  return typeof value === 'boolean' ? value : null
}

function setBooleanMetricValue(
  predicate: CompletionReviewPredicateContract,
  metric: string,
  value: unknown,
) {
  if (typeof value === 'boolean' || value === null) {
    formForPredicate(predicate).metrics[metric] = value
  }
}

function artifactItemsForPredicate(predicate: CompletionReviewPredicateContract) {
  return artifactOptionsForPredicate(predicate, selectedCompletionArtifacts.value)
    .map((artifact) => ({
      title: `${artifact.todoInstanceId} · ${artifact.artifactId} · ${artifact.kind}`,
      value: artifact.artifactId,
    }))
}

function formForTodo(todo: CompletionReviewRequiredTodo): CompletionTodoReviewForm {
  const existing = completionReviewForm.value.todoReviews[todo.todoInstanceId]
  if (existing) return existing
  const created: CompletionTodoReviewForm = {
    verdict: 'review_required',
    reasonCode: 'visual_confirmation_required',
    artifactRefs: [],
  }
  completionReviewForm.value.todoReviews[todo.todoInstanceId] = created
  return created
}

function setTodoReviewVerdict(todo: CompletionReviewRequiredTodo, value: unknown): void {
  if (value !== 'confirmed' && value !== 'rejected' && value !== 'review_required') return
  const form = formForTodo(todo)
  form.verdict = value
  form.reasonCode = value === 'confirmed'
    ? 'visual_completion_confirmed'
    : value === 'rejected'
      ? 'visual_completion_rejected'
      : 'visual_confirmation_required'
}

function todoArtifacts(todoInstanceId: string) {
  return selectedCompletionArtifacts.value.filter((artifact) => artifact.todoInstanceId === todoInstanceId)
}

function todoEvidenceItems(todoInstanceId: string) {
  return todoArtifacts(todoInstanceId).map((artifact) => ({
    title: `${artifact.artifactId} · ${artifact.runAttemptId} · ${artifact.contentType}`,
    value: artifact.artifactId,
  }))
}

function constraintText(constraint: CompletionReviewPredicateContract['constraints'][number]): string {
  return `${constraint.metric} ${constraint.operator} ${String(constraint.expected)}`
}

function activeClaimContext(): ClaimSecretContext | undefined {
  if (!selected.value || !activeClaim.value) return undefined
  return {
    workItemId: selected.value.workItemId,
    claimId: activeClaim.value.claimId,
    principal: currentPrincipal,
  }
}

const hasUsableClaim = computed(() => {
  claimSecretRevision.value
  const context = activeClaimContext()
  return !!context && claimSecret.has(context)
})
const needsClaimRecovery = computed(() => canRecoverPageClaimSecret(
  currentClaim.value,
  currentPrincipal,
  hasUsableClaim.value,
))
const canClaim = computed(() => !!selected.value
  && !terminalWorkItem.value
  && !claims.error.value
  && (!currentClaim.value || needsClaimRecovery.value))
const claimActionText = computed(() => {
  if (needsClaimRecovery.value) return '重新领取 / 恢复凭据'
  if (hasUsableClaim.value) return '已领取'
  return '领取'
})

function activeClaimToken(): string | undefined {
  const context = activeClaimContext()
  return context ? claimSecret.tokenFor(context) : undefined
}

function clearClaimSecret(): void {
  claimSecret.clear()
  claimSecretRevision.value += 1
}

const capabilityByRef = computed(() => new Map(capabilities.items.value.map((capability) => [
  `${capability.capabilityId}@${capability.version}`,
  capability,
])))
const capabilityItems = computed(() => (selected.value?.allowedCapabilityRefs ?? []).map((reference) => {
  const definition = capabilityByRef.value.get(reference)
  const state = !definition ? 'Manager 未登记' : definition.enabled ? '可用' : '已禁用'
  return {
    title: `${definition?.displayName ?? reference} · ${state}`,
    value: reference,
  }
}))
const selectedCapability = computed(() => capabilityRef.value ? capabilityByRef.value.get(capabilityRef.value) : undefined)
const inputFields = computed(() => capabilityInputFields(selectedCapability.value))
const argumentValidation = computed(() => buildCapabilityArguments(selectedCapability.value, capabilityInputs.value))
const selectedClosure = computed(() => capabilityClosure(selectedCapability.value?.capabilityId))
const readOnlyCapability = computed(() => selectedCapability.value?.requiresIdempotencyKey === false)

function nestedString(item: AgentWorkItem, key: string): string | undefined {
  const direct = item[key]
  if (typeof direct === 'string') return direct
  const result = item.result?.[key]
  return typeof result === 'string' ? result : undefined
}

function prefilledInputs(): Record<string, unknown> {
  const item = selected.value
  if (!item) return {}
  const values: Record<string, unknown> = {}
  for (const field of capabilityInputFields(selectedCapability.value)) {
    if (field.name === 'gameId' && item.gameId) values[field.name] = item.gameId
    else if (field.name === 'runId' && item.runId) values[field.name] = item.runId
    else if (field.name === 'batchId' && item.batchId) values[field.name] = item.batchId
    else if (field.name === 'artifactId' && item.artifactRefs?.[0]) values[field.name] = item.artifactRefs[0]
    else if (field.name === 'artifactIds' && item.artifactRefs?.length) values[field.name] = item.artifactRefs.join(', ')
    else if (field.name === 'gameIds' && item.gameId) values[field.name] = item.gameId
    else if (field.name === 'verdict') values[field.name] = evidenceVerdict.value
    else {
      const value = nestedString(item, field.name)
      if (value) values[field.name] = value
    }
  }
  return values
}

watch(selected, (item) => {
  capabilityRef.value = item?.allowedCapabilityRefs?.[0]
  scene.value = ''
  delta.value = ''
  lastCapabilityResult.value = undefined
  completionReviewForm.value = emptyCompletionReviewForm(
    completionContract.value.value,
    completionScope.value.value,
  )
})
watch([selectedId, diagnosticScopeKey], () => {
  const allowed = new Set(selected.value?.artifactRefs ?? [])
  diagnosisDrafts.value = selectedTodoDiagnostics.value.map((item) => createTodoDiagnosisDraft(
    item,
    (item.evidenceRefs ?? []).filter((artifactId) => allowed.has(artifactId)),
  ))
}, { immediate: true })
watch(selectedId, clearClaimSecret)
watch([selectedId, capabilityRef, selectedCapability], () => { capabilityInputs.value = prefilledInputs() })

const claimExplanation = computed(() => {
  if (claims.error.value) return `无法核验 Claim：${claims.error.value}`
  if (terminalWorkItem.value) return '工作项已结束，不能再次领取。'
  if (currentClaim.value && currentClaim.value.claimant !== currentPrincipal) {
    return `有效 Claim 属于 ${currentClaim.value.claimant}，WebGUI 不会使用别的主体的 fencing token。`
  }
  if (!activeClaim.value) return '尚无属于当前 WebGUI 的有效 Claim；写操作保持禁用。'
  if (!hasUsableClaim.value) {
    return 'Manager 中存在属于当前 WebGUI 的有效 Claim，但当前页面没有它的 fencing token。可重新领取恢复当前页凭据；在新的直接响应抵达前，决策和写入型能力调用保持禁用。'
  }
  return `Claim 有效，截止 ${formatTime(activeClaim.value.expiresAt)}。`
})

const capabilityUnavailableReason = computed(() => {
  if (!capabilityRef.value) return '工作项没有选择允许的 capability。'
  if (capabilities.error.value) return `无法读取 Manager capability 目录：${capabilities.error.value}`
  if (!selectedCapability.value) return '该 capability 虽在工作项 allowed 列表中，但当前 Manager 没有登记对应版本。'
  if (selectedCapability.value.enabled !== true) {
    if (selectedCapability.value.capabilityId.endsWith('.run')) return 'Manager 已禁用实际执行能力；当前只可做安全计划或诊断。'
    return 'Manager capability 目录将该能力标记为 disabled。'
  }
  if (readOnlyCapability.value) {
    return manager.supports('/snapshot', 'get') ? undefined : '当前 Manager OpenAPI 没有对应读取端点。'
  }
  if (!manager.supports('/capability-invocations', 'post')) return '当前 Manager OpenAPI 没有 typed capability invocation 端点。'
  if (!hasUsableClaim.value) return claimExplanation.value
  if (argumentValidation.value.errors.length) return argumentValidation.value.errors.join('；')
  return undefined
})

const capabilityClosureExplanation = computed(() => {
  const id = selectedCapability.value?.capabilityId
  if (!id) return '请选择工作项 allowed 列表中的 capability。'
  if (selectedCapability.value?.enabled !== true) {
    return `Manager 已禁用该 capability，本次不会创建 invocation 或 request。${selectedCapability.value?.description ?? ''}`
  }
  if (selectedClosure.value === 'read') return '这是只读能力：WebGUI 调用 Manager snapshot GET，不创建 mutation receipt。'
  if (selectedClosure.value === 'execution-request') return '这是执行请求；receipt 只证明 Manager 受理，仍需同 run 的完成合同和证据才能判定完成。'
  if (id === 'adapter.canary.request') return '该能力运行 Manager-owned 诊断 Canary，只检查 Host、固定入口、哈希和执行门，不启动 Adapter 或游戏。'
  if (selectedClosure.value === 'domain') return '该能力会创建对应的 Manager 领域记录；计划或工作项创建成功不等于游戏完成。'
  return '当前后端只会创建 planned capability-request 记录；它不代表维修、复核、回滚或其他领域动作已经执行。'
})

const capabilityActionText = computed(() => {
  if (readOnlyCapability.value) return '读取 Manager 快照'
  if (selectedClosure.value === 'request-record') return '创建能力请求记录'
  if (selectedClosure.value === 'execution-request') return '提交执行请求'
  return '调用 Manager 领域能力'
})

const decisionUnavailableReason = computed(() => {
  if (!scene.value || !delta.value) return '必须填写 scene 与 delta。'
  if (!hasUsableClaim.value) return claimExplanation.value
  if (diagnosisBuild.value.errors.length) return diagnosisBuild.value.errors.join('；')
  if (isDiagnoseWorkItem.value && diagnosisBuild.value.value.length === 0) return '诊断工作项至少需要一项 typed Todo 判断。'
  if (isDiagnoseWorkItem.value && selectedDecision.value === 'accepted'
    && diagnosisBuild.value.value.length !== selectedTodoDiagnostics.value.length) {
    return 'accepted 的诊断工作项必须覆盖冻结范围内的全部 Todo。'
  }
  if (!isCompletionReviewWorkItem.value) return undefined
  if (artifacts.loading.value) return '正在读取工作项 artifact 台账。'
  if (artifacts.error.value) return `无法读取工作项 artifact 台账：${artifacts.error.value}`
  if (completionReviews.loading.value || completionAdjudications.loading.value) return '正在读取当前完成复核台账。'
  if (completionReviews.error.value || completionAdjudications.error.value) {
    return `无法读取当前完成复核台账：${completionReviews.error.value ?? completionAdjudications.error.value}`
  }
  if (completionReviewBuild.value.errors.length) return completionReviewBuild.value.errors.join('；')
  return undefined
})

const requestMoreEvidenceUnavailableReason = computed(() => {
  if (!hasUsableClaim.value) return claimExplanation.value
  if (isDiagnoseWorkItem.value && diagnosisBuild.value.errors.length) return diagnosisBuild.value.errors.join('；')
  if (isDiagnoseWorkItem.value && diagnosisBuild.value.value.length === 0) return '请至少提交一项 unknown/unsupported 诊断并说明缺少什么证据。'
  if (!isCompletionReviewWorkItem.value) return undefined
  if (artifacts.loading.value) return '正在读取工作项 artifact 台账。'
  if (artifacts.error.value) return `无法读取工作项 artifact 台账：${artifacts.error.value}`
  if (reviewRequiredCompletionBuild.value.errors.length) return reviewRequiredCompletionBuild.value.errors.join('；')
  return undefined
})

async function refreshAll(): Promise<void> {
  await Promise.all([
    resource.load(),
    claims.load(),
    capabilities.load(),
    assessments.load(),
    completionReviews.load(),
    completionAdjudications.load(),
    artifacts.load(),
  ])
}

async function claim(): Promise<void> {
  if (!selected.value || !canClaim.value) return
  const workItemId = selected.value.workItemId
  const recovering = needsClaimRecovery.value
  busy.value = true
  try {
    const receipt = await manager.submitCommand(recovering ? '恢复当前页 Claim 凭据' : '领取 Agent 工作项', 'POST', `/agent/work-items/${encodeURIComponent(workItemId)}/claims`, {
      claimant: currentPrincipal,
      leaseSeconds: 300,
    }, { idempotencyKey: newClaimIdempotencyKey() })
    claimSecret.captureDirectReceipt(receipt, { workItemId, principal: currentPrincipal })
    claimSecretRevision.value += 1
    await Promise.all([resource.load(), claims.load()])
  } finally {
    busy.value = false
  }
}

async function decide(): Promise<void> {
  const fencingToken = activeClaimToken()
  if (!selected.value || !activeClaim.value || !fencingToken || decisionUnavailableReason.value) return
  busy.value = true
  try {
    const decision = selectedDecision.value
    const completionReview = isCompletionReviewWorkItem.value
      ? completionReviewBuild.value.value
      : undefined
    const diagnosisEvidenceIds = diagnosisBuild.value.value.flatMap((item) => item.evidenceIds)
    const evidenceIds = isCompletionReviewWorkItem.value
      ? [...new Set([...completionReviewEvidenceIds(completionReviewForm.value), ...diagnosisEvidenceIds])]
      : [...new Set([...(selected.value.artifactRefs ?? []), ...diagnosisEvidenceIds])]
    const request: ClaimDecisionCreateRequest = {
      claimId: activeClaim.value.claimId,
      fencingToken,
      decision,
      reason: `${scene.value}；变化：${delta.value}；风险：${riskClass.value}；证据判断：${evidenceVerdict.value}`,
      evidenceIds,
      requestedBy: currentPrincipal,
      todoDiagnoses: diagnosisBuild.value.value,
      ...(completionReview ? { completionReview } : {}),
    }
    const receipt = await manager.submitCommand('提交 Agent 判断', 'POST', '/claims/decisions', { ...request })
    if (receipt) {
      clearClaimSecret()
      await Promise.all([
        resource.load(),
        claims.load(),
        assessments.load(),
        completionReviews.load(),
        completionAdjudications.load(),
      ])
    }
  } finally {
    busy.value = false
  }
}

async function requestMoreEvidence(): Promise<void> {
  const fencingToken = activeClaimToken()
  if (!selected.value || !activeClaim.value || !fencingToken || requestMoreEvidenceUnavailableReason.value) return
  const completionReview = isCompletionReviewWorkItem.value
    ? reviewRequiredCompletionBuild.value.value
    : undefined
  const diagnosisEvidenceIds = diagnosisBuild.value.value.flatMap((item) => item.evidenceIds)
  const evidenceIds = isCompletionReviewWorkItem.value
    ? [...new Set([...completionReviewEvidenceIds(completionReviewForm.value), ...diagnosisEvidenceIds])]
    : [...new Set([...(selected.value.artifactRefs ?? []), ...diagnosisEvidenceIds])]
  busy.value = true
  try {
    const request: ClaimDecisionCreateRequest = {
      claimId: activeClaim.value.claimId,
      fencingToken,
      decision: 'review_required',
      reason: `场景：${scene.value || '未能确认'}；变化：${delta.value || '观察不足'}；请求：同 run 新鲜画面与窗口快照`,
      evidenceIds,
      requestedBy: currentPrincipal,
      todoDiagnoses: isDiagnoseWorkItem.value ? diagnosisBuild.value.value : [],
      ...(completionReview ? { completionReview } : {}),
    }
    const receipt = await manager.submitCommand('请求补充证据', 'POST', '/claims/decisions', { ...request })
    if (receipt) {
      clearClaimSecret()
      await Promise.all([
        resource.load(),
        claims.load(),
        completionReviews.load(),
        completionAdjudications.load(),
      ])
    }
  } finally {
    busy.value = false
  }
}

async function requestCapability(): Promise<void> {
  if (!selected.value || !selectedCapability.value || capabilityUnavailableReason.value) return
  busy.value = true
  try {
    if (readOnlyCapability.value) {
      await manager.refresh()
      lastCapabilityResult.value = manager.snapshot as unknown as JsonObject
      return
    }
    const fencingToken = activeClaimToken()
    if (!activeClaim.value || !fencingToken) return
    const receipt = await manager.submitCommand('调用已登记能力', 'POST', '/capability-invocations', {
      capability: selectedCapability.value.capabilityId,
      arguments: argumentValidation.value.arguments,
      requestedBy: currentPrincipal,
      workItemId: selected.value.workItemId,
      claimId: activeClaim.value.claimId,
      fencingToken,
    })
    if (receipt) {
      lastCapabilityResult.value = receipt
      await Promise.all([resource.load(), claims.load()])
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <PageHeader
    eyebrow="AGENT / WORK ITEMS"
    title="Agent 工作台"
    description="Agent 只能领取耐久工作项、读取 Manager artifact、提交结构化判断，并在有效 Claim 内调用工作项明确允许的 capability。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value || claims.loading.value || capabilities.loading.value || assessments.loading.value || completionReviews.loading.value || completionAdjudications.loading.value || artifacts.loading.value" @click="refreshAll">刷新</v-btn>
  </PageHeader>

  <ResourceState
    :loading="resource.loading.value"
    :error="resource.error.value"
    :empty="!resource.items.value.length"
    empty-title="没有待处理的 Agent 工作项"
    empty-detail="这里为空表示暂时没有观察或 Incident 需要 Agent，不代表今日全部完成。"
    @retry="refreshAll"
  >
    <v-alert v-if="claims.error.value || capabilities.error.value || assessments.error.value || completionReviews.error.value || completionAdjudications.error.value || artifacts.error.value" type="error" variant="tonal" class="mb-4">
      {{ claims.error.value ?? capabilities.error.value ?? assessments.error.value ?? completionReviews.error.value ?? completionAdjudications.error.value ?? artifacts.error.value }}。涉及 Claim、capability、诊断、artifact 或完成复核的写操作已按失败关闭。
    </v-alert>
    <div class="content-grid">
      <v-card class="panel span-5">
        <div class="panel-title"><h2>工作项队列</h2><span class="soft-note">{{ resource.items.value.length }} 项</span></div>
        <div class="work-list">
          <button
            v-for="item in resource.items.value"
            :key="item.workItemId"
            :class="['work-row', { active: item.workItemId === selectedId }]"
            @click="selectedId = item.workItemId"
          >
            <div><strong>{{ item.kind }}</strong><small>{{ item.gameId ?? 'global' }} · {{ item.workItemId }}</small></div>
            <StatusBadge :state="item.state ?? 'open'" small />
          </button>
        </div>
      </v-card>

      <v-card v-if="selected" class="panel span-7">
        <div class="panel-title"><h2>判断与能力调用</h2><StatusBadge :state="selected.state ?? 'open'" /></div>
        <div class="panel-body">
          <v-alert :type="hasUsableClaim ? 'info' : 'warning'" variant="tonal" class="mb-4">{{ claimExplanation }}</v-alert>
          <section class="todo-diagnostics mb-5">
            <div class="diagnostic-heading">
              <strong>Manager Todo 诊断上下文</strong>
              <span>{{ selectedTodoDiagnostics.length }} 个 scoped Todo</span>
            </div>
            <p v-if="!selectedTodoDiagnostics.length" class="soft-note">这个工作项没有携带 <code>todoDifficulty</code> / <code>todoSnapshot</code>；Agent 不应凭空推测已完成步骤。</p>
            <article v-for="todo in selectedTodoDiagnostics" :key="todo.todoInstanceId" class="diagnostic-todo">
              <div>
                <strong>{{ todo.title ?? todo.operation ?? todo.todoInstanceId }}</strong>
                <small>{{ todo.gameId ?? selected.gameId ?? 'global' }} · {{ todo.operation ?? 'operation unknown' }} · 尝试 {{ todo.attempts ?? '未提供' }}</small>
                <p v-if="todo.reason">{{ todo.reason }}</p>
                <p v-if="todo.blockedCount || todo.reviewRequiredCount || todo.humanRequiredCount || todo.retryableFailureCount">
                  阻塞 {{ todo.blockedCount ?? 0 }} · 待复核 {{ todo.reviewRequiredCount ?? 0 }} · 人工处理 {{ todo.humanRequiredCount ?? 0 }} · 可重试失败 {{ todo.retryableFailureCount ?? 0 }}
                  <span v-if="todo.latestState"> · 最近 {{ todo.latestState }}<template v-if="todo.latestReasonCode"> / {{ todo.latestReasonCode }}</template></span>
                </p>
                <p v-if="todo.evidenceRefs?.length">证据：{{ todo.evidenceRefs.join(' · ') }}</p>
                <p v-if="todo.dispatchDisposition">
                  Manager 调度：{{ todo.dispatchDisposition }}<template v-if="todo.dispatchReasonCode"> / {{ todo.dispatchReasonCode }}</template><template v-if="todo.dispatchReason"> · {{ todo.dispatchReason }}</template>
                </p>
                <p v-if="todo.actionAvailability?.nextAction">Manager 下一动作：{{ todo.actionAvailability.nextAction }}</p>
                <p v-if="todo.latestTodoAttempt">
                  最近 TodoAttempt：{{ todo.latestTodoAttempt.state }} · {{ todo.latestTodoAttempt.reasonCode || '无 reasonCode' }}<template v-if="todo.latestTodoAttempt.reason"> · {{ todo.latestTodoAttempt.reason }}</template>
                </p>
                <p v-if="todo.activeBlocker">
                  活跃 Blocker：{{ projectionText(todo.activeBlocker, 'kind') ?? 'kind 未提供' }}<template v-if="projectionText(todo.activeBlocker, 'code')"> / {{ projectionText(todo.activeBlocker, 'code') }}</template><template v-if="projectionText(todo.activeBlocker, 'reason')"> · {{ projectionText(todo.activeBlocker, 'reason') }}</template>
                </p>
                <p v-if="todo.recentExecutionFacts?.length">近期执行事实：{{ todo.recentExecutionFacts.map((fact) => projectionText(fact, 'factType') ?? 'unknown').join(' · ') }}</p>
                <p v-if="assessmentFor(todo)">
                  最近 AutomationAssessment：{{ assessmentFor(todo)?.difficulty }} · 自动化 {{ assessmentFor(todo)?.automatable === true ? '是' : assessmentFor(todo)?.automatable === false ? '否' : 'Manager 未提供' }}<template v-if="assessmentFor(todo)?.issue"> · 问题：{{ assessmentFor(todo)?.issue }}</template><template v-if="assessmentFor(todo)?.recommendation"> · 建议：{{ assessmentFor(todo)?.recommendation }}</template>
                </p>
              </div>
              <div class="diagnostic-badges">
                <StatusBadge :state="todo.status ?? 'unknown'" small />
                <StatusBadge :state="todo.automationDifficulty ?? 'unknown'" small />
              </div>
            </article>
          </section>
          <section v-if="selectedTodoDiagnostics.length" class="todo-diagnostics mb-5">
            <div class="diagnostic-heading">
              <strong>结构化 AutomationAssessment</strong>
              <span>{{ diagnosisDrafts.filter((item) => item.included).length }} / {{ diagnosisDrafts.length }} 项纳入本次判断；不改变 Todo 状态</span>
            </div>
            <article v-for="draft in diagnosisDrafts" :key="draft.todoInstanceId" class="diagnosis-editor">
              <div class="diagnosis-editor-heading">
                <div>
                  <strong>{{ selectedTodoDiagnostics.find((item) => item.todoInstanceId === draft.todoInstanceId)?.title ?? draft.todoInstanceId }}</strong>
                  <small class="mono">{{ draft.todoInstanceId }}</small>
                </div>
                <v-checkbox v-model="draft.included" label="纳入本次诊断" hide-details density="compact" />
              </div>
              <div class="diagnosis-editor-grid" :class="{ disabled: !draft.included }">
                <v-select
                  v-model="draft.difficulty"
                  :disabled="!draft.included"
                  :items="['easy','moderate','hard','unsupported','unknown']"
                  label="自动化难度"
                  variant="outlined"
                />
                <v-select
                  v-model="draft.automatable"
                  :disabled="!draft.included"
                  :items="automatableItems"
                  clearable
                  label="是否可自动化（需明确判断）"
                  variant="outlined"
                />
                <v-text-field
                  v-model.number="draft.confidence"
                  :disabled="!draft.included"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  label="置信度"
                  variant="outlined"
                />
                <v-text-field v-model="draft.failureStage" :disabled="!draft.included" maxlength="160" label="失败阶段" variant="outlined" />
                <v-select
                  v-model="draft.evidenceIds"
                  :disabled="!draft.included"
                  :items="diagnosisEvidenceItems(draft.todoInstanceId)"
                  label="本 Todo 证据（Manager lineage）"
                  multiple
                  chips
                  clearable
                  variant="outlined"
                />
                <v-textarea
                  v-model="draft.basisText"
                  :disabled="!draft.included"
                  label="判断依据（每行一条）"
                  rows="3"
                  variant="outlined"
                />
                <v-textarea v-model="draft.issue" :disabled="!draft.included" maxlength="1000" label="具体问题（issue）" rows="3" variant="outlined" />
                <v-textarea v-model="draft.recommendation" :disabled="!draft.included" maxlength="1000" label="建议改进" rows="3" variant="outlined" />
              </div>
            </article>
            <v-alert v-if="diagnosisBuild.errors.length" type="warning" variant="tonal" class="mb-4">
              {{ diagnosisBuild.errors.join('；') }}。WebGUI 只校验 typed 输入完整性，不替 Manager 或 Agent 补业务判断。
            </v-alert>
            <article v-for="assessment in selectedAssessments" :key="assessment.assessmentId" class="diagnostic-todo">
              <div>
                <strong>{{ assessment.difficulty }} · 自动化 {{ assessment.automatable === true ? '是' : assessment.automatable === false ? '否' : 'Manager 未提供' }} · {{ assessment.failureStage || '未标阶段' }}</strong>
                <small>{{ assessment.todoInstanceId }} · 置信度 {{ assessment.confidence }} · {{ formatTime(assessment.createdAt) }}</small>
                <p>{{ assessment.basis.join('；') }}</p>
                <p v-if="assessment.issue">问题：{{ assessment.issue }}</p>
                <p v-if="assessment.recommendation">建议：{{ assessment.recommendation }}</p>
              </div>
              <StatusBadge :state="assessment.difficulty" small />
            </article>
          </section>
          <div class="form-row">
            <v-text-field v-model="scene" label="场景（scene）" variant="outlined" density="comfortable" />
            <v-text-field v-model="delta" label="变化（delta）" variant="outlined" density="comfortable" />
            <v-select v-model="riskClass" :items="['observe_only','safe_navigation','routine_action','approval_required']" label="风险类别" variant="outlined" />
            <v-select v-model="evidenceVerdict" :items="['needs_more_evidence','startup_progress_only','supports_completion','rejects_completion']" label="证据判断" variant="outlined" />
          </div>

          <div v-if="selected.artifactRefs?.length" class="artifact-links mb-4">
            <span class="soft-note">工作项证据（只通过 Manager content 端点读取）：</span>
            <v-btn
              v-for="artifactId in selected.artifactRefs"
              :key="artifactId"
              size="small"
              variant="tonal"
              :disabled="!manager.supports('/artifacts/{artifactId}/content', 'get')"
              :href="artifactContentPath(artifactId)"
              target="_blank"
              rel="noopener noreferrer"
            >{{ artifactId }}</v-btn>
          </div>

          <section v-if="isCompletionReviewWorkItem" class="completion-form mb-5">
            <div class="diagnostic-heading">
              <strong>结构化 CompletionReview</strong>
              <span>每个 required Todo 都要单独看图、给结论并绑定同 Todo 原始截图</span>
            </div>
            <v-alert v-if="completionContextErrors.length" type="error" variant="tonal" class="mb-4">
              {{ completionContextErrors.join('；') }}。缺失或畸形合同会失败关闭，前端不会猜测 scope、谓词或指标。
            </v-alert>
            <template v-else-if="completionScope.value && completionContract.value">
              <div class="scope-grid mb-4">
                <span>Batch <code>{{ completionScope.value.batchId }}</code></span>
                <span>Game <code>{{ completionScope.value.gameId }}</code></span>
                <span>Run <code>{{ completionScope.value.runId }}</code></span>
                <span>Attempt <code>{{ completionScope.value.runAttemptId }}</code></span>
                <span>GameDay <code>{{ completionScope.value.gameDayKey }}</code></span>
                <span>Policy <code>{{ completionContract.value.policyId }}@{{ completionContract.value.policyVersion }}</code></span>
                <span>Cadence <code>{{ completionContract.value.cadence }}</code></span>
                <span>Reset <code>{{ completionContract.value.timezone }} · {{ completionContract.value.resetTime }}</code></span>
                <span>Period <code>{{ completionContract.value.periodStartsAt }} → {{ completionContract.value.periodEndsAt }}</code></span>
              </div>
              <v-alert v-if="completionContract.value.policyStatus === 'unsupported'" type="warning" variant="tonal" class="mb-4">
                Manager 将该 policy 标记为 unsupported：{{ completionContract.value.unsupportedReason }}。只允许 rejected 或 review_required，accepted 保持禁用。
              </v-alert>

              <v-alert v-if="selectedDecision === 'accepted'" type="warning" variant="tonal" class="mb-4">
                accepted 不是“有一张截图就算完成”：下列每个 required Todo 都必须是 completed、明确标为 confirmed，并至少绑定一张归属于本 Todo 的同 run 原始 PNG/JPEG。Manager 还会复查文件实体、哈希、时间、TodoAttempt 归属和重复画面。
              </v-alert>

              <div class="todo-review-form mb-4">
                <article
                  v-for="todo in completionScope.value.requiredTodos"
                  :key="todo.todoInstanceId"
                  class="todo-review-card"
                >
                  <div class="todo-review-heading">
                    <div>
                      <strong>{{ todo.operation }}</strong>
                      <small>{{ todo.todoInstanceId }}</small>
                    </div>
                    <StatusBadge :state="todo.status" small />
                  </div>
                  <v-alert v-if="!todoArtifacts(todo.todoInstanceId).length" type="warning" variant="tonal" density="compact">
                    这个 Todo 没有通过实体、归属与原始图片筛选的截图，不能确认为完成；请选择“需要补证据”。
                  </v-alert>
                  <div v-else class="todo-shot-grid">
                    <a
                      v-for="artifact in todoArtifacts(todo.todoInstanceId)"
                      :key="artifact.artifactId"
                      :href="artifactContentPath(artifact.artifactId)"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="todo-shot"
                    >
                      <v-img :src="artifactContentPath(artifact.artifactId)" height="126" cover />
                      <span>{{ artifact.artifactId }}</span>
                      <small>{{ artifact.runAttemptId }} · {{ artifact.contentType }}</small>
                    </a>
                  </div>
                  <v-select
                    :model-value="formForTodo(todo).verdict"
                    :items="todoReviewVerdictItems"
                    label="这一步的画面结论"
                    variant="outlined"
                    @update:model-value="setTodoReviewVerdict(todo, $event)"
                  />
                  <v-text-field
                    v-model="formForTodo(todo).reasonCode"
                    label="原因码"
                    hint="小写字母开头，只允许 a-z、0-9、点、下划线和短横线。"
                    persistent-hint
                    maxlength="160"
                    variant="outlined"
                  />
                  <v-select
                    v-model="formForTodo(todo).artifactRefs"
                    :items="todoEvidenceItems(todo.todoInstanceId)"
                    label="用于判断这一步的原始截图"
                    multiple
                    chips
                    variant="outlined"
                  />
                </article>
              </div>

              <template v-if="completionContract.value.policyStatus === 'supported'">
                <v-alert v-if="!completionContract.value.predicates.length" type="info" variant="tonal" class="mb-4">
                  该游戏没有额外数值谓词，但逐 Todo 画面复核仍为必填；普通前后图不能替代每一步的语义结论。
                </v-alert>
                <div class="predicate-form">
                  <div v-for="predicate in completionContract.value.predicates" :key="predicate.predicateId" class="predicate-inputs">
                    <strong>{{ predicate.label ?? predicate.predicateId }}<code>{{ predicate.predicateId }}</code></strong>
                    <p v-if="predicate.description" class="soft-note">{{ predicate.description }}</p>
                    <div class="constraint-list">
                      <code v-for="(constraint, index) in predicate.constraints" :key="`${constraint.metric}:${index}`">{{ constraintText(constraint) }}</code>
                    </div>
                    <template v-for="field in completionMetricFields(predicate)" :key="field.metric">
                      <v-select
                        v-if="field.valueType === 'boolean'"
                        :model-value="booleanMetricValue(predicate, field.metric)"
                        :items="[true, false]"
                        :label="`${field.metric} · boolean observation`"
                        variant="outlined"
                        @update:model-value="setBooleanMetricValue(predicate, field.metric, $event)"
                      />
                      <v-text-field
                        v-else-if="field.valueType === 'number'"
                        v-model.number="formForPredicate(predicate).metrics[field.metric]"
                        type="number"
                        :label="`${field.metric} · number observation`"
                        variant="outlined"
                      />
                      <v-text-field
                        v-else
                        v-model="formForPredicate(predicate).metrics[field.metric]"
                        maxlength="200"
                        :label="`${field.metric} · bounded string observation`"
                        hint="禁止 URL、路径、shell 片段或 JSON；该值只作为 typed metric 提交。"
                        persistent-hint
                        variant="outlined"
                      />
                    </template>
                    <v-select
                      v-model="formForPredicate(predicate).artifactRefs"
                      :items="artifactItemsForPredicate(predicate)"
                      label="该谓词允许的工作项 artifactId · kind"
                      multiple
                      chips
                      variant="outlined"
                    />
                    <p class="soft-note">允许 kind：{{ predicate.allowedArtifactKinds.join(' · ') }}</p>
                  </div>
                </div>
              </template>
              <v-alert :type="completionReviewBuild.errors.length ? 'warning' : 'info'" variant="tonal" class="mt-3">
                {{ completionReviewBuild.errors.length ? completionReviewBuild.errors.join('；') : `当前 ${selectedDecision} 可生成逐 Todo、合同限定的 typed CompletionReview。` }}
              </v-alert>
            </template>
          </section>

          <v-select
            v-model="capabilityRef"
            :items="capabilityItems"
            label="工作项允许的 capability"
            variant="outlined"
            clearable
          />
          <div v-if="inputFields.length" class="capability-fields">
            <template v-for="field in inputFields" :key="field.name">
              <v-select
                v-if="field.values || field.type === 'boolean'"
                v-model="capabilityInputs[field.name]"
                :items="field.values ?? [true, false]"
                :label="`${field.name} · ${field.type}${field.required ? ' · 必填' : ''}`"
                :hint="field.description"
                persistent-hint
                variant="outlined"
              />
              <v-textarea
                v-else-if="field.type === 'object'"
                v-model="capabilityInputs[field.name]"
                :label="`${field.name} · JSON 对象${field.required ? ' · 必填' : ''}`"
                :hint="field.description"
                persistent-hint
                rows="3"
                variant="outlined"
              />
              <v-text-field
                v-else
                v-model="capabilityInputs[field.name]"
                :label="`${field.name} · ${field.type}${field.required ? ' · 必填' : ''}`"
                :hint="field.type === 'array' ? '多个值用逗号或换行分隔' : field.description"
                persistent-hint
                variant="outlined"
              />
            </template>
          </div>
          <v-alert :type="selectedClosure === 'request-record' ? 'warning' : 'info'" variant="tonal" class="mb-4">
            {{ capabilityClosureExplanation }}
          </v-alert>
          <v-alert v-if="capabilityUnavailableReason" type="warning" variant="tonal" class="mb-4">
            {{ capabilityUnavailableReason }}
          </v-alert>
          <v-alert v-if="decisionUnavailableReason" type="warning" variant="tonal" class="mb-4">
            提交判断暂不可用：{{ decisionUnavailableReason }}
          </v-alert>

          <div class="action-row">
            <v-btn variant="tonal" :loading="busy" :disabled="!canClaim" @click="claim">{{ claimActionText }}</v-btn>
            <v-btn variant="tonal" :loading="busy" :disabled="!!requestMoreEvidenceUnavailableReason" @click="requestMoreEvidence">退回补证据</v-btn>
            <v-btn variant="tonal" color="warning" :loading="busy" :disabled="!!capabilityUnavailableReason" @click="requestCapability">{{ capabilityActionText }}</v-btn>
            <v-btn color="primary" :loading="busy" :disabled="!!decisionUnavailableReason" @click="decide">提交判断</v-btn>
          </div>
          <p class="soft-note mt-4">Claim：{{ activeClaim?.claimId ?? '无可用 Claim' }} · 领取期限：{{ formatTime(activeClaim?.expiresAt) }}。提交判断不会直接写 checkpoint，也不会直接操作窗口。</p>
          <JsonPanel v-if="lastCapabilityResult" title="最近一次能力结果（按其 state 解释，不等同完成）" :value="lastCapabilityResult" :open="true" />
          <JsonPanel title="工作项完整载荷" :value="selected" />
        </div>
      </v-card>

      <v-card v-if="selected" class="panel span-12">
        <div class="panel-title"><h2>当前工作项完成复核</h2><span class="soft-note">按 workItem / run 过滤</span></div>
        <div class="panel-body">
          <CompletionLedger
            :reviews="selectedCompletionReviews"
            :adjudications="selectedCompletionAdjudications"
            :blockers="selectedBlockers"
          />
        </div>
      </v-card>
    </div>
  </ResourceState>
</template>

<style scoped>
.work-list { padding: 0 12px 16px; }
.work-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; margin: 6px 0; padding: 13px; border: 1px solid transparent; border-radius: 12px; background: rgba(8,22,37,.45); color: inherit; text-align: left; cursor: pointer; }
.work-row:hover, .work-row.active { border-color: rgba(115,201,255,.22); background: rgba(55,102,142,.16); }
.work-row strong, .work-row small { display: block; }.work-row strong { font-size: .8rem; }.work-row small { margin-top: 4px; color: var(--muted); font-size: .68rem; }
.artifact-links { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.capability-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.todo-diagnostics { padding: 13px; border: 1px solid rgba(255,198,109,.18); border-radius: 12px; background: rgba(80,56,18,.1); }
.completion-form { padding: 13px; border: 1px solid rgba(103,221,178,.18); border-radius: 12px; background: rgba(16,74,58,.1); }
.scope-grid { display: flex; flex-wrap: wrap; gap: 7px 14px; color: var(--muted); font-size: .68rem; }.scope-grid code { color: #9fd9c5; overflow-wrap: anywhere; }
.todo-review-form { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }
.todo-review-card { display: grid; gap: 10px; padding: 12px; border: 1px solid rgba(103,221,178,.16); border-radius: 12px; background: rgba(7,30,27,.38); }
.todo-review-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }.todo-review-heading strong,.todo-review-heading small { display: block; }.todo-review-heading strong { color: #cceee2; font-size: .76rem; }.todo-review-heading small { margin-top: 3px; color: var(--muted); font-size: .62rem; overflow-wrap: anywhere; }
.todo-shot-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; }.todo-shot { min-width: 0; overflow: hidden; border: 1px solid rgba(142,190,230,.14); border-radius: 9px; color: inherit; text-decoration: none; background: rgba(4,16,25,.62); }.todo-shot span,.todo-shot small { display: block; padding: 5px 7px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.todo-shot span { color: #cce3f1; font-size: .62rem; }.todo-shot small { padding-bottom: 7px; color: var(--muted); font-size: .56rem; }
.predicate-form { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }.predicate-inputs { display: grid; gap: 8px; padding: 11px; border: 1px solid rgba(142,190,230,.1); border-radius: 10px; }.predicate-inputs strong { display: grid; gap: 3px; color: #cde3f4; font-size: .7rem; overflow-wrap: anywhere; }.predicate-inputs strong code { color: #7fa9ca; font-size: .64rem; }
.constraint-list { display: flex; flex-wrap: wrap; gap: 6px; }.constraint-list code { padding: 3px 7px; border-radius: 999px; background: rgba(70,126,169,.14); color: #9cc7e5; font-size: .62rem; }
.diagnostic-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.diagnostic-heading strong { font-size: .8rem; }.diagnostic-heading span { color: var(--muted); font-size: .7rem; }
.diagnostic-todo { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 10px 0; border-top: 1px solid rgba(142,190,230,.08); }
.diagnostic-todo strong,.diagnostic-todo small { display: block; }.diagnostic-todo strong { font-size: .78rem; }.diagnostic-todo small,.diagnostic-todo p { margin: 4px 0 0; color: var(--muted); font-size: .68rem; line-height: 1.5; }
.diagnostic-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.diagnosis-editor { display: grid; gap: 12px; margin-bottom: 12px; padding: 12px; border: 1px solid rgba(142,190,230,.12); border-radius: 12px; background: rgba(8,24,38,.34); }
.diagnosis-editor-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.diagnosis-editor-heading strong,.diagnosis-editor-heading small { display: block; }.diagnosis-editor-heading small { margin-top: 4px; color: var(--muted); font-size: .65rem; overflow-wrap: anywhere; }
.diagnosis-editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }.diagnosis-editor-grid.disabled { opacity: .55; }.diagnosis-editor-grid > :nth-child(n+5) { grid-column: span 2; }
@media (max-width: 760px) { .capability-fields,.todo-review-form,.predicate-form,.diagnosis-editor-grid { grid-template-columns: 1fr; }.diagnosis-editor-grid > :nth-child(n+5) { grid-column: auto; } }
</style>

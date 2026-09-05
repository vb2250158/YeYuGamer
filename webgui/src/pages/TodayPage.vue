<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import TodoChecklist from '../components/TodoChecklist.vue'
import { useManagerStore } from '../stores/manager'
import { formatTime } from '../utils/format'
import { executionIsEnabled, runtimeBindingReadinessForGames } from '../utils/managerResources'
import { acceptedCompletionEvidence, currentStepEvidence } from '../utils/todayEvidence'
import { useCurrentTodos } from '../composables/useTodos'
import { todoSummaryFromItems, todosByGame } from '../utils/todos'
import { buildTodayScope, todayScopePeriodLabel, withFrozenBatchScope } from '../utils/todayScope'
import { todayGameNextAction, todayGameSituationState, todayRuntimeIsIssue, todayRuntimeState } from '../utils/todayRuntime'
import { queueCleanupTarget, queueCleanupView } from '../utils/queueCleanup'
import { managerApi } from '../api/client'
import { recordOf, type AdapterInfo, type BatchRun, type ConfigDocument, type EvidenceArtifact, type GameIntegration, type GamePathConfig, type GameState, type LDPlayerGameBindingConfig, type RunAttempt, type TodoInstance, type TodoResetPreview } from '../api/contracts'
import { useResource } from '../composables/useResource'

type OKWWProfile = {
  whichToFarm: 'Tacet Suppression' | 'Forgery Challenge' | 'Simulation Challenge'
  tacetSuppressionNumber: number
  forgeryChallengeNumber: number
  materialSelection: 'Resonator EXP' | 'Weapon EXP' | 'Shell Credit'
  farmNightmareNestForDailyEcho: boolean
}

type EndfieldProfile = {
  staminaStage: string
  rewardTier: '保持当前' | '低阶' | '高阶'
  staminaRotationStartDate: string
  staminaRotation: string[]
  teamSlot: '不换队伍' | '1' | '2' | '3' | '4' | '5'
}

type NTEProfile = {
  anomalyTaskType: '经验与甲硬币' | '异能升级材料' | '弧盘突破材料' | '空幕'
  expRewardTarget: '角色经验' | '弧盘经验' | '甲硬币'
  materialIndex: number
  staminaTarget: number
  autoCycleSubTask: boolean
  coffeeMode: '不执行' | '领取/补货' | '完整自动化'
}

type CZNProfile = {
  staminaCategory: '成长' | '主战员' | '辅战员' | '潜能'
  staminaTarget: string
  battleEfficiency: number
  untilExhausted: true
}

const defaultOKWWProfile = (): OKWWProfile => ({
  whichToFarm: 'Tacet Suppression',
  tacetSuppressionNumber: 1,
  forgeryChallengeNumber: 1,
  materialSelection: 'Shell Credit',
  farmNightmareNestForDailyEcho: true,
})

const tacetSuppressionOptions = Array.from({ length: 19 }, (_, index) => ({
  title: `F2 列表第 ${index + 1} 个`,
  value: index + 1,
}))

const defaultEndfieldProfile = (): EndfieldProfile => ({
  staminaStage: '超距辉映管', rewardTier: '保持当前', staminaRotationStartDate: '2026-04-06', staminaRotation: ['超距辉映管'], teamSlot: '不换队伍',
})

const defaultNTEProfile = (): NTEProfile => ({
  anomalyTaskType: '经验与甲硬币', expRewardTarget: '甲硬币', materialIndex: 1, staminaTarget: 200, autoCycleSubTask: false, coffeeMode: '不执行',
})

const defaultCZNProfile = (): CZNProfile => ({
  staminaCategory: '成长', staminaTarget: '单元币', battleEfficiency: 4, untilExhausted: true,
})

const manager = useManagerStore()
const router = useRouter()
const {
  snapshot,
  activeBatch,
  executionInProgress,
} = storeToRefs(manager)
const busy = ref(false)
const expanded = ref(new Set<string>())
const configDocument = shallowRef<ConfigDocument>()
const gameIntegrations = shallowRef<GameIntegration[]>([])
const evidenceArtifacts = shallowRef<EvidenceArtifact[]>([])
const stepScreenshotDialog = ref(false)
const selectedStepTodo = shallowRef<TodoInstance>()
const selectedStepGameName = ref('')
const resetConfirmDialog = ref(false)
const pendingResetGame = shallowRef<{ gameId: string; displayName: string }>()
const gameIntegrationsError = ref<string>()
const dailyTodos = useCurrentTodos('daily')
const runtimeBindings = useResource<AdapterInfo>('/adapters')
const gamePathDrafts = ref<Record<string, GamePathConfig>>({})
const gameEnabledDrafts = ref<Record<string, boolean>>({})
const dailyTodoSelectionDrafts = ref<Record<string, string[]>>({})
const okWwProfileDraft = ref<OKWWProfile>()
const endfieldProfileDraft = ref<EndfieldProfile>()
const nteProfileDraft = ref<NTEProfile>()
const cznProfileDraft = ref<CZNProfile>()
const savingGameConfigId = ref<string>()
const resettingGameId = ref<string>()
const gameConfigErrors = ref<Record<string, string>>({})
const savingDailySelectionIds = ref(new Set<string>())
const failedDailySelectionIds = ref(new Set<string>())
const configurationFlushError = ref<string>()
const executionReadinessError = ref<string>()
let dailySelectionSaveQueue: Promise<void> = Promise.resolve()
const dailySelectionRevisions = new Map<string, number>()
const manualConfigRevisions = new Map<string, number>()
let configurationMutationRevision = 0

const allGames = computed(() => snapshot.value.games)
const queuedDailyGames = computed(() => allGames.value.filter((game) => (
  dailyGameEnabled(game.gameId)
  && selectedDailyTodoDefinitionIds(game.gameId).length > 0
)))
const dailySelectionSaving = computed(() => savingDailySelectionIds.value.size > 0)
const executionEnabled = computed(() => executionIsEnabled(snapshot.value))
const queuedRuntimeBindingReadiness = computed(() => runtimeBindingReadinessForGames(
  queuedDailyGames.value.map((game) => game.gameId),
  runtimeBindings.items.value,
))
const unavailableQueuedGames = computed(() => queuedRuntimeBindingReadiness.value.unavailableGames.map((item) => ({
  ...item,
  displayName: allGames.value.find((game) => game.gameId === item.gameId)?.displayName ?? item.gameId,
})))
const canExecuteQueuedGames = computed(() => (
  executionEnabled.value
  && !runtimeBindings.loading.value
  && !runtimeBindings.error.value
  && queuedRuntimeBindingReadiness.value.anyReady
))
const configurationLocked = computed(() => busy.value || executionInProgress.value)
const todoGroups = computed(() => todosByGame(dailyTodos.items.value))
const retainedExecutionBatch = computed(() => {
  if (activeBatch.value || !executionInProgress.value) return undefined
  return (snapshot.value.recentBatches ?? []).find((batch) => (
    batch.state === 'running'
    || batch.result?.batchActionAvailability?.cancel === true
  ))
})
const displayedExecutionBatch = computed(() => activeBatch.value ?? retainedExecutionBatch.value)
const cleanupTarget = computed(() => queueCleanupTarget(displayedExecutionBatch.value))
const cleanupAttempt = shallowRef<RunAttempt>()
const cleanupView = computed(() => queueCleanupView(
  cleanupTarget.value,
  cleanupAttempt.value,
  Object.fromEntries(allGames.value.map((game) => [game.gameId, game.displayName])),
))
watch(() => [cleanupTarget.value, snapshot.value.stateVersion] as const, ([target], _previous, onCleanup) => {
  let stale = false
  const timer = setTimeout(async () => {
    if (!target) { cleanupAttempt.value = undefined; return }
    try {
      const attempt = await managerApi.get<RunAttempt>(`/run-attempts/${encodeURIComponent(target.attemptId)}`)
      if (!stale) cleanupAttempt.value = attempt
    } catch {
      if (!stale) cleanupAttempt.value = undefined
    }
  }, 200)
  onCleanup(() => { stale = true; clearTimeout(timer) })
}, { immediate: true })
const frozenBatchDetail = shallowRef<BatchRun>()
let loadingBatchScopeId: string | undefined
async function loadCurrentBatchScope(): Promise<void> {
  const batchId = displayedExecutionBatch.value?.batchId
  if (!batchId) {
    frozenBatchDetail.value = undefined
    return
  }
  if (frozenBatchDetail.value?.batchId === batchId || loadingBatchScopeId === batchId) return
  loadingBatchScopeId = batchId
  try {
    const detail = await managerApi.get<BatchRun>(`/batches/${encodeURIComponent(batchId)}`)
    if (displayedExecutionBatch.value?.batchId === batchId && detail.batchId === batchId) frozenBatchDetail.value = detail
  } catch {
    // Keep the locked games visible and retry on the next Manager snapshot.
  } finally {
    if (loadingBatchScopeId === batchId) loadingBatchScopeId = undefined
  }
}
watch(() => [displayedExecutionBatch.value?.batchId, snapshot.value.stateVersion], () => void loadCurrentBatchScope(), { immediate: true })
const selectedTodoDefinitionIdsByGame = computed<Record<string, string[]>>(() => Object.fromEntries(
  allGames.value.map((game) => [game.gameId, selectedDailyTodoDefinitionIds(game.gameId)]),
))
const todayScope = computed(() => buildTodayScope({
  activeBatch: withFrozenBatchScope(displayedExecutionBatch.value, frozenBatchDetail.value),
  games: allGames.value,
  todos: dailyTodos.items.value,
  selectedTodoDefinitionIds: selectedTodoDefinitionIdsByGame.value,
}))
const gameDayLabel = computed(() => {
  const label = todayScopePeriodLabel(todayScope.value.todos, todayScope.value.periodKeys)
  return label === '暂无项目' && !todayScope.value.known ? '等待同步' : label
})
const scopedGameIdSet = computed(() => new Set(todayScope.value.gameIds))
const scopedGames = computed(() => allGames.value.filter((game) => scopedGameIdSet.value.has(game.gameId)))
const scopedTodoGroups = computed(() => todosByGame(todayScope.value.todos))
const todoRequired = computed(() => todayScope.value.requiredTotal)
const todoCompleted = computed(() => todayScope.value.requiredCompleted)
const acceptedCount = computed(() => scopedGames.value.filter((game) => {
  const required = (scopedTodoGroups.value.get(game.gameId) ?? []).filter((todo) => todo.required)
  return required.length > 0 && required.every((todo) => todo.status === 'completed') && game.acceptanceState === 'accepted_done'
}).length)
const evidencePendingGames = computed(() => scopedGames.value.filter((game) => {
  if (['human_required', 'human_takeover'].includes(gameRuntimeState(game))) return false
  if (game.acceptanceState !== 'evidence_pending') return false
  if (displayedExecutionBatch.value?.batchId && game.batchId === displayedExecutionBatch.value.batchId) return true
  const currentRunIds = new Set(todayScope.value.todos
    .filter((todo) => todo.gameId === game.gameId && typeof todo.runId === 'string')
    .map((todo) => todo.runId))
  return typeof game.runId === 'string' && currentRunIds.has(game.runId)
}))
const reviewCount = computed(() => new Set([
  ...todayScope.value.todos.filter((todo) => todo.status === 'review_required').map((todo) => todo.gameId),
  ...evidencePendingGames.value.map((game) => game.gameId),
]).size)
const riskCount = computed(() => todayScope.value.blocked)
const todayScopeUsable = computed(() => todayScope.value.known && !dailyTodos.loading.value && !dailyTodos.error.value)
const todayScopeStatus = computed(() => {
  if (dailyTodos.loading.value) return '正在读取今天的每日项目，进度暂不确定。'
  if (dailyTodos.error.value) return '每日项目读取失败，当前进度暂不确定。'
  if (!todayScope.value.known) return '本次每日范围尚未同步完整，进度暂待确认。'
  return todayScope.value.source === 'current_batch'
    ? '按本次运行已经锁定的游戏和每日项目统计。'
    : '按今天已启用并勾选的每日项目统计。'
})
const manualDirtyGameIds = computed(() => {
  const ids = new Set(Object.keys(gamePathDrafts.value))
  if (okWwProfileDraft.value) ids.add('WW')
  if (endfieldProfileDraft.value) ids.add('Endfield')
  if (nteProfileDraft.value) ids.add('NTE')
  if (cznProfileDraft.value) ids.add('CZN')
  return [...ids]
})
const selectionDirtyGameIds = computed(() => [...new Set([
  ...Object.keys(gameEnabledDrafts.value),
  ...Object.keys(dailyTodoSelectionDrafts.value),
  ...failedDailySelectionIds.value,
])])
const hasUnsavedConfiguration = computed(() => dailySelectionSaving.value || manualDirtyGameIds.value.length > 0 || selectionDirtyGameIds.value.length > 0)
const canReviewEvidence = computed(() => evidencePendingGames.value.length > 0)
const humanTakeoverTargets = computed(() => {
  const actions = displayedExecutionBatch.value?.result?.batchActionAvailability
  return actions?.humanTakeover === true ? actions.humanTakeoverTargets ?? [] : []
})
const runResumeTargets = computed(() => {
  const actions = displayedExecutionBatch.value?.result?.batchActionAvailability
  if (actions?.runResume !== true || !Array.isArray(actions.runResumeTargets)) return []
  return actions.runResumeTargets.filter((target) => (
    typeof target?.runId === 'string' && target.runId.length > 0
    && typeof target?.gameId === 'string' && target.gameId.length > 0
  ))
})
const canReviewBatch = computed(() => displayedExecutionBatch.value?.result?.batchActionAvailability?.review === true)
const mustStartFreshAfterCancel = computed(() => (
  displayedExecutionBatch.value?.result?.batchActionAvailability?.nextAction
  === 'cancel_old_batch_then_start_fresh'
))
const primaryStopReason = computed(() => {
  if (humanTakeoverTargets.value.length) return '本次每日停在人工接管；请保留游戏现场，处理后显式释放接管，再恢复同一次运行。'
  if (canReviewEvidence.value) return `${evidencePendingGames.value.map((game) => game.displayName).join('、')} 已完成运行，但证据尚未复核。`
  if (runResumeTargets.value.length) return `${runResumeTargets.value.map((target) => allGames.value.find((game) => game.gameId === target.gameId)?.displayName ?? target.gameId).join('、')} 的上一次运行已停止在可恢复状态。`
  if (mustStartFreshAfterCancel.value) return '上一轮有动作结果不明，当前没有可靠记录可证明是否已执行。请安全结束旧运行，再重新开始；页面不会盲目续跑。'
  if (canReviewBatch.value) return '本次运行需要确认后才能继续。'
  if (executionInProgress.value) return '正在执行今天的每日；为避免重复操作，配置暂时锁定。'
  if (configurationFlushError.value) return configurationFlushError.value
  if (executionReadinessError.value) return executionReadinessError.value
  if (!queuedDailyGames.value.length) return '还没有选择要执行的游戏和每日项。'
  if (!executionEnabled.value) return '执行功能暂不可用，请稍后再试。'
  if (runtimeBindings.loading.value) return '正在读取 Manager 的逐游戏 runtimeBinding readiness。'
  if (runtimeBindings.error.value) return '逐游戏 runtimeBinding readiness 读取失败，当前不会启动批次。'
  if (!queuedRuntimeBindingReadiness.value.anyReady) return '所选游戏当前都没有可执行的 runtimeBinding。'
  if (!todayScope.value.known) return todayScopeStatus.value
  return hasUnsavedConfiguration.value ? '参数有未保存修改；开始时会先保存并确认新版本。' : '范围已确认，可以开始今天的每日。'
})
const primaryNextAction = computed(() => {
  if (humanTakeoverTargets.value.length) return '打开人工接管详情'
  if (canReviewEvidence.value) return '打开完成复核，逐项确认本次截图'
  if (runResumeTargets.value.length) return '恢复上次安全中断的游戏'
  if (canReviewBatch.value) return '打开运行复核，确认后继续'
  if (displayedExecutionBatch.value?.result?.batchActionAvailability?.cancel === true) {
    return mustStartFreshAfterCancel.value ? '安全结束旧运行，然后重新开始' : '安全停止当前执行'
  }
  if (executionInProgress.value) return '等待当前步骤完成'
  if (!queuedDailyGames.value.length) return '选择游戏和每日项目'
  if (!executionEnabled.value) return '等待执行功能恢复'
  if (!queuedRuntimeBindingReadiness.value.anyReady) return '处理所选游戏的 runtimeBinding 阻塞'
  return hasUnsavedConfiguration.value ? '保存修改并开始每日' : '开始今天的每日'
})
const displayedCurrentGameId = computed(() => (
  displayedExecutionBatch.value?.currentGameId
  ?? displayedExecutionBatch.value?.result?.currentGameId
))
const activeGame = computed(() => {
  const currentGameId = displayedCurrentGameId.value
  return allGames.value.find((game) => game.gameId === currentGameId)
    ?? allGames.value.find((game) => game.runtimeState === 'running')
})
const activeTodo = computed(() => activeGame.value
  ? gameTodos(activeGame.value.gameId).find((item) => item.status === 'in_progress')
  : undefined)
const displayedRunState = computed(() => {
  if (humanTakeoverTargets.value.length) return 'human_required'
  if (canReviewEvidence.value || canReviewBatch.value || runResumeTargets.value.length) return 'review_required'
  if (executionInProgress.value) return 'executing'
  if (displayedExecutionBatch.value?.state === 'completed') return 'completed'
  if (displayedExecutionBatch.value?.state === 'cancelled') return 'cancelled'
  if (displayedExecutionBatch.value && displayedExecutionBatch.value.state !== 'planned') return 'blocked'
  return 'not_started'
})
const acceptanceDisplayStates = new Set([
  'unknown', 'not_started', 'in_progress', 'evidence_pending', 'accepted_done', 'rejected_done', 'not_applicable',
])

function knownDisplayState(state: string | undefined, allowed: Set<string>): string {
  return state && allowed.has(state) ? state : 'unknown'
}

function gameRuntimeState(game: GameState): string {
  return todayRuntimeState(game, gameTodos(game.gameId))
}

function gameNextActionLabel(game: GameState): string {
  return todayGameNextAction(game, gameTodos(game.gameId))
}
const gamePaths = computed<Record<string, GamePathConfig>>(() => {
  const raw = recordOf(configDocument.value?.config)
  const configured = recordOf(raw.gamePaths ?? raw.game_paths)
  return Object.fromEntries(Object.entries(configured).map(([gameId, value]) => {
    const path = recordOf(value)
    const rawEmulator = recordOf(path.emulator)
    const provider = rawEmulator.provider
    const emulator = provider === 'ldplayer' ? {
      provider,
      consolePath: String(rawEmulator.consolePath ?? rawEmulator.console_path ?? ''),
      adbPath: String(rawEmulator.adbPath ?? rawEmulator.adb_path ?? ''),
      instanceIndex: Number(rawEmulator.instanceIndex ?? rawEmulator.instance_index ?? 0),
      instanceName: typeof (rawEmulator.instanceName ?? rawEmulator.instance_name) === 'string' ? String(rawEmulator.instanceName ?? rawEmulator.instance_name) : null,
      adbSerial: String(rawEmulator.adbSerial ?? rawEmulator.adb_serial ?? ''),
    } satisfies LDPlayerGameBindingConfig : null
    return [gameId, {
      gamePath: typeof path.gamePath === 'string' ? path.gamePath : typeof path.game_path === 'string' ? path.game_path : null,
      toolPath: typeof path.toolPath === 'string' ? path.toolPath : typeof path.tool_path === 'string' ? path.tool_path : null,
      emulator,
    }]
  }))
})
const configuredDailyTodoSelection = computed<Record<string, string[]>>(() => {
  const raw = recordOf(configDocument.value?.config)
  const configured = recordOf(raw.dailyTodoSelection ?? raw.daily_todo_selection)
  return Object.fromEntries(Object.entries(configured).map(([gameId, definitionIds]) => [
    gameId,
    Array.isArray(definitionIds) ? definitionIds.filter((item): item is string => typeof item === 'string') : [],
  ]))
})
const dailyToolProfiles = computed(() => {
  const raw = recordOf(configDocument.value?.config)
  return recordOf(raw.dailyToolProfiles ?? raw.daily_tool_profiles)
})

async function loadConfiguration(): Promise<void> {
  try {
    configDocument.value = await managerApi.get<ConfigDocument>('/config')
  } catch (error) {
    manager.rememberError('读取游戏路径配置', error)
  }
}

async function loadGameIntegrations(): Promise<void> {
  try {
    const response = await managerApi.get<{ items?: GameIntegration[] }>('/integrations')
    gameIntegrations.value = Array.isArray(response.items) ? response.items : []
    gameIntegrationsError.value = undefined
  } catch (error) {
    gameIntegrationsError.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadEvidenceArtifacts(): Promise<void> {
  try {
    const response = await managerApi.get<{ items?: EvidenceArtifact[] }>('/artifacts?limit=500')
    evidenceArtifacts.value = Array.isArray(response.items) ? response.items : []
  } catch (error) {
    manager.rememberError('读取今日完成截图', error)
  }
}

let evidenceReloadTimer: ReturnType<typeof setTimeout> | undefined
watch(() => snapshot.value.stateVersion, () => {
  if (evidenceReloadTimer) clearTimeout(evidenceReloadTimer)
  evidenceReloadTimer = setTimeout(() => void loadEvidenceArtifacts(), 750)
})

onMounted(() => void Promise.all([loadConfiguration(), loadGameIntegrations(), loadEvidenceArtifacts()]))
onUnmounted(() => { if (evidenceReloadTimer) clearTimeout(evidenceReloadTimer) })

function integrationForGame(gameId: string): GameIntegration | undefined {
  return gameIntegrations.value.find((item) => item.gameId === gameId)
}

function integrationOperation(gameId: string, operation: string) {
  return integrationForGame(gameId)?.operations.find((item) => item.operation === operation)
}

function integrationSummary(gameId: string): string {
  const integration = integrationForGame(gameId)
  if (!integration) return '正在读取自动执行方式'
  if (integration.mappingStatus === 'registered') return '已可自动执行'
  if (integration.mappingStatus === 'incomplete') return '部分步骤尚未接好，暂时不能开始'
  return '暂时不能自动执行'
}

function allGameTodos(gameId: string) {
  return todoGroups.value.get(gameId) ?? []
}

function gameTodos(gameId: string) {
  return scopedTodoGroups.value.get(gameId) ?? []
}

function requiredGameTodos(gameId: string) {
  return gameTodos(gameId).filter((item) => item.required)
}

function optionalGameTodos(gameId: string) {
  return gameTodos(gameId).filter((item) => !item.required)
}

function gameTodoSummary(gameId: string) {
  return todoSummaryFromItems(gameTodos(gameId), 'daily')
}

function todayEvidenceForGame(gameId: string): EvidenceArtifact[] {
  const game = allGames.value.find((item) => item.gameId === gameId)
  const completion = recordOf(recordOf(game?.policy).currentCompletion)
  const acceptedArtifactIds = Array.isArray(completion.screenshotEvidenceIds)
    ? completion.screenshotEvidenceIds.filter((item): item is string => typeof item === 'string')
    : []
  return acceptedCompletionEvidence(evidenceArtifacts.value, {
    acceptanceState: game?.acceptanceState ?? 'not_started',
    gameId,
    runId: game?.runId,
    gameDayKeys: dailyConfigTodos(gameId).map((item) => item.periodKey).filter(Boolean),
    acceptedArtifactIds,
  })
}

function artifactContentUrl(artifactId: string): string {
  return `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`
}

function evidenceKindLabel(artifact: EvidenceArtifact): string {
  if (artifact.kind === 'game-ui-daily-reward-watermarked') return '领取完成 · 北京时间水印'
  return '领取完成 · 原始截图'
}

function stepEvidenceForTodo(todo: TodoInstance): EvidenceArtifact[] {
  return currentStepEvidence(evidenceArtifacts.value, todo)
}

const selectedStepEvidence = computed(() => (
  selectedStepTodo.value ? stepEvidenceForTodo(selectedStepTodo.value) : []
))
const selectedStepEvidenceDuplicated = computed(() => {
  const hashes = selectedStepEvidence.value
    .map((artifact) => artifact.hash)
    .filter((value): value is string => typeof value === 'string' && value.length > 0)
  return selectedStepEvidence.value.length >= 2
    && hashes.length === selectedStepEvidence.value.length
    && new Set(hashes).size === 1
})

function openStepScreenshots(gameName: string, todo: TodoInstance): void {
  selectedStepGameName.value = gameName
  selectedStepTodo.value = todo
  stepScreenshotDialog.value = true
}

function stepEvidenceLabel(artifact: EvidenceArtifact): string {
  return artifact.kind === 'game-ui-step-before-raw'
    ? '步骤完成前 · 原始截图'
    : '步骤完成后 · 北京时间水印'
}

function toggleTodo(gameId: string): void {
  const next = new Set(expanded.value)
  if (next.has(gameId)) next.delete(gameId)
  else next.add(gameId)
  expanded.value = next
}

function dailyConfigTodos(gameId: string) {
  return allGameTodos(gameId).filter((item) => item.cadence === 'daily')
}

function hasDraft<T>(drafts: Record<string, T>, gameId: string): boolean {
  return Object.prototype.hasOwnProperty.call(drafts, gameId)
}

const emulatorGameIds = new Set(['FGO', 'BD2', 'CZN'])

function touchManualConfig(gameId: string): void {
  manualConfigRevisions.set(gameId, (manualConfigRevisions.get(gameId) ?? 0) + 1)
  configurationMutationRevision += 1
  configurationFlushError.value = undefined
}

function pathDraft(gameId: string): GamePathConfig {
  return gamePathDrafts.value[gameId] ?? gamePaths.value[gameId] ?? { gamePath: null, toolPath: null }
}

function updatePathDraft(gameId: string, key: 'gamePath' | 'toolPath', value: string): void {
  if (configurationLocked.value) return
  gamePathDrafts.value = { ...gamePathDrafts.value, [gameId]: { ...pathDraft(gameId), [key]: value } }
  touchManualConfig(gameId)
}

function emulatorDraft(gameId: string): LDPlayerGameBindingConfig {
  return pathDraft(gameId).emulator ?? {
    provider: 'ldplayer',
    consolePath: '',
    adbPath: '',
    instanceIndex: 0,
    instanceName: null,
    adbSerial: 'emulator-5554',
  }
}

function updateEmulatorDraft<K extends keyof LDPlayerGameBindingConfig>(gameId: string, key: K, value: LDPlayerGameBindingConfig[K]): void {
  if (configurationLocked.value) return
  gamePathDrafts.value = {
    ...gamePathDrafts.value,
    [gameId]: {
      ...pathDraft(gameId),
      gamePath: null,
      emulator: { ...emulatorDraft(gameId), [key]: value },
    },
  }
  touchManualConfig(gameId)
}

function dailyGameEnabled(gameId: string): boolean {
  const game = allGames.value.find((item) => item.gameId === gameId)
  return hasDraft(gameEnabledDrafts.value, gameId) ? gameEnabledDrafts.value[gameId] : game?.enabled !== false
}

function updateDailyGameEnabled(gameId: string, value: boolean): void {
  if (configurationLocked.value) return
  configurationFlushError.value = undefined
  const failures = new Set(failedDailySelectionIds.value)
  failures.delete(gameId)
  failedDailySelectionIds.value = failures
  gameEnabledDrafts.value = { ...gameEnabledDrafts.value, [gameId]: value }
  if (value && selectedDailyTodoDefinitionIds(gameId).length === 0) {
    const registeredOperations = new Set(
      integrationForGame(gameId)?.operations.map((item) => item.operation) ?? [],
    )
    const safeDefaults = dailyConfigTodos(gameId)
      .filter((item) => (
        item.required
        && item.risk === 'routine_action'
        && typeof item.adapterCapabilityRef === 'string'
        && registeredOperations.has(item.operation)
      ))
      .map((item) => item.todoDefinitionId)
    if (safeDefaults.length) {
      dailyTodoSelectionDrafts.value = {
        ...dailyTodoSelectionDrafts.value,
        [gameId]: safeDefaults,
      }
    }
  }
  dailySelectionRevisions.set(gameId, (dailySelectionRevisions.get(gameId) ?? 0) + 1)
  configurationMutationRevision += 1
  void saveDailySelection(gameId)
}

function selectedDailyTodoDefinitionIds(gameId: string): string[] {
  if (hasDraft(dailyTodoSelectionDrafts.value, gameId)) return dailyTodoSelectionDrafts.value[gameId]
  if (hasDraft(configuredDailyTodoSelection.value, gameId)) return configuredDailyTodoSelection.value[gameId]
  return []
}

function dailyTodoSelected(gameId: string, todoDefinitionId: string): boolean {
  return selectedDailyTodoDefinitionIds(gameId).includes(todoDefinitionId)
}

function updateDailyTodoSelection(gameId: string, todoDefinitionId: string, selected: boolean): void {
  if (configurationLocked.value) return
  configurationFlushError.value = undefined
  const failures = new Set(failedDailySelectionIds.value)
  failures.delete(gameId)
  failedDailySelectionIds.value = failures
  const next = new Set(selectedDailyTodoDefinitionIds(gameId))
  if (selected) next.add(todoDefinitionId)
  else next.delete(todoDefinitionId)
  dailyTodoSelectionDrafts.value = { ...dailyTodoSelectionDrafts.value, [gameId]: [...next] }
  dailySelectionRevisions.set(gameId, (dailySelectionRevisions.get(gameId) ?? 0) + 1)
  configurationMutationRevision += 1
  void saveDailySelection(gameId)
}

function todayTodoState(todo: { status: string }): string {
  return todo.status === 'completed' ? '本步骤已执行' : '本步骤未完成'
}

function configuredOKWWProfile(): OKWWProfile {
  const raw = recordOf(dailyToolProfiles.value.okWw ?? dailyToolProfiles.value.ok_ww)
  const defaults = defaultOKWWProfile()
  const whichToFarm = raw.whichToFarm ?? raw.which_to_farm
  const materialSelection = raw.materialSelection ?? raw.material_selection
  const tacetSuppressionNumber = raw.tacetSuppressionNumber ?? raw.tacet_suppression_number
  const forgeryChallengeNumber = raw.forgeryChallengeNumber ?? raw.forgery_challenge_number
  return {
    whichToFarm: whichToFarm === 'Forgery Challenge' || whichToFarm === 'Simulation Challenge' ? whichToFarm : defaults.whichToFarm,
    tacetSuppressionNumber: typeof tacetSuppressionNumber === 'number' ? tacetSuppressionNumber : defaults.tacetSuppressionNumber,
    forgeryChallengeNumber: typeof forgeryChallengeNumber === 'number' ? forgeryChallengeNumber : defaults.forgeryChallengeNumber,
    materialSelection: materialSelection === 'Resonator EXP' || materialSelection === 'Weapon EXP' ? materialSelection : defaults.materialSelection,
    farmNightmareNestForDailyEcho: typeof (raw.farmNightmareNestForDailyEcho ?? raw.farm_nightmare_nest_for_daily_echo) === 'boolean'
      ? Boolean(raw.farmNightmareNestForDailyEcho ?? raw.farm_nightmare_nest_for_daily_echo)
      : defaults.farmNightmareNestForDailyEcho,
  }
}

function okWWProfile(): OKWWProfile {
  return okWwProfileDraft.value ?? configuredOKWWProfile()
}

function updateOKWWProfile<K extends keyof OKWWProfile>(key: K, value: OKWWProfile[K]): void {
  if (configurationLocked.value) return
  okWwProfileDraft.value = { ...okWWProfile(), [key]: value }
  touchManualConfig('WW')
}

function updateOKWWFarmTarget(value: unknown): void {
  if (value === 'Tacet Suppression' || value === 'Forgery Challenge' || value === 'Simulation Challenge') {
    updateOKWWProfile('whichToFarm', value)
  }
}

function updateOKWWMaterial(value: unknown): void {
  if (value === 'Resonator EXP' || value === 'Weapon EXP' || value === 'Shell Credit') {
    updateOKWWProfile('materialSelection', value)
  }
}

function updateOKWWIndex(key: 'tacetSuppressionNumber' | 'forgeryChallengeNumber', value: unknown): void {
  const index = Number(value)
  if (Number.isInteger(index) && index >= 1) updateOKWWProfile(key, index)
}

function configuredEndfieldProfile(): EndfieldProfile {
  const raw = recordOf(dailyToolProfiles.value.endfield)
  const defaults = defaultEndfieldProfile()
  const rotation = raw.staminaRotation ?? raw.stamina_rotation
  const rewardTier = raw.rewardTier ?? raw.reward_tier
  const teamSlot = raw.teamSlot ?? raw.team_slot
  return {
    staminaStage: typeof (raw.staminaStage ?? raw.stamina_stage) === 'string' ? String(raw.staminaStage ?? raw.stamina_stage) : defaults.staminaStage,
    rewardTier: rewardTier === '低阶' || rewardTier === '高阶' ? rewardTier : defaults.rewardTier,
    staminaRotationStartDate: typeof (raw.staminaRotationStartDate ?? raw.stamina_rotation_start_date) === 'string' ? String(raw.staminaRotationStartDate ?? raw.stamina_rotation_start_date) : defaults.staminaRotationStartDate,
    staminaRotation: Array.isArray(rotation) && rotation.every((item) => typeof item === 'string') && rotation.length ? rotation as string[] : defaults.staminaRotation,
    teamSlot: ['1', '2', '3', '4', '5'].includes(String(teamSlot)) ? String(teamSlot) as EndfieldProfile['teamSlot'] : defaults.teamSlot,
  }
}

function endfieldProfile(): EndfieldProfile { return endfieldProfileDraft.value ?? configuredEndfieldProfile() }
function updateEndfieldProfile<K extends keyof EndfieldProfile>(key: K, value: EndfieldProfile[K]): void {
  if (configurationLocked.value) return
  endfieldProfileDraft.value = { ...endfieldProfile(), [key]: value }
  touchManualConfig('Endfield')
}

function configuredNTEProfile(): NTEProfile {
  const raw = recordOf(dailyToolProfiles.value.nte)
  const defaults = defaultNTEProfile()
  const anomalyTaskType = raw.anomalyTaskType ?? raw.anomaly_task_type
  const expRewardTarget = raw.expRewardTarget ?? raw.exp_reward_target
  const coffeeMode = raw.coffeeMode ?? raw.coffee_mode
  const materialIndex = raw.materialIndex ?? raw.material_index
  const staminaTarget = raw.staminaTarget ?? raw.stamina_target
  return {
    anomalyTaskType: anomalyTaskType === '异能升级材料' || anomalyTaskType === '弧盘突破材料' || anomalyTaskType === '空幕' ? anomalyTaskType : defaults.anomalyTaskType,
    expRewardTarget: expRewardTarget === '角色经验' || expRewardTarget === '弧盘经验' ? expRewardTarget : defaults.expRewardTarget,
    materialIndex: typeof materialIndex === 'number' ? materialIndex : defaults.materialIndex,
    staminaTarget: typeof staminaTarget === 'number' ? staminaTarget : defaults.staminaTarget,
    autoCycleSubTask: typeof (raw.autoCycleSubTask ?? raw.auto_cycle_sub_task) === 'boolean' ? Boolean(raw.autoCycleSubTask ?? raw.auto_cycle_sub_task) : defaults.autoCycleSubTask,
    coffeeMode: coffeeMode === '领取/补货' || coffeeMode === '完整自动化' ? coffeeMode : defaults.coffeeMode,
  }
}

function nteProfile(): NTEProfile { return nteProfileDraft.value ?? configuredNTEProfile() }
function updateNTEProfile<K extends keyof NTEProfile>(key: K, value: NTEProfile[K]): void {
  if (configurationLocked.value) return
  nteProfileDraft.value = { ...nteProfile(), [key]: value }
  touchManualConfig('NTE')
}

const cznTargets: Record<CZNProfile['staminaCategory'], string[]> = {
  成长: ['单元币', '主战员升级材料', '辅战员升级材料'],
  主战员: ['前锋', '守卫', '游侠', '猎人', '奥义师', '操控师'],
  辅战员: ['前锋', '守卫', '游侠', '猎人', '奥义师', '操控师'],
  潜能: ['热情', '秩序', '本能', '虚无', '正义'],
}

function configuredCZNProfile(): CZNProfile {
  const raw = recordOf(dailyToolProfiles.value.czn)
  const defaults = defaultCZNProfile()
  const category = raw.staminaCategory ?? raw.stamina_category
  const safeCategory: CZNProfile['staminaCategory'] = category === '主战员' || category === '辅战员' || category === '潜能' ? category : '成长'
  const target = String(raw.staminaTarget ?? raw.stamina_target ?? '')
  const efficiency = Number(raw.battleEfficiency ?? raw.battle_efficiency)
  return {
    staminaCategory: safeCategory,
    staminaTarget: cznTargets[safeCategory].includes(target) ? target : cznTargets[safeCategory][0],
    battleEfficiency: Number.isInteger(efficiency) && efficiency >= 1 && efficiency <= 5 ? efficiency : defaults.battleEfficiency,
    untilExhausted: true,
  }
}

function cznProfile(): CZNProfile { return cznProfileDraft.value ?? configuredCZNProfile() }
function updateCZNCategory(value: unknown): void {
  if (configurationLocked.value) return
  const category: CZNProfile['staminaCategory'] = value === '主战员' || value === '辅战员' || value === '潜能' ? value : '成长'
  cznProfileDraft.value = { ...cznProfile(), staminaCategory: category, staminaTarget: cznTargets[category][0] }
  touchManualConfig('CZN')
}
function updateCZNProfile<K extends keyof CZNProfile>(key: K, value: CZNProfile[K]): void {
  if (configurationLocked.value) return
  cznProfileDraft.value = { ...cznProfile(), [key]: value }
  touchManualConfig('CZN')
}

function dailyToolProfilePatch(gameId: string): Record<string, unknown> | undefined {
  if (gameId === 'WW') return { okWw: okWWProfile() }
  if (gameId === 'Endfield') return { endfield: endfieldProfile() }
  if (gameId === 'NTE') return { nte: nteProfile() }
  if (gameId === 'CZN') return { czn: cznProfile() }
  return undefined
}

function markDailySelectionSaving(gameId: string, saving: boolean): void {
  const next = new Set(savingDailySelectionIds.value)
  if (saving) next.add(gameId)
  else next.delete(gameId)
  savingDailySelectionIds.value = next
}

function markDailySelectionFailure(gameId: string, failed: boolean): void {
  const next = new Set(failedDailySelectionIds.value)
  if (failed) next.add(gameId)
  else next.delete(gameId)
  failedDailySelectionIds.value = next
}

async function confirmSavedStateVersion(beforeStateVersion: number, acceptedStateVersion?: number): Promise<boolean> {
  await Promise.all([manager.refresh({ quiet: true }), loadConfiguration()])
  const target = acceptedStateVersion ?? beforeStateVersion + 1
  return target > beforeStateVersion
    && snapshot.value.stateVersion >= target
    && Number(configDocument.value?.stateVersion ?? target) >= target
}

function saveDailySelection(gameId: string): Promise<void> {
  const scheduled = dailySelectionSaveQueue.catch(() => undefined).then(async () => {
    markDailySelectionSaving(gameId, true)
    const revision = dailySelectionRevisions.get(gameId) ?? 0
    try {
      // Config mutations all share one Manager state version. Serialize writes
      // across cards so quick toggles cannot leave an optimistic draft ahead
      // of the Manager state used by batch validation.
      await manager.refresh({ quiet: true })
      const beforeStateVersion = snapshot.value.stateVersion
      const receipt = await manager.submitCommand('保存每日勾选', 'PATCH', '/config', {
        enabled: { [gameId]: dailyGameEnabled(gameId) },
        dailyTodoSelection: { [gameId]: selectedDailyTodoDefinitionIds(gameId) },
      })
      if (!receipt) {
        markDailySelectionFailure(gameId, true)
        gameConfigErrors.value = { ...gameConfigErrors.value, [gameId]: manager.errors[0]?.detail ?? '每日选择没有保存成功。' }
        return
      }
      // The Config receipt advances Manager state, but the event stream can
      // deliver that snapshot after this draft is cleared. Refresh both truth
      // sources before removing the optimistic value so the switch never
      // flickers back to the previous game scope.
      if (!await confirmSavedStateVersion(beforeStateVersion, receipt.acceptedStateVersion)) {
        markDailySelectionFailure(gameId, true)
        gameConfigErrors.value = { ...gameConfigErrors.value, [gameId]: '保存结果尚未确认，未允许开始执行。' }
        return
      }
      markDailySelectionFailure(gameId, false)
      if ((dailySelectionRevisions.get(gameId) ?? 0) === revision) {
        gameEnabledDrafts.value = Object.fromEntries(Object.entries(gameEnabledDrafts.value).filter(([id]) => id !== gameId))
        dailyTodoSelectionDrafts.value = Object.fromEntries(Object.entries(dailyTodoSelectionDrafts.value).filter(([id]) => id !== gameId))
        gameConfigErrors.value = Object.fromEntries(Object.entries(gameConfigErrors.value).filter(([id]) => id !== gameId))
      }
    } catch (error) {
      manager.rememberError('自动保存每日勾选', error)
      markDailySelectionFailure(gameId, true)
      gameConfigErrors.value = { ...gameConfigErrors.value, [gameId]: error instanceof Error ? error.message : String(error) }
    } finally {
      markDailySelectionSaving(gameId, false)
    }
  })
  dailySelectionSaveQueue = scheduled
  return scheduled
}

async function saveDailyGameConfig(gameId: string): Promise<boolean> {
  await dailySelectionSaveQueue.catch(() => undefined)
  const path = pathDraft(gameId)
  const dailyToolProfilesPatch = dailyToolProfilePatch(gameId)
  const manualRevision = manualConfigRevisions.get(gameId) ?? 0
  const selectionRevision = dailySelectionRevisions.get(gameId) ?? 0
  savingGameConfigId.value = gameId
  try {
    await manager.refresh({ quiet: true })
    const beforeStateVersion = snapshot.value.stateVersion
    const receipt = await manager.submitCommand('保存每日游戏配置', 'PATCH', '/config', {
      enabled: { [gameId]: dailyGameEnabled(gameId) },
      gamePaths: {
        [gameId]: {
          gamePath: emulatorGameIds.has(gameId) ? null : path.gamePath?.trim() || null,
          toolPath: path.toolPath?.trim() || null,
          emulator: emulatorGameIds.has(gameId) ? {
            ...emulatorDraft(gameId),
            consolePath: emulatorDraft(gameId).consolePath.trim(),
            adbPath: emulatorDraft(gameId).adbPath.trim(),
            instanceName: emulatorDraft(gameId).instanceName?.trim() || null,
            adbSerial: emulatorDraft(gameId).adbSerial.trim(),
          } : null,
        },
      },
      dailyTodoSelection: { [gameId]: selectedDailyTodoDefinitionIds(gameId) },
      ...(dailyToolProfilesPatch ? { dailyToolProfiles: dailyToolProfilesPatch } : {}),
    })
    if (receipt) {
      if (!await confirmSavedStateVersion(beforeStateVersion, receipt.acceptedStateVersion)) {
        gameConfigErrors.value = { ...gameConfigErrors.value, [gameId]: '保存结果尚未确认，未允许开始执行。' }
        return false
      }
      if ((manualConfigRevisions.get(gameId) ?? 0) === manualRevision) {
        gamePathDrafts.value = Object.fromEntries(Object.entries(gamePathDrafts.value).filter(([id]) => id !== gameId))
        if (gameId === 'WW') okWwProfileDraft.value = undefined
        if (gameId === 'Endfield') endfieldProfileDraft.value = undefined
        if (gameId === 'NTE') nteProfileDraft.value = undefined
        if (gameId === 'CZN') cznProfileDraft.value = undefined
      }
      if ((dailySelectionRevisions.get(gameId) ?? 0) === selectionRevision) {
        gameEnabledDrafts.value = Object.fromEntries(Object.entries(gameEnabledDrafts.value).filter(([id]) => id !== gameId))
        dailyTodoSelectionDrafts.value = Object.fromEntries(Object.entries(dailyTodoSelectionDrafts.value).filter(([id]) => id !== gameId))
        markDailySelectionFailure(gameId, false)
      }
      gameConfigErrors.value = Object.fromEntries(Object.entries(gameConfigErrors.value).filter(([id]) => id !== gameId))
      return true
    } else {
      gameConfigErrors.value = {
        ...gameConfigErrors.value,
          [gameId]: manager.errors[0]?.detail ?? '游戏配置没有保存成功。',
      }
      return false
    }
  } catch (error) {
    manager.rememberError('保存每日游戏配置失败', error)
    gameConfigErrors.value = { ...gameConfigErrors.value, [gameId]: error instanceof Error ? error.message : String(error) }
    return false
  } finally {
    savingGameConfigId.value = undefined
  }
}

async function flushDailyConfiguration(): Promise<boolean> {
  configurationFlushError.value = undefined
  await dailySelectionSaveQueue.catch(() => undefined)

  // Retry any optimistic switch/checkbox draft that did not receive a durable
  // config receipt. A failed save remains visible and must never fall through
  // into POST /batches.
  for (const gameId of selectionDirtyGameIds.value) {
    await saveDailySelection(gameId)
  }
  await dailySelectionSaveQueue.catch(() => undefined)
  if (failedDailySelectionIds.value.size > 0 || selectionDirtyGameIds.value.length > 0) {
    configurationFlushError.value = '每日范围尚未保存成功；已停止，没有创建执行批次。'
    return false
  }

  for (const gameId of [...manualDirtyGameIds.value]) {
    if (!await saveDailyGameConfig(gameId)) {
      configurationFlushError.value = `${allGames.value.find((game) => game.gameId === gameId)?.displayName ?? gameId} 的参数保存失败；已停止，没有创建执行批次。`
      return false
    }
  }
  if (manualDirtyGameIds.value.length > 0) {
    configurationFlushError.value = '保存期间参数又发生变化；请确认后再开始。'
    return false
  }
  return true
}

async function submitToday(mode: 'plan' | 'execute'): Promise<void> {
  if (mode === 'execute' && executionInProgress.value) return
  const expectedConfigurationRevision = configurationMutationRevision
  executionReadinessError.value = undefined
  busy.value = true
  try {
    if (!await flushDailyConfiguration()) return
    await Promise.all([
      manager.refresh({ quiet: true }),
      ...(mode === 'execute' ? [runtimeBindings.load()] : []),
    ])
    if (mode === 'execute' && executionInProgress.value) return
    if (
      configurationMutationRevision !== expectedConfigurationRevision
      || hasUnsavedConfiguration.value
    ) {
      configurationFlushError.value = '保存期间配置发生变化；已停止，没有创建执行批次。'
      return
    }
    const gameIds = queuedDailyGames.value.map((game) => game.gameId)
    if (!gameIds.length) {
      configurationFlushError.value = '没有已保存且已选择每日项的游戏，未创建批次。'
      return
    }
    if (mode === 'execute') {
      if (runtimeBindings.error.value) {
        executionReadinessError.value = `Manager runtimeBinding readiness 读取失败：${runtimeBindings.error.value}。没有创建执行批次。`
        return
      }
      if (!queuedRuntimeBindingReadiness.value.anyReady) {
        executionReadinessError.value = `所选游戏当前都不可执行：${unavailableQueuedGames.value.map((game) => `${game.displayName}：${game.reason}`).join('；')} 没有创建执行批次。`
        return
      }
    }
    await manager.submitCommand(mode === 'plan' ? '规划今日队列' : '执行今日队列', 'POST', '/batches', {
      cadence: 'daily',
      gameIds,
      mode,
      requestedBy: 'webgui',
    })
  } finally {
    busy.value = false
  }
}

function requestTodayStatusReset(gameId: string, displayName: string): void {
  if (executionInProgress.value || resettingGameId.value) return
  pendingResetGame.value = { gameId, displayName }
  resetConfirmDialog.value = true
}

function cancelTodayStatusReset(): void {
  if (resettingGameId.value) return
  resetConfirmDialog.value = false
  pendingResetGame.value = undefined
}

async function confirmTodayStatusReset(): Promise<void> {
  const target = pendingResetGame.value
  if (!target || executionInProgress.value || resettingGameId.value) return
  await resetTodayStatus(target.gameId, target.displayName)
  resetConfirmDialog.value = false
  pendingResetGame.value = undefined
}

async function resetTodayStatus(gameId: string, displayName: string): Promise<void> {
  if (executionInProgress.value || resettingGameId.value) return
  resettingGameId.value = gameId
  try {
    const query = new URLSearchParams({ action: 'reset', cadence: 'daily', gameId })
    const preview = await managerApi.get<TodoResetPreview>(`/todo-reset-preview?${query}`)
    const receipt = await manager.submitCommand(
      `重置 ${displayName} 今日状态`,
      'POST',
      '/todo-reset-requests',
      {
        gameIds: [gameId],
        cadence: 'daily',
        reason: 'operator_explicit_reset_current_game_daily_status',
        requestedBy: 'webgui',
      },
      { expectedStateVersion: preview.stateVersion },
    )
    if (receipt) await Promise.all([manager.refresh({ quiet: true }), dailyTodos.load()])
  } finally {
    resettingGameId.value = undefined
  }
}

async function cancelBatch(): Promise<void> {
  // Vue passes the click MouseEvent to a bare @click handler. Resolve the
  // current Manager projection here so the event cannot become `batch` and
  // produce /batches/undefined/cancel-requests.
  const batch = displayedExecutionBatch.value
  if (!batch || busy.value) return
  busy.value = true
  try {
    await manager.submitCommand(activeBatch.value ? '请求安全取消' : '停止遗留运行', 'POST', `/batches/${encodeURIComponent(batch.batchId)}/cancel-requests`, {
      reason: activeBatch.value
        ? 'operator_request_from_webgui'
        : 'operator_requested_stop_retained_execution_from_webgui',
      requestedBy: 'webgui',
    })
  } finally {
    busy.value = false
  }
}

async function resumeGameRun(runId: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    // Refresh first, then re-resolve the optimistic target from the new
    // availability projection. This prevents a stale Today page from resuming
    // a run that has already advanced elsewhere.
    await manager.refresh({ quiet: true })
    const resumeTarget = runResumeTargets.value.find((item) => item.runId === runId)
    if (!resumeTarget) {
      manager.rememberError('恢复游戏运行', new Error('刷新后该运行已不再允许安全恢复；页面没有提交请求。'))
      return
    }
    const target = resumeTarget
    const receipt = await manager.submitCommand(
      `恢复 ${allGames.value.find((game) => game.gameId === target.gameId)?.displayName ?? target.gameId}`,
      'POST',
      `/game-runs/${encodeURIComponent(target.runId)}/resume-requests`,
      {
        reason: 'explicit_operator_resume_from_today',
        requestedBy: 'webgui',
        ...(typeof target.expectedRunRevision === 'number' ? { expectedRunRevision: target.expectedRunRevision } : {}),
        ...(target.expectedCurrentAttemptId ? { expectedCurrentAttemptId: target.expectedCurrentAttemptId } : {}),
        ...(typeof target.expectedCurrentAttemptRevision === 'number' ? { expectedCurrentAttemptRevision: target.expectedCurrentAttemptRevision } : {}),
      },
    )
    if (receipt) await manager.refresh({ quiet: true })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <PageHeader
    eyebrow="今天 / 每日任务"
    title="今天的每日"
    :description="`任务日期 ${gameDayLabel}。只有完成项目和本次截图都确认后，才会显示为完成。`"
  />

  <v-alert v-if="!executionEnabled" type="info" variant="tonal" class="mb-5">
    当前执行功能暂不可用。“开始每日”保持禁用，不会启动游戏。
  </v-alert>
  <v-alert
    v-if="queuedDailyGames.length && (runtimeBindings.loading.value || runtimeBindings.error.value || unavailableQueuedGames.length)"
    :type="queuedRuntimeBindingReadiness.anyReady ? 'info' : 'warning'"
    variant="tonal"
    class="mb-5"
  >
    <template v-if="runtimeBindings.loading.value">正在读取 Manager 的逐游戏 runtimeBinding readiness；读取完成前不会启动执行批次。</template>
    <template v-else-if="runtimeBindings.error.value">逐游戏 runtimeBinding readiness 读取失败：{{ runtimeBindings.error.value }}。当前不会启动执行批次。</template>
    <template v-else>
      <strong>{{ queuedRuntimeBindingReadiness.anyReady ? '部分所选游戏当前不可执行。' : '所选游戏当前都不可执行。' }}</strong>
      <ul class="mt-2 ml-5">
        <li v-for="game in unavailableQueuedGames" :key="game.gameId">{{ game.displayName }}：{{ game.reason }}</li>
      </ul>
      <p v-if="queuedRuntimeBindingReadiness.anyReady" class="mt-2">执行请求仍会包含全部所选游戏；Manager 将最终裁决每个 Todo，并保留不可执行游戏的延后原因。</p>
    </template>
  </v-alert>
  <v-alert v-if="executionReadinessError" type="error" variant="tonal" class="mb-5">
    {{ executionReadinessError }}
  </v-alert>
  <v-alert v-if="executionInProgress && !humanTakeoverTargets.length && !runResumeTargets.length" type="warning" variant="tonal" class="mb-5">
    <strong>正在执行今天的每日。</strong> 为避免重复操作，游戏范围和参数暂时锁定；本次范围内有 {{ todayScope.blocked }} 项需要处理，旧日期的问题不会混入这里。
  </v-alert>
  <v-alert v-if="dailyTodos.error.value" type="warning" variant="tonal" class="mb-5">
    每日项目读取失败，当前进度暂时无法确认。详情可在“诊断”页查看。
  </v-alert>
  <v-alert v-if="gameIntegrationsError" type="warning" variant="tonal" class="mb-5">
    自动执行方式读取失败，暂时不会开始受影响的游戏。详情可在“诊断”页查看。
  </v-alert>
  <v-alert v-if="configurationFlushError" type="error" variant="tonal" class="mb-5">
    {{ configurationFlushError }} 请修正上方对应游戏的保存错误后再点“开始每日”。
  </v-alert>
  <v-alert v-if="canReviewEvidence" type="warning" variant="tonal" class="mb-5">
    当前运行正在等待完成截图确认。点击顶部“打开证据复核”逐项核对；没有可复核内容时仍会保持等待，不会误报完成。
  </v-alert>

  <div class="content-grid">
    <v-card class="panel span-12 primary-action-panel">
      <div class="panel-title">
        <div><h2>现在需要做什么</h2><p class="primary-action-reason">{{ primaryStopReason }}</p></div>
        <StatusBadge :state="humanTakeoverTargets.length ? 'human_required' : (canReviewEvidence || runResumeTargets.length ? 'review_required' : (executionInProgress ? 'executing' : (canExecuteQueuedGames ? 'ready' : 'blocked')))" />
      </div>
      <div class="panel-body primary-action-detail">
        <div class="primary-action-copy">
          <span>{{ todayScopeStatus }}</span>
          <span>本次范围：{{ todayScope.gameIds.length }} 款游戏 · {{ todayScopeUsable ? `${todoCompleted}/${todoRequired} 个必做项目` : '项目明细等待同步' }}</span>
          <div v-if="cleanupView" class="queue-cleanup-note" role="status" aria-live="polite">
            <strong>{{ cleanupView.title }}</strong><span>{{ cleanupView.summary }}</span>
          </div>
        </div>
        <div class="primary-action-buttons">
          <template v-if="humanTakeoverTargets.length">
            <v-btn v-for="target in humanTakeoverTargets" :key="target.runId" color="warning" @click="router.push(`/games/${encodeURIComponent(target.gameId)}`)">打开{{ allGames.find((game) => game.gameId === target.gameId)?.displayName ?? target.gameId }}人工接管详情</v-btn>
          </template>
          <v-btn v-else-if="canReviewEvidence" color="warning" :loading="busy" @click="router.push('/agent-workbench')">打开证据复核</v-btn>
          <template v-else-if="runResumeTargets.length">
            <v-btn
              v-for="target in runResumeTargets"
              :key="target.runId"
              color="primary"
              variant="tonal"
              :loading="busy"
              :disabled="!manager.supports('/game-runs/{runId}/resume-requests', 'post')"
              @click="resumeGameRun(target.runId)"
            >恢复{{ allGames.find((game) => game.gameId === target.gameId)?.displayName ?? target.gameId }}</v-btn>
          </template>
          <v-btn v-else-if="canReviewBatch" color="warning" :loading="busy" @click="router.push('/agent-workbench')">打开运行复核</v-btn>
          <v-btn v-else-if="displayedExecutionBatch?.result?.batchActionAvailability?.cancel === true" color="error" variant="tonal" :loading="busy" :disabled="!manager.supports('/batches/{batchId}/cancel-requests', 'post')" @click="cancelBatch">{{ mustStartFreshAfterCancel ? '结束旧运行后重新开始' : (activeBatch ? '安全停止当前执行' : '停止遗留运行') }}</v-btn>
          <v-btn v-else-if="!executionInProgress" color="primary" :loading="busy" :disabled="!queuedDailyGames.length || !canExecuteQueuedGames || !manager.supports('/batches', 'post')" @click="submitToday('execute')">{{ hasUnsavedConfiguration ? '保存并开始每日' : '开始每日' }}</v-btn>
        </div>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title">
        <div><h2>每日游戏配置</h2><p class="soft-note">勾选决定一键每日的范围；最右侧只显示今天的完成状态，不会被勾选操作改写。</p></div>
        <span class="soft-note">修改会在本机自动保存</span>
      </div>
      <div v-if="allGames.length" class="daily-game-config-list">
        <details v-for="game in allGames" :key="game.gameId" class="daily-game-config">
          <summary>
            <span class="daily-game-config-heading">
              <v-switch
                class="daily-game-enabled-toggle"
                :model-value="dailyGameEnabled(game.gameId)"
                :aria-label="`${game.displayName}纳入一键每日`"
                :title="dailyGameEnabled(game.gameId) ? '已纳入一键每日' : '未纳入一键每日'"
                color="primary"
                density="compact"
                hide-details
                :disabled="configurationLocked"
                @click.stop
                @update:model-value="updateDailyGameEnabled(game.gameId, Boolean($event))"
              />
              <span class="daily-game-title"><strong>{{ game.displayName }}</strong><small v-if="savingDailySelectionIds.has(game.gameId)">保存中</small></span>
            </span>
            <span class="daily-game-config-summary">{{ dailyGameEnabled(game.gameId) ? '已纳入每日' : '未纳入每日' }} · 已选 {{ selectedDailyTodoDefinitionIds(game.gameId).length }} 项 · {{ integrationSummary(game.gameId) }}</span>
          </summary>
          <div class="daily-game-config-body">
            <div class="game-setup-fields">
              <v-text-field
                v-if="!emulatorGameIds.has(game.gameId)"
                :model-value="pathDraft(game.gameId).gamePath ?? ''"
                label="游戏 EXE 路径"
                placeholder="例如 D:\\Games\\游戏目录\\Game.exe"
                density="compact"
                variant="outlined"
                hide-details
                :disabled="configurationLocked"
                @update:model-value="updatePathDraft(game.gameId, 'gamePath', String($event ?? ''))"
              />
              <template v-else>
                <v-text-field
                  :model-value="emulatorDraft(game.gameId).consolePath"
                  label="雷电控制台路径"
                  placeholder="例如 C:\\Game\\LDPlayer9\\ldconsole.exe"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEmulatorDraft(game.gameId, 'consolePath', String($event ?? ''))"
                />
                <v-text-field
                  :model-value="emulatorDraft(game.gameId).adbPath"
                  label="雷电 ADB 路径"
                  placeholder="例如 C:\\Game\\LDPlayer9\\adb.exe"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEmulatorDraft(game.gameId, 'adbPath', String($event ?? ''))"
                />
                <v-text-field
                  :model-value="emulatorDraft(game.gameId).instanceIndex"
                  label="模拟器实例编号"
                  type="number"
                  min="0"
                  max="32"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEmulatorDraft(game.gameId, 'instanceIndex', Number($event ?? 0))"
                />
                <v-text-field
                  :model-value="emulatorDraft(game.gameId).instanceName ?? ''"
                  label="模拟器实例名称"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEmulatorDraft(game.gameId, 'instanceName', String($event ?? ''))"
                />
                <v-text-field
                  :model-value="emulatorDraft(game.gameId).adbSerial"
                  label="ADB 设备序列号"
                  placeholder="emulator-5554"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEmulatorDraft(game.gameId, 'adbSerial', String($event ?? ''))"
                />
              </template>
              <v-text-field
                :model-value="pathDraft(game.gameId).toolPath ?? ''"
                label="工具路径"
                placeholder="例如 D:\\Tools\\辅助工具目录"
                density="compact"
                variant="outlined"
                hide-details
                :disabled="configurationLocked"
                @update:model-value="updatePathDraft(game.gameId, 'toolPath', String($event ?? ''))"
              />
              <v-btn color="primary" :loading="savingGameConfigId === game.gameId" :disabled="configurationLocked || !manager.supports('/config', 'patch')" @click="saveDailyGameConfig(game.gameId)">保存</v-btn>
              <v-btn
                color="warning"
                variant="tonal"
                :loading="resettingGameId === game.gameId"
                :disabled="configurationLocked || !manager.supports('/todo-reset-preview', 'get') || !manager.supports('/todo-reset-requests', 'post')"
                @click="requestTodayStatusReset(game.gameId, game.displayName)"
              >重置今日状态</v-btn>
            </div>
            <v-alert v-if="gameConfigErrors[game.gameId]" type="error" variant="tonal" density="compact">
              {{ gameConfigErrors[game.gameId] }}
            </v-alert>
            <v-alert v-if="integrationForGame(game.gameId)?.mappingStatus !== 'registered'" type="info" variant="tonal" density="compact">
              {{ integrationSummary(game.gameId) }}。当前选择仍会保存，开始时会再次确认能否自动执行。
            </v-alert>
            <section v-if="game.gameId === 'WW'" class="tool-profile ok-ww-profile">
              <strong>OK-WW 每日配置映射</strong>
              <p class="soft-note">选好体力路线和材料后，软件会按这些选择自动完成每日；不需要按快捷键或操作底层导航。</p>
              <div class="ok-ww-profile-grid">
                <v-select
                  :model-value="okWWProfile().whichToFarm"
                  label="体力路线"
                  :items="[
                    { title: '无音区', value: 'Tacet Suppression' },
                    { title: '锻造挑战', value: 'Forgery Challenge' },
                    { title: '模拟领域', value: 'Simulation Challenge' },
                  ]"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateOKWWFarmTarget"
                />
                <v-select
                  v-if="okWWProfile().whichToFarm === 'Tacet Suppression'"
                  :model-value="okWWProfile().tacetSuppressionNumber"
                  label="无音区目标编号"
                  :items="tacetSuppressionOptions"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateOKWWIndex('tacetSuppressionNumber', $event)"
                />
                <v-text-field
                  v-else-if="okWWProfile().whichToFarm === 'Forgery Challenge'"
                  :model-value="okWWProfile().forgeryChallengeNumber"
                  label="锻造挑战目标编号"
                  type="number"
                  min="1"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateOKWWIndex('forgeryChallengeNumber', $event)"
                />
                <v-select
                  v-else
                  :model-value="okWWProfile().materialSelection"
                  label="模拟领域材料"
                  :items="[
                    { title: '共鸣者经验', value: 'Resonator EXP' },
                    { title: '武器经验', value: 'Weapon EXP' },
                    { title: '贝币', value: 'Shell Credit' },
                  ]"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateOKWWMaterial"
                />
              </div>
              <v-checkbox
                :model-value="okWWProfile().farmNightmareNestForDailyEcho"
                label="通关 1 次梦魇聚落或残象聚落（+20 活跃，确保满 100）"
                density="compact"
                hide-details
                :disabled="configurationLocked"
                @update:model-value="updateOKWWProfile('farmNightmareNestForDailyEcho', Boolean($event))"
              />
            </section>
            <section v-if="game.gameId === 'Endfield'" class="tool-profile">
              <strong>终末地每日玩法草稿</strong>
              <p class="soft-note">这里的下拉会保存到 YeYu Gamer；等逐阶段 Adapter 接好后，保存的选择会原样下发，不会直接改上游工具文件。</p>
              <div class="ok-ww-profile-grid">
                <v-select
                  :model-value="endfieldProfile().staminaStage"
                  label="普通理智本"
                  :items="['干员经验', '干员进阶', '钱币收集', '技能提升', '武器经验', '武器进阶', '罗丹', '三位一体', '白垩界卫', '阮一', '聂菲斯', 'D96钢', '超距辉映管', '快子遴捡晶格', '象限拟合液', '三相纳米片', '枢纽区', '源石研究园', '试验园区', '矿脉源区', '供能高地', '武陵城', '清波寨', '首墩', '藏剑谷']"
                  density="compact" variant="outlined" hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEndfieldProfile('staminaStage', String($event ?? '超距辉映管'))"
                />
                <v-select
                  :model-value="endfieldProfile().rewardTier" label="奖励档位" :items="['保持当前', '低阶', '高阶']"
                  density="compact" variant="outlined" hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEndfieldProfile('rewardTier', $event === '低阶' || $event === '高阶' ? $event : '保持当前')"
                />
                <v-text-field
                  :model-value="endfieldProfile().staminaRotationStartDate" label="轮换起始日期" type="date"
                  density="compact" variant="outlined" hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEndfieldProfile('staminaRotationStartDate', String($event ?? '2026-04-06'))"
                />
                <v-select
                  :model-value="endfieldProfile().staminaRotation" label="理智本轮换" multiple chips
                  :items="['干员经验', '干员进阶', '钱币收集', '技能提升', '武器经验', '武器进阶', '罗丹', '三位一体', '白垩界卫', '阮一', '聂菲斯', 'D96钢', '超距辉映管', '快子遴捡晶格', '象限拟合液', '三相纳米片', '枢纽区', '源石研究园', '试验园区', '矿脉源区', '供能高地', '武陵城', '清波寨', '首墩', '藏剑谷']"
                  density="compact" variant="outlined" hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEndfieldProfile('staminaRotation', Array.isArray($event) ? $event.map(String) : [endfieldProfile().staminaStage])"
                />
                <v-select
                  :model-value="endfieldProfile().teamSlot" label="队伍槽" :items="['不换队伍', '1', '2', '3', '4', '5']"
                  density="compact" variant="outlined" hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateEndfieldProfile('teamSlot', ['1', '2', '3', '4', '5'].includes(String($event)) ? String($event) as EndfieldProfile['teamSlot'] : '不换队伍')"
                />
              </div>
            </section>
            <section v-if="game.gameId === 'NTE'" class="tool-profile">
              <strong>异环每日玩法草稿</strong>
              <p class="soft-note">一咖舍也是玩法参数。若实际流程出现购买或资源消耗确认，执行会停在那一个界面，而不是禁止你提前选择策略。</p>
              <div class="ok-ww-profile-grid">
                <v-select :model-value="nteProfile().anomalyTaskType" label="异象界域类型" :items="['经验与甲硬币', '异能升级材料', '弧盘突破材料', '空幕']" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('anomalyTaskType', $event === '异能升级材料' || $event === '弧盘突破材料' || $event === '空幕' ? $event : '经验与甲硬币')" />
                <v-select v-if="nteProfile().anomalyTaskType === '经验与甲硬币'" :model-value="nteProfile().expRewardTarget" label="经验与甲硬币目标" :items="['角色经验', '弧盘经验', '甲硬币']" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('expRewardTarget', $event === '角色经验' || $event === '弧盘经验' ? $event : '甲硬币')" />
                <v-text-field v-else :model-value="nteProfile().materialIndex" label="材料序号" type="number" min="1" max="6" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('materialIndex', Math.min(6, Math.max(1, Number($event) || 1)))" />
                <v-select :model-value="nteProfile().staminaTarget" label="自然体力目标（40 为一局）" :items="[40, 80, 120, 160, 200, 240, 280, 320, 360]" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('staminaTarget', Number($event) || 200)" />
                <v-select :model-value="nteProfile().coffeeMode" label="一咖舍策略" :items="['不执行', '领取/补货', '完整自动化']" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('coffeeMode', $event === '领取/补货' || $event === '完整自动化' ? $event : '不执行')" />
              </div>
              <v-checkbox :model-value="nteProfile().autoCycleSubTask" label="完成后下次自动轮换子目标" density="compact" hide-details :disabled="configurationLocked" @update:model-value="updateNTEProfile('autoCycleSubTask', Boolean($event))" />
            </section>
            <section v-if="game.gameId === 'CZN'" class="tool-profile">
              <strong>卡厄思梦境每日玩法配置</strong>
              <p class="soft-note">软件会按以下参数安全清理自然体力；周本、记忆碎片、商店和抽卡不会被带入。</p>
              <div class="ok-ww-profile-grid">
                <v-select :model-value="cznProfile().staminaCategory" label="体力分类" :items="['成长', '主战员', '辅战员', '潜能']" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateCZNCategory" />
                <v-select :model-value="cznProfile().staminaTarget" label="体力目标" :items="cznTargets[cznProfile().staminaCategory]" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateCZNProfile('staminaTarget', String($event ?? cznTargets[cznProfile().staminaCategory][0]))" />
                <v-select :model-value="cznProfile().battleEfficiency" label="战斗效率" :items="[1, 2, 3, 4, 5]" density="compact" variant="outlined" hide-details :disabled="configurationLocked" @update:model-value="updateCZNProfile('battleEfficiency', Math.min(5, Math.max(1, Number($event) || 4)))" />
              </div>
              <v-checkbox :model-value="true" label="持续到自然体力不足（固定安全策略）" density="compact" hide-details disabled />
            </section>
            <div class="daily-item-list">
              <div v-for="todo in dailyConfigTodos(game.gameId)" :key="todo.todoInstanceId" class="daily-item-config-row">
                <v-switch
                  class="daily-item-toggle"
                  :model-value="dailyTodoSelected(game.gameId, todo.todoDefinitionId)"
                  :label="todo.title"
                  color="primary"
                  inset
                  density="compact"
                  hide-details
                  :disabled="configurationLocked"
                  @update:model-value="updateDailyTodoSelection(game.gameId, todo.todoDefinitionId, Boolean($event))"
                />
                <span class="daily-item-capability">
                  <small v-if="integrationOperation(game.gameId, todo.operation)">已可自动执行</small>
                  <small v-else-if="integrationForGame(game.gameId)?.mappingStatus === 'incomplete'">这一步尚未接好，暂时不能自动执行</small>
                  <small v-else>这一步暂时不能自动执行</small>
                </span>
                <v-btn
                  size="small"
                  variant="tonal"
                  color="info"
                  :disabled="!stepEvidenceForTodo(todo).length"
                  @click="openStepScreenshots(game.displayName, todo)"
                >查看步骤截图</v-btn>
                <span class="today-todo-state" :class="{ complete: todo.status === 'completed' }">{{ todayTodoState(todo) }}</span>
              </div>
              <p v-if="!dailyConfigTodos(game.gameId).length" class="soft-note">这款游戏暂时没有可选的每日项目。</p>
            </div>
            <section class="completion-evidence">
              <div class="completion-evidence-heading">
                <strong>今日完成截图</strong>
                <small>领取完成后由 YeYu Gamer 截取；带北京时间水印并绑定本次运行</small>
              </div>
              <div v-if="todayEvidenceForGame(game.gameId).length" class="completion-evidence-grid">
                <a
                  v-for="artifact in todayEvidenceForGame(game.gameId)"
                  :key="artifact.artifactId"
                  class="completion-evidence-card"
                  :href="artifactContentUrl(artifact.artifactId)"
                  target="_blank"
                  rel="noopener"
                >
                  <img :src="artifactContentUrl(artifact.artifactId)" :alt="`${game.displayName} ${evidenceKindLabel(artifact)}`" loading="lazy">
                  <span>{{ evidenceKindLabel(artifact) }}</span>
                  <small>{{ formatTime(artifact.capturedAt) }}</small>
                </a>
              </div>
              <p v-else-if="dailyConfigTodos(game.gameId).some((todo) => stepEvidenceForTodo(todo).length >= 2)" class="soft-note">
                已记录每个完成步骤的执行前后截图；请使用对应步骤右侧的“查看步骤截图”核对。
              </p>
              <p v-else class="soft-note">今天还没有领取完成截图；没有截图不会验收为完成。</p>
            </section>
          </div>
        </details>
      </div>
      <EmptyState v-else title="没有可配置的游戏" detail="正在读取游戏列表。" />
    </v-card>

    <v-dialog v-model="resetConfirmDialog" max-width="560" persistent>
      <v-card>
        <div class="panel-title">
          <div>
            <h2>重置今日状态</h2>
            <span class="soft-note">只清除这款游戏今天的完成记录</span>
          </div>
        </div>
        <div class="panel-body">
          <p>
            确定重置「{{ pendingResetGame?.displayName ?? '当前游戏' }}」今天的状态吗？
            下一次“开始每日”会重新执行这款游戏；其他游戏和配置不会改变。
          </p>
          <div class="dialog-actions">
            <v-btn variant="text" :disabled="!!resettingGameId" @click="cancelTodayStatusReset">取消</v-btn>
            <v-btn
              color="warning"
              :loading="resettingGameId === pendingResetGame?.gameId"
              :disabled="!pendingResetGame || executionInProgress"
              @click="confirmTodayStatusReset"
            >确认重置</v-btn>
          </div>
        </div>
      </v-card>
    </v-dialog>

    <v-dialog v-model="stepScreenshotDialog" max-width="1180">
      <v-card class="step-screenshot-dialog">
        <div class="panel-title">
          <div>
            <h2>{{ selectedStepGameName }} · {{ selectedStepTodo?.title ?? '步骤截图' }}</h2>
            <span class="soft-note">同一次执行的前后画面；缺少任一张时，这一步不能确认完成</span>
          </div>
          <v-btn icon="mdi-close" variant="text" @click="stepScreenshotDialog = false" />
        </div>
        <div class="panel-body">
          <v-alert v-if="selectedStepEvidenceDuplicated" type="warning" variant="tonal" class="mb-4">
            这组前后截图的文件摘要完全相同，只能用于诊断，不能证明步骤发生了变化或已经完成。
          </v-alert>
          <div v-if="selectedStepEvidence.length" class="step-screenshot-grid">
            <a
              v-for="artifact in selectedStepEvidence"
              :key="artifact.artifactId"
              class="completion-evidence-card"
              :href="artifactContentUrl(artifact.artifactId)"
              target="_blank"
              rel="noopener"
            >
              <img :src="artifactContentUrl(artifact.artifactId)" :alt="stepEvidenceLabel(artifact)">
              <span>{{ stepEvidenceLabel(artifact) }}</span>
              <small>{{ formatTime(artifact.capturedAt) }}</small>
            </a>
          </div>
          <p v-else class="soft-note">这个步骤还没有同一运行绑定的前后截图。</p>
        </div>
      </v-card>
    </v-dialog>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>当前执行</h2><StatusBadge :state="displayedRunState" /></div>
      <div class="panel-body current-execution">
        <template v-if="humanTakeoverTargets.length">
          <div class="primary-cell"><small>等待人工接管</small><strong>{{ activeGame?.displayName ?? '当前游戏' }}</strong></div>
          <div class="primary-cell"><small>接下来</small><strong>{{ primaryNextAction }}</strong><span class="soft-note">已保留本次范围，后续游戏保持未启动。</span></div>
        </template>
        <template v-else-if="runResumeTargets.length">
          <div class="primary-cell"><small>等待恢复</small><strong>{{ activeGame?.displayName ?? '当前游戏' }}</strong></div>
          <div class="primary-cell"><small>接下来</small><strong>{{ primaryNextAction }}</strong><span class="soft-note">仍使用本次锁定的游戏和每日项目。</span></div>
        </template>
        <template v-else-if="executionInProgress">
          <div class="primary-cell"><small>正在执行的游戏</small><strong>{{ activeGame?.displayName ?? '正在确认当前游戏' }}</strong></div>
          <div class="primary-cell"><small>当前步骤</small><strong>{{ cleanupView?.inProgress ? cleanupView.title : activeTodo?.title ?? '正在启动、等待下一项或安全停止' }}</strong></div>
          <div class="primary-cell"><small>执行保护</small><strong>已锁定本次范围</strong><span class="soft-note">避免重复启动或在途中改动参数。</span></div>
          <div class="primary-cell"><small>接下来</small><strong>{{ primaryNextAction }}</strong><span class="soft-note">请只使用上方的主按钮继续。</span></div>
        </template>
        <EmptyState v-else title="当前没有执行中的每日" detail="开始后，这里会显示当前游戏和正在处理的每日项目。" />
      </div>
    </v-card>

    <v-card class="panel metric-card span-3">
      <span>今日必做</span><strong>{{ todayScopeUsable ? `${todoCompleted}/${todoRequired}` : '—' }}</strong><small>仅今天与本次选择</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>确认完成</span><strong>{{ todayScopeUsable ? acceptedCount : '—' }}</strong><small>每日项目与完成截图均已确认</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>等待复核</span><strong>{{ todayScopeUsable ? reviewCount : '—' }}</strong><small>点顶部唯一动作进入复核</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>需要处理</span><strong>{{ todayScopeUsable ? riskCount : '—' }}</strong><small>不含旧日期的历史问题</small>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title">
        <h2>本次每日</h2>
        <StatusBadge :state="displayedRunState" />
      </div>
      <div class="panel-body">
        <EmptyState
          v-if="!displayedExecutionBatch"
          title="当前没有正在运行的每日"
          detail="可以从顶部启动今天勾选的全部游戏。"
        />
        <template v-else>
          <div class="form-row">
            <div class="primary-cell"><small>当前游戏</small><strong>{{ activeGame?.displayName ?? '等待调度' }}</strong></div>
            <div class="primary-cell"><small>启动时间</small><strong>{{ formatTime(displayedExecutionBatch.startedAt) }}</strong></div>
            <div class="primary-cell"><small>当前情况</small><strong>{{ primaryStopReason }}</strong></div>
            <div class="primary-cell"><small>接下来</small><strong>{{ primaryNextAction }}</strong></div>
          </div>
          <v-progress-linear class="mt-5" color="primary" rounded height="8" :model-value="displayedExecutionBatch.progress ?? 0" />
          <p class="soft-note mt-3">关闭或刷新网页不会取消正在执行的每日。</p>
        </template>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>各游戏进度</h2><span class="soft-note">每日进度、完成确认与下一步</span></div>
      <div v-if="scopedGames.length" class="table-scroll">
        <table class="data-table">
          <thead><tr><th>游戏</th><th>每日进度</th><th>执行</th><th>完成</th><th>当前情况</th><th>接下来</th><th>更新</th></tr></thead>
          <tbody>
            <template v-for="game in scopedGames" :key="game.gameId">
              <tr>
                <td class="primary-cell">
                  <button class="game-link" @click="router.push(`/games/${encodeURIComponent(game.gameId)}`)"><strong>{{ game.displayName }}</strong></button>
                </td>
                <td>
                  <button v-if="todayScopeUsable" class="todo-toggle" @click="toggleTodo(game.gameId)">
                    {{ gameTodoSummary(game.gameId).requiredCompleted }}/{{ gameTodoSummary(game.gameId).requiredTotal }} · 每日已执行项
                    <span v-if="gameTodoSummary(game.gameId).counts.blocked">· 阻塞 {{ gameTodoSummary(game.gameId).counts.blocked }}</span>
                    <span v-if="gameTodoSummary(game.gameId).counts.review_required">· 复核 {{ gameTodoSummary(game.gameId).counts.review_required }}</span>
                  </button>
                  <span v-else class="soft-note">项目明细等待同步</span>
                </td>
                <td><StatusBadge :state="gameRuntimeState(game)" /></td>
                <td><StatusBadge :state="knownDisplayState(game.acceptanceState, acceptanceDisplayStates)" /></td>
                <td><StatusBadge :state="todayGameSituationState(game, gameTodos(game.gameId))" /></td>
                <td>
                  <button v-if="todayRuntimeIsIssue(gameRuntimeState(game)) || ['human_required', 'human_takeover'].includes(gameRuntimeState(game))" class="game-link" @click="router.push(`/games/${encodeURIComponent(game.gameId)}`)">{{ gameNextActionLabel(game) }}</button>
                  <template v-else>{{ gameNextActionLabel(game) }}</template>
                </td>
                <td>{{ formatTime(game.updatedAt) }}</td>
              </tr>
              <tr v-if="expanded.has(game.gameId)" class="todo-detail-row">
                <td colspan="7">
                  <section class="daily-plan">
                    <div class="daily-plan-heading">
                      <div>
                        <strong>本日需要完成的项目</strong>
                        <p class="soft-note">{{ gameTodoSummary(game.gameId).requiredCompleted }}/{{ requiredGameTodos(game.gameId).length }} 项已完成；每一项都显示当前状态与下一动作。</p>
                      </div>
                      <StatusBadge :state="knownDisplayState(game.acceptanceState, acceptanceDisplayStates)" small />
                    </div>
                    <TodoChecklist :items="requiredGameTodos(game.gameId)" compact product-mode />
                    <details v-if="optionalGameTodos(game.gameId).length" class="optional-todos">
                      <summary>查看 {{ optionalGameTodos(game.gameId).length }} 项可选每日项</summary>
                      <TodoChecklist :items="optionalGameTodos(game.gameId)" compact product-mode />
                    </details>
                    <p class="soft-note mt-3">所有必做项目完成后，还要确认同一次运行中的完成截图，才会显示为“确认完成”。</p>
                  </section>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <EmptyState v-else-if="!todayScopeUsable || todayScope.gameIds.length" title="本次范围等待同步" detail="已锁定的游戏和项目正在同步，暂不显示未确认的进度。" />
      <EmptyState v-else title="今日范围为空" detail="请在上方启用游戏并选择至少一个每日项；历史游戏不会显示在这里。" />
    </v-card>
  </div>
</template>

<style scoped>
.primary-cell small { color: var(--muted); }
.primary-action-panel { border-color: rgba(121,184,255,.32) !important; }
.primary-action-reason { margin: 7px 0 0; color: #dcecff; line-height: 1.55; }
.primary-action-detail { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 22px; color: var(--muted); font-size: .8rem; }
.primary-action-copy { display: grid; gap: 6px; }
.queue-cleanup-note { display: grid; gap: 4px; padding: 10px 12px; border-left: 3px solid #9dc5e7; background: rgba(142,190,230,.07); }
.primary-action-buttons { display: flex; flex-wrap: wrap; gap: 8px; }
.game-link,.todo-toggle { padding: 0; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; }
.game-link:hover strong,.todo-toggle:hover { color: #8fd4ff; }
.todo-toggle { color: #b9cde2; font: .74rem "Cascadia Code", Consolas, monospace; }
.daily-game-config-list { display: grid; gap: 9px; }
.daily-game-config { border: 1px solid rgba(142,190,230,.16); border-radius: 8px; background: rgba(4,14,25,.24); }
.daily-game-config summary { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 14px; cursor: pointer; }
.daily-game-config-heading { display: flex; align-items: center; gap: 10px; min-width: 0; }
.daily-game-title { display: grid; gap: 2px; min-width: 0; }
.daily-game-config summary small { color: var(--muted); }
.daily-game-config-summary { color: #9dc5e7; font-size: .78rem; }
.daily-game-config-body { display: grid; gap: 14px; padding: 0 14px 14px; border-top: 1px solid rgba(142,190,230,.12); }
.daily-game-enabled-toggle { flex: 0 0 auto; margin: 0; }
.daily-game-enabled-toggle :deep(.v-selection-control) { min-height: 32px; }
.tool-profile { display: grid; gap: 10px; padding-top: 14px; border-top: 1px solid rgba(142,190,230,.12); }
.tool-profile > p { margin: -5px 0 0; }
.ok-ww-profile-grid { display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap: 12px; }
.daily-item-list { display: grid; border-top: 1px solid rgba(142,190,230,.12); }
.daily-item-config-row { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(120px, .45fr) auto auto; gap: 12px; align-items: center; padding: 7px 0; border-bottom: 1px solid rgba(142,190,230,.08); }
.daily-item-toggle { min-width: 0; margin: 0; }
.daily-item-toggle :deep(.v-selection-control) { min-height: 34px; }
.daily-item-toggle :deep(.v-switch__track) { opacity: .9; }
.daily-item-capability { color: var(--muted); font-size: .72rem; overflow-wrap: anywhere; }
.today-todo-state { color: #f1c276; white-space: nowrap; font-size: .78rem; }
.today-todo-state.complete { color: #85d8aa; }
.completion-evidence { display: grid; gap: 10px; padding-top: 4px; border-top: 1px solid rgba(142,190,230,.12); }
.completion-evidence-heading { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.completion-evidence-heading small { color: var(--muted); text-align: right; }
.completion-evidence-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.completion-evidence-card { display: grid; gap: 5px; color: #d9ecff; text-decoration: none; border: 1px solid rgba(142,190,230,.18); border-radius: 8px; padding: 8px; background: rgba(4,14,25,.36); }
.completion-evidence-card:hover { border-color: rgba(91,189,255,.6); }
.completion-evidence-card img { width: 100%; aspect-ratio: 16 / 9; object-fit: contain; border-radius: 5px; background: #02070c; }
.completion-evidence-card span { font-size: .8rem; }
.completion-evidence-card small { color: var(--muted); }
.step-screenshot-dialog { overflow: hidden; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.step-screenshot-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.step-screenshot-grid .completion-evidence-card img { aspect-ratio: 16 / 9; object-fit: contain; }
.current-execution { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.current-execution .soft-note { display: block; margin-top: 3px; }
.todo-detail-row td { padding: 14px 18px 18px; background: rgba(4,14,25,.34); }
.daily-plan { display: grid; gap: 11px; }
.daily-plan-heading { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.daily-plan-heading p { margin: 4px 0 0; }
.optional-todos { margin-top: 2px; border-top: 1px solid rgba(142,190,230,.12); padding-top: 9px; }
.optional-todos summary { color: #8bb8df; cursor: pointer; font-size: .76rem; }
.optional-todos .todo-checklist { margin-top: 9px; }
.game-setup-fields { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto; gap: 12px; align-items: center; margin-top: 14px; }
@media (max-width: 900px) { .game-setup-fields,.current-execution,.daily-item-config-row,.ok-ww-profile-grid,.completion-evidence-grid,.step-screenshot-grid { grid-template-columns: 1fr; } }
</style>

<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { managerApi } from '../api/client'
import type {
  BatchRun,
  AdapterInfo,
  CompletionAdjudication,
  CompletionReview,
  ConfigDocument,
  JsonObject,
} from '../api/contracts'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import TodoChecklist from '../components/TodoChecklist.vue'
import CompletionLedger from '../components/CompletionLedger.vue'
import { useManagerStore } from '../stores/manager'
import { executionIsEnabled, runtimeBindingReadinessForGames } from '../utils/managerResources'
import { useCurrentTodos } from '../composables/useTodos'
import { useAutomationAssessments } from '../composables/useAutomationAssessments'
import { useResource } from '../composables/useResource'
import { cadenceSummary, deferredTodo, resetCountdown, schedulableTodo, todoSummaryFromItems, todosByGame } from '../utils/todos'
import {
  awaitingCompletionReviewRunIds,
  batchRunIds,
  blockersFromBatchResult,
} from '../utils/completion'

const manager = useManagerStore()
const { snapshot, activeBatch, executionInProgress, executionBusyReason, activeControllerLeaseCount, activeTodoBlockerCount } = storeToRefs(manager)
const selected = ref<string[]>([])
const executionStrategy = ref<'continue' | 'stop'>('continue')
const loadedSelection = ref<string[]>([])
const loadedStrategy = ref<'continue' | 'stop'>('continue')
const configLoaded = ref(false)
const configError = ref<string>()
const busy = ref(false)
const now = ref(Date.now())
const dailyTodos = useCurrentTodos('daily')
const automationAssessments = useAutomationAssessments()
const completionReviews = useResource<CompletionReview>('/completion-reviews?limit=500')
const completionAdjudications = useResource<CompletionAdjudication>('/completion-adjudications?limit=500')
const runtimeBindings = useResource<AdapterInfo>('/adapters')
const selectedBatchId = ref('')
const selectedBatchDetail = ref<BatchRun>()
const selectedBatchLoading = ref(false)
const selectedBatchError = ref<string>()
const executionReadinessError = ref<string>()
let clock: number | undefined

const allGames = computed(() => snapshot.value.games)
const enabledGames = computed(() => snapshot.value.games.filter((game) => game.enabled !== false))
const executionEnabled = computed(() => executionIsEnabled(snapshot.value))
const selectedRuntimeBindingReadiness = computed(() => runtimeBindingReadinessForGames(selected.value, runtimeBindings.items.value))
const unavailableSelectedGames = computed(() => selectedRuntimeBindingReadiness.value.unavailableGames.map((item) => ({
  ...item,
  displayName: allGames.value.find((game) => game.gameId === item.gameId)?.displayName ?? item.gameId,
})))
const canExecuteSelection = computed(() => (
  executionEnabled.value
  && !runtimeBindings.loading.value
  && !runtimeBindings.error.value
  && selectedRuntimeBindingReadiness.value.anyReady
))
const todoGroups = computed(() => todosByGame(dailyTodos.items.value))
const selectedTodos = computed(() => dailyTodos.items.value.filter((item) => selected.value.includes(item.gameId)))
const nextResetAt = computed(() => selectedTodos.value.map((item) => item.periodEndsAt).filter(Boolean).sort()[0])
const configDirty = computed(() => JSON.stringify({ order: selected.value, strategy: executionStrategy.value })
  !== JSON.stringify({ order: loadedSelection.value, strategy: loadedStrategy.value }))
const batchSummaries = computed(() => {
  const unique = new Map<string, BatchRun>()
  if (activeBatch.value) unique.set(activeBatch.value.batchId, activeBatch.value)
  for (const batch of snapshot.value.recentBatches ?? []) {
    if (!unique.has(batch.batchId)) unique.set(batch.batchId, batch)
  }
  return [...unique.values()]
})
const batchOptions = computed(() => batchSummaries.value.map((batch) => ({
  title: `${batch.batchId} · ${batch.state}${batch.result?.batchLineage ? ` · 续跑 #${batch.result.batchLineage.continuationOrdinal}` : ''}`,
  value: batch.batchId,
})))
const selectedBatch = computed(() => selectedBatchDetail.value?.batchId === selectedBatchId.value
  ? selectedBatchDetail.value
  : batchSummaries.value.find((batch) => batch.batchId === selectedBatchId.value))
const selectedBatchRunIds = computed(() => batchRunIds(selectedBatch.value))
const selectedBatchReviews = computed(() => {
  const runIds = new Set(selectedBatchRunIds.value)
  return completionReviews.items.value.filter((review) => runIds.has(review.runId))
})
const selectedBatchAdjudications = computed(() => completionAdjudications.items.value
  .filter((item) => item.batchId === selectedBatchId.value))
const selectedBatchBlockers = computed(() => blockersFromBatchResult(selectedBatch.value?.result))
const selectedAwaitingRunIds = computed(() => awaitingCompletionReviewRunIds(selectedBatch.value))
const selectedReviewWorkItemIds = computed(() => selectedBatch.value?.result?.completionReviewWorkItemIds ?? {})
const completionReviewPhaseStatus = computed(() => {
  const value = selectedBatch.value?.result?.completionReviewPhase?.status
  return typeof value === 'string' ? value : undefined
})
const selectedBatchActions = computed(() => selectedBatch.value?.result?.batchActionAvailability)
watch(allGames, (games) => {
  if (!configLoaded.value && games.length) selected.value = games.filter((game) => game.enabled !== false).map((game) => game.gameId)
}, { immediate: true })
watch(batchSummaries, (batches) => {
  if (!batches.some((batch) => batch.batchId === selectedBatchId.value)) {
    selectedBatchId.value = batches[0]?.batchId ?? ''
  }
}, { immediate: true })

function gameTodos(gameId: string) {
  return todoGroups.value.get(gameId) ?? []
}

function gameTodoSummary(gameId: string) {
  return snapshot.value.todo?.games[gameId]
    ?? cadenceSummary(snapshot.value.games.find((game) => game.gameId === gameId)?.todoSummary, 'daily')
    ?? todoSummaryFromItems(gameTodos(gameId), 'daily')
}

function completedSkipCount(gameId: string): number {
  return gameTodos(gameId).filter((item) => item.dispatchDisposition === 'completed_skip').length
}

function schedulableCount(gameId: string): number {
  return gameTodos(gameId).filter(schedulableTodo).length
}

function deferredCount(gameId: string): number {
  return gameTodos(gameId).filter(deferredTodo).length
}

function terminalSkipCount(gameId: string): number {
  return gameTodos(gameId).filter((item) => item.dispatchDisposition === 'terminal_skip').length
}

async function loadQueueConfig(): Promise<void> {
  configError.value = undefined
  try {
    const response = await managerApi.get<ConfigDocument>('/config')
    const values = response.config ?? {}
    const enabled = values.enabled && typeof values.enabled === 'object' ? values.enabled as Record<string, unknown> : {}
    const order = Array.isArray(values.order) ? values.order.filter((item): item is string => typeof item === 'string') : []
    const knownIds = new Set((response.allowedGameIds ?? allGames.value.map((game) => game.gameId)))
    const configured = [
      ...order.filter((gameId) => knownIds.has(gameId) && enabled[gameId] !== false),
      ...[...knownIds].filter((gameId) => !order.includes(gameId) && enabled[gameId] !== false),
    ]
    const strategy = values.execution_strategy === 'stop' || values.executionStrategy === 'stop' ? 'stop' : 'continue'
    selected.value = configured
    executionStrategy.value = strategy
    loadedSelection.value = [...configured]
    loadedStrategy.value = strategy
    configLoaded.value = true
  } catch (caught) {
    configError.value = caught instanceof Error ? caught.message : String(caught)
  }
}

async function submitSelection(mode: 'plan' | 'execute'): Promise<void> {
  if (configDirty.value || !selected.value.length || (mode === 'execute' && executionInProgress.value)) return
  executionReadinessError.value = undefined
  busy.value = true
  try {
    if (mode === 'execute') {
      await runtimeBindings.load()
      if (runtimeBindings.error.value) {
        executionReadinessError.value = `Manager runtimeBinding readiness 读取失败：${runtimeBindings.error.value}。没有创建执行批次。`
        return
      }
      if (!selectedRuntimeBindingReadiness.value.anyReady) {
        executionReadinessError.value = `所选游戏当前都不可执行：${unavailableSelectedGames.value.map((game) => `${game.displayName}：${game.reason}`).join('；')} 没有创建执行批次。`
        return
      }
    }
    await manager.submitCommand(mode === 'plan' ? '规划选中队列' : '执行选中队列', 'POST', '/batches', {
      cadence: 'daily',
      gameIds: selected.value,
      mode,
      requestedBy: 'webgui',
    })
  } finally {
    busy.value = false
  }
}

async function saveQueue(): Promise<void> {
  busy.value = true
  try {
    const body: JsonObject = {
      order: selected.value,
      enabled: Object.fromEntries(allGames.value.map((game) => [game.gameId, selected.value.includes(game.gameId)])),
      executionStrategy: executionStrategy.value,
    }
    const receipt = await manager.submitCommand('保存队列配置', 'PATCH', '/config', body)
    if (receipt) {
      await manager.refresh({ quiet: true })
      await loadQueueConfig()
    }
  } finally {
    busy.value = false
  }
}

async function loadSelectedBatch(): Promise<void> {
  const batchId = selectedBatchId.value
  selectedBatchDetail.value = undefined
  selectedBatchError.value = undefined
  if (!batchId) return
  selectedBatchLoading.value = true
  try {
    const loaded = await managerApi.get<BatchRun>(`/batches/${encodeURIComponent(batchId)}`)
    if (selectedBatchId.value === batchId) selectedBatchDetail.value = loaded
  } catch (caught) {
    if (selectedBatchId.value === batchId) {
      selectedBatchError.value = caught instanceof Error ? caught.message : String(caught)
    }
  } finally {
    if (selectedBatchId.value === batchId) selectedBatchLoading.value = false
  }
}

async function refreshBatchLedger(): Promise<void> {
  await Promise.all([
    manager.refresh({ quiet: true }),
    automationAssessments.load(),
    completionReviews.load(),
    completionAdjudications.load(),
  ])
  await loadSelectedBatch()
}

async function resumeSelectedBatch(): Promise<void> {
  const batch = selectedBatch.value
  if (!batch || selectedBatchActions.value?.resume !== true) return
  busy.value = true
  try {
    const receipt = await manager.submitCommand(
      '恢复未启动的批次成员',
      'POST',
      `/batches/${encodeURIComponent(batch.batchId)}/resume-requests`,
      {
        reason: 'operator requested the Manager-approved recovered Batch resume',
        requestedBy: 'webgui',
      },
    )
    if (receipt) await refreshBatchLedger()
  } finally {
    busy.value = false
  }
}

async function cancelSelectedBatch(): Promise<void> {
  const batch = selectedBatch.value
  if (!batch || selectedBatchActions.value?.cancel !== true) return
  busy.value = true
  try {
    const receipt = await manager.submitCommand(
      '协作取消批次',
      'POST',
      `/batches/${encodeURIComponent(batch.batchId)}/cancel-requests`,
      {
        reason: 'operator requested cooperative cancellation from Queue WebGUI',
        requestedBy: 'webgui',
      },
    )
    if (receipt) await refreshBatchLedger()
  } finally {
    busy.value = false
  }
}

watch(selectedBatchId, () => { void loadSelectedBatch() })

onMounted(() => {
  void loadQueueConfig()
  clock = window.setInterval(() => { now.value = Date.now() }, 30_000)
})
onScopeDispose(() => { if (clock !== undefined) window.clearInterval(clock) })
</script>

<template>
  <PageHeader
    eyebrow="QUEUE / SCOPE"
    title="队列与执行范围"
    description="在 Manager 里确定本次运行顺序、范围与重试预算。保存配置和启动批次是两个独立命令。"
  >
    <v-btn variant="tonal" :loading="busy" :disabled="!configLoaded || !configDirty || !manager.supports('/config', 'patch')" @click="saveQueue">保存配置</v-btn>
    <v-btn variant="tonal" :loading="busy" :disabled="!selected.length || configDirty || !!activeBatch || dailyTodos.loading.value || !!dailyTodos.error.value || !manager.supports('/batches', 'post')" @click="submitSelection('plan')">规划所选批次</v-btn>
    <v-btn color="primary" :loading="busy" :disabled="!selected.length || configDirty || executionInProgress || dailyTodos.loading.value || !!dailyTodos.error.value || !canExecuteSelection || !manager.supports('/batches', 'post')" @click="submitSelection('execute')">执行所选批次</v-btn>
  </PageHeader>

  <v-alert v-if="!executionEnabled" type="info" variant="tonal" class="mb-5">
    Manager 的实际执行开关当前关闭。保存范围与生成计划可用；执行按钮不会绕过开关，也不会启动游戏。
  </v-alert>
  <v-alert
    v-if="selected.length && (runtimeBindings.loading.value || runtimeBindings.error.value || unavailableSelectedGames.length)"
    :type="selectedRuntimeBindingReadiness.anyReady ? 'info' : 'warning'"
    variant="tonal"
    class="mb-5"
  >
    <template v-if="runtimeBindings.loading.value">正在读取 Manager 的逐游戏 runtimeBinding readiness；读取完成前不会启动执行批次。</template>
    <template v-else-if="runtimeBindings.error.value">逐游戏 runtimeBinding readiness 读取失败：{{ runtimeBindings.error.value }}。当前不会启动执行批次。</template>
    <template v-else>
      <strong>{{ selectedRuntimeBindingReadiness.anyReady ? '部分所选游戏当前不可执行。' : '所选游戏当前都不可执行。' }}</strong>
      <ul class="mt-2 ml-5">
        <li v-for="game in unavailableSelectedGames" :key="game.gameId">{{ game.displayName }}：{{ game.reason }}</li>
      </ul>
      <p v-if="selectedRuntimeBindingReadiness.anyReady" class="mt-2">执行请求仍会包含全部所选游戏；Manager 将最终裁决每个 Todo，并保留不可执行游戏的延后原因。</p>
    </template>
  </v-alert>
  <v-alert v-if="executionReadinessError" type="error" variant="tonal" class="mb-5">
    {{ executionReadinessError }}
  </v-alert>
  <v-alert v-if="executionInProgress" type="warning" variant="tonal" class="mb-5">
    <strong>执行入口已锁定。</strong> {{ executionBusyReason }} 活动桌面租约 {{ activeControllerLeaseCount }}，活动 Todo 阻塞 {{ activeTodoBlockerCount }}。请在“今日总览”的当前执行区域查看或停止遗留运行。
  </v-alert>
  <v-alert v-if="configError" type="error" variant="tonal" class="mb-5">
    无法读取 Manager 队列配置：{{ configError }}。保存、规划和执行均保持禁用。
  </v-alert>
  <v-alert v-else-if="configDirty" type="warning" variant="tonal" class="mb-5">
    当前范围或策略尚未保存。先由 Manager 保存并重新读取配置，之后才能用这份范围规划或执行批次。
  </v-alert>
  <v-alert v-if="dailyTodos.error.value" type="warning" variant="tonal" class="mb-5">
    无法读取当日 Todo：{{ dailyTodos.error.value }}。为了避免把未知工作误当成已完成，规划与执行保持禁用。
  </v-alert>
  <v-alert v-if="automationAssessments.error.value" type="warning" variant="tonal" class="mb-5">
    AutomationAssessment 读取失败：{{ automationAssessments.error.value }}。队列仍只使用 Manager 的 dispatchDisposition；页面不会从风险、能力或历史状态补算。
  </v-alert>
  <v-alert v-if="completionReviews.error.value || completionAdjudications.error.value" type="warning" variant="tonal" class="mb-5">
    完成复核台账读取不完整：{{ completionReviews.error.value ?? completionAdjudications.error.value }}。页面不会把空列表解释为没有阻塞。
  </v-alert>

  <div class="content-grid">
    <v-card class="panel span-5">
      <div class="panel-title"><h2>本次范围</h2><StatusBadge :state="activeBatch ? 'running' : 'not_started'" /></div>
      <div class="panel-body">
        <v-select
          v-model="selected"
          :items="allGames"
          item-title="displayName"
          item-value="gameId"
          label="游戏"
          multiple
          chips
          variant="outlined"
          density="comfortable"
        />
        <v-select v-model="executionStrategy" :items="[{ title: '故障后继续后续游戏', value: 'continue' }, { title: '故障后停止批次', value: 'stop' }]" label="故障调度策略" variant="outlined" />
        <div class="risk-box">
          重试预算不会越过人工接管、登录、付费、抽卡、账号设置、PVP 或不可逆动作。策略门仍由 Manager 在每次能力调用前裁决。
        </div>
      </div>
    </v-card>

    <v-card class="panel span-7">
      <div class="panel-title"><h2>执行顺序与当前周期</h2><span class="soft-note">距边界 {{ resetCountdown(nextResetAt, now) }}</span></div>
      <div class="panel-body">
        <EmptyState v-if="!selected.length" title="尚未选择游戏" detail="选择至少一个游戏后才能提交运行请求。" />
        <ol v-else class="queue-list">
          <li v-for="(gameId, index) in selected" :key="gameId">
            <div class="queue-head">
              <span class="queue-index">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <strong>{{ enabledGames.find((game) => game.gameId === gameId)?.displayName ?? gameId }}</strong>
                <small>{{ gameId }} · {{ gameTodoSummary(gameId).requiredCompleted }}/{{ gameTodoSummary(gameId).requiredTotal }} 必做已完成</small>
              </div>
              <StatusBadge :state="snapshot.games.find((game) => game.gameId === gameId)?.runtimeState" />
            </div>
            <div class="queue-counters">
              <span>已完成跳过 {{ completedSkipCount(gameId) }}</span>
              <span>Manager 判定可调度 {{ schedulableCount(gameId) }}</span>
              <span>Manager 延后/不支持 {{ deferredCount(gameId) }}</span>
              <span>终态跳过 {{ terminalSkipCount(gameId) }}</span>
            </div>
            <TodoChecklist :items="gameTodos(gameId)" :assessments="automationAssessments.items.value" compact />
          </li>
        </ol>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title">
        <div><h2>批次详情与完成复核</h2><span class="soft-note">读取 Batch result，不从客户端推断封印结果</span></div>
        <v-btn size="small" variant="tonal" :loading="selectedBatchLoading || completionReviews.loading.value || completionAdjudications.loading.value" @click="refreshBatchLedger">刷新台账</v-btn>
      </div>
      <div class="panel-body">
        <EmptyState v-if="!batchSummaries.length" title="尚无批次" detail="创建批次后，这里会显示等待复核的 Run 与最终完成合同。" />
        <template v-else>
          <v-select v-model="selectedBatchId" :items="batchOptions" label="查看批次" variant="outlined" density="comfortable" />
          <v-alert v-if="selectedBatchError" type="warning" variant="tonal" class="mb-4">
            批次详情读取失败，以下内容最多来自 snapshot 摘要：{{ selectedBatchError }}
          </v-alert>
          <div v-if="selectedBatch" class="batch-summary mb-4">
            <div><span>状态</span><StatusBadge :state="selectedBatch.state" small /></div>
            <div><span>CompletionReview 阶段</span><StatusBadge :state="completionReviewPhaseStatus ?? 'unknown'" small /></div>
            <div><span>等待 Run</span><strong>{{ selectedAwaitingRunIds.length }}</strong></div>
            <div><span>acceptedDone</span><strong>{{ selectedBatch.result?.acceptedDone === true ? 'true' : selectedBatch.result?.acceptedDone === false ? 'false' : '未裁决' }}</strong></div>
            <div><span>sealVersion</span><strong>{{ selectedBatch.result?.sealVersion ?? '未封印' }}</strong></div>
            <div><span>Root Batch</span><strong>{{ selectedBatch.result?.batchLineage?.rootBatchId ?? '等待 Manager 投影' }}</strong></div>
            <div><span>Predecessor</span><strong>{{ selectedBatch.result?.batchLineage ? (selectedBatch.result.batchLineage.predecessorBatchId ?? '无') : '等待 Manager 投影' }}</strong></div>
            <div><span>续跑序号</span><strong>{{ selectedBatch.result?.batchLineage?.continuationOrdinal ?? '等待投影' }}</strong></div>
            <div><span>Resume Intent</span><strong>{{ selectedBatch.result?.batchLineage ? (selectedBatch.result.batchLineage.resumeIntentId ?? '无') : '等待 Manager 投影' }}</strong></div>
            <div><span>当前 attempt 证据</span><strong>{{ selectedBatch.result?.currentAttemptEvidenceArtifactIds?.length ?? '等待投影' }}</strong></div>
            <div><span>继承证据</span><strong>{{ selectedBatch.result?.carriedEvidenceArtifactIds?.length ?? '等待投影' }}</strong></div>
          </div>
          <p v-if="selectedBatch?.result?.acceptanceReason" class="batch-reason mb-4">验收说明：{{ selectedBatch.result.acceptanceReason }}</p>
          <div v-if="selectedBatchActions" class="batch-actions mb-4">
            <div>
              <strong>{{ selectedBatchActions.nextAction }}</strong>
              <span>{{ selectedBatchActions.reasonCode }} · {{ selectedBatchActions.reason }}</span>
            </div>
            <v-btn
              v-if="selectedBatchActions.review"
              variant="tonal"
              to="/evidence"
            >查看证据与人工复核</v-btn>
            <v-btn
              variant="tonal"
              :loading="busy"
              :disabled="!selectedBatchActions.cancel || !manager.supports('/batches/{batchId}/cancel-requests', 'post')"
              @click="cancelSelectedBatch"
            >协作取消</v-btn>
            <v-btn
              color="primary"
              :loading="busy"
              :disabled="!selectedBatchActions.resume || !manager.supports('/batches/{batchId}/resume-requests', 'post')"
              @click="resumeSelectedBatch"
            >恢复未启动成员</v-btn>
          </div>
          <p v-if="selectedBatch?.result?.unresolvedRequiredTodoIds?.length" class="batch-reason mb-4">未解决 required Todo：{{ selectedBatch.result.unresolvedRequiredTodoIds.join(' · ') }}</p>
          <p v-if="selectedBatch?.result?.sealEvidenceArtifactIds?.length" class="batch-reason mb-4">封印证据：{{ selectedBatch.result.sealEvidenceArtifactIds.join(' · ') }}</p>
          <CompletionLedger
            v-if="selectedBatch"
            :reviews="selectedBatchReviews"
            :adjudications="selectedBatchAdjudications"
            :awaiting-run-ids="selectedAwaitingRunIds"
            :review-work-item-ids="selectedReviewWorkItemIds"
            :blockers="selectedBatchBlockers"
          />
        </template>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>调度约束</h2></div>
      <div class="panel-body form-row">
        <div class="risk-box"><strong>单桌面控制租约</strong><br />任何时刻最多一个 Adapter 持有 ControllerLease；WebGUI 不持有租约。</div>
        <div class="risk-box"><strong>证据先于完成</strong><br />游戏退出、脚本成功或单次人工判断都不会单独改变 acceptanceState。</div>
        <div class="risk-box"><strong>Todo 不是验收合同</strong><br />即使 required Todo 全部勾选，也要等待同一 GameRun 的新证据与 Manager 写入 <code>accepted_done</code>。</div>
      </div>
    </v-card>
  </div>
</template>

<style scoped>
.queue-list { display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }
.queue-list li { display: grid; gap: 12px; padding: 13px; border: 1px solid var(--line); border-radius: 12px; background: rgba(8,22,37,.52); }
.queue-head { display: grid; grid-template-columns: 42px 1fr auto; align-items: center; gap: 12px; }
.queue-list strong, .queue-list small { display: block; }
.queue-list small { margin-top: 3px; color: var(--muted); font-size: .7rem; }
.queue-index { color: #71b9e9; font: .76rem "Cascadia Code", monospace; }
.queue-counters { display: flex; flex-wrap: wrap; gap: 7px 16px; padding-left: 54px; color: #8da7c4; font-size: .7rem; }
.batch-summary { display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 8px; }.batch-summary > div { display: grid; gap: 5px; padding: 9px; border: 1px solid var(--line); border-radius: 9px; background: rgba(8,22,37,.45); }.batch-summary span { color: var(--muted); font-size: .65rem; }.batch-summary strong { color: #dcecf8; font-size: .74rem; overflow-wrap: anywhere; }
.batch-reason { color: #a9bfd3; font-size: .7rem; line-height: 1.5; }
.batch-actions { display: flex; align-items: center; justify-content: flex-end; gap: 9px; padding: 11px; border: 1px solid var(--line); border-radius: 10px; background: rgba(8,22,37,.45); }.batch-actions > div { min-width: 0; margin-right: auto; }.batch-actions strong,.batch-actions span { display: block; overflow-wrap: anywhere; }.batch-actions strong { color: #dcecf8; font-size: .76rem; }.batch-actions span { margin-top: 4px; color: var(--muted); font-size: .66rem; }
@media (max-width: 900px) { .batch-summary { grid-template-columns: repeat(2, minmax(0,1fr)); } }
</style>

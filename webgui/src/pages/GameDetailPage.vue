<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { managerApi } from '../api/client'
import {
  extractItems,
  type CapabilityDefinition,
  type CompletionAdjudication,
  type CompletionReview,
  type GameDetail,
  type GameRunRecord,
  type PageResult,
  type RunAttempt,
} from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useAutomationAssessments } from '../composables/useAutomationAssessments'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import JsonPanel from '../components/JsonPanel.vue'
import StatusBadge from '../components/StatusBadge.vue'
import TodoChecklist from '../components/TodoChecklist.vue'
import CompletionLedger from '../components/CompletionLedger.vue'
import { useManagerStore } from '../stores/manager'
import { formatTime } from '../utils/format'
import { cadenceSummary, resetCountdown, todoDiagnostics, todoSummaryFromItems } from '../utils/todos'
import { blockersFromDiagnostics, blockersFromTodos } from '../utils/completion'
import { artifactContentPath } from '../utils/managerResources'
import { accountIdOf, accountTodos, loadedAttemptCounts, scopeGameDetailToAccount } from '../utils/gameAccounts'

const manager = useManagerStore()
const { snapshot, executionInProgress, executionBusyReason } = storeToRefs(manager)
const route = useRoute()
const router = useRouter()
const gameId = ref('')
const detail = ref<GameDetail>()
const loading = ref(false)
const error = ref<string>()
const completionLedgerError = ref<string>()
const completionLedgerLoading = ref(false)
const completionReviews = ref<CompletionReview[]>([])
const completionAdjudications = ref<CompletionAdjudication[]>([])
const busy = ref(false)
const now = ref(Date.now())
let clock: number | undefined
const capabilities = useResource<CapabilityDefinition>('/capabilities')
const automationAssessments = useAutomationAssessments()

const gameOptions = computed(() => snapshot.value.games.map((game) => ({ title: game.displayName, value: game.gameId })))
const fallback = computed(() => snapshot.value.games.find((game) => game.gameId === gameId.value))
const requestedRunId = computed(() => typeof route.query.runId === 'string' ? route.query.runId : undefined)
const requestedAccountId = computed(() => typeof route.query.accountId === 'string' ? route.query.accountId : undefined)
const accountScoped = computed(() => gameId.value === 'WW' || Boolean(requestedRunId.value || requestedAccountId.value))
const shown = computed<GameDetail | undefined>(() => detail.value ?? (accountScoped.value ? undefined : fallback.value as GameDetail | undefined))
const executionEnabled = computed(() => snapshot.value.manager?.legacyExecutionEnabled === true)
const capabilityEnabled = (capabilityId: string) => computed(() =>
  capabilities.items.value.some((item) => item.capabilityId === capabilityId && item.enabled === true),
)
const dailyPlanEnabled = capabilityEnabled('game.daily.plan')
const dailyRunEnabled = capabilityEnabled('game.daily.run')
const resumeEnabled = capabilityEnabled('run.resume.request')
const screenshotEnabled = capabilityEnabled('observation.capture.request')
const latestScreenshot = computed(() => (shown.value?.artifacts ?? [])
  .filter((item) => item.kind === 'window-screenshot' && item.contentType === 'image/png')
  .sort((left, right) => String(right.capturedAt).localeCompare(String(left.capturedAt)))[0])
const takeoverActive = computed(() => ['human_takeover', 'human_required'].includes(shown.value?.runtimeState ?? '')
  || ['human_takeover', 'human_required'].includes(shown.value?.stage ?? ''))
const dailyTodos = computed(() => (detail.value?.todoInstances ?? [])
  .filter((item) => item.cadence === 'daily')
  .sort((left, right) => left.orderIndex - right.orderIndex))
const weeklyTodos = computed(() => (detail.value?.todoInstances ?? [])
  .filter((item) => item.cadence === 'weekly')
  .sort((left, right) => left.orderIndex - right.orderIndex))
const resumeProjection = computed(() => [...dailyTodos.value, ...weeklyTodos.value]
  .find((item) => item.actionAvailability.resume))
const resumeProjectionText = computed(() => {
  const projected = resumeProjection.value
  if (projected) {
    return `Manager 将 ${projected.title} 投影为可恢复；下一动作：${projected.actionAvailability.nextAction}`
  }
  if (dailyTodos.value.length || weeklyTodos.value.length) {
    return 'Manager 当前 Todo actionAvailability.resume 均为 false；恢复按钮保持禁用。'
  }
  return 'Manager 尚未返回 Todo actionAvailability；恢复按钮失败关闭。'
})
const dailySummary = computed(() => detail.value?.todoSummary?.daily
  ?? (accountScoped.value ? undefined : cadenceSummary(fallback.value?.todoSummary, 'daily'))
  ?? todoSummaryFromItems(dailyTodos.value, 'daily'))
const weeklySummary = computed(() => detail.value?.todoSummary?.weekly
  ?? todoSummaryFromItems(weeklyTodos.value, 'weekly'))
const attemptCounts = computed(() => loadedAttemptCounts(shown.value))
const diagnosticBlockers = computed(() => blockersFromDiagnostics(todoDiagnostics(
  shown.value?.attemptAnalysis ? { attemptAnalysis: shown.value.attemptAnalysis } : undefined,
)))
const completionBlockers = computed(() => {
  const values = [
    ...blockersFromTodos(detail.value?.todoInstances ?? []),
    ...diagnosticBlockers.value,
  ]
  return [...new Map(values.map((item) => [`${item.source}:${item.blockerId}`, item])).values()]
})
const humanRequiredAttemptCount = computed(() => attemptCounts.value.humanRequiredAttemptCount ?? '尚未读取')

type PagePayload<T> = T[] | PageResult<T> | { data?: T[] }

async function loadCompletionLedger(runId?: string): Promise<void> {
  completionReviews.value = []
  completionAdjudications.value = []
  completionLedgerError.value = undefined
  if (!runId) return
  completionLedgerLoading.value = true
  const encodedRunId = encodeURIComponent(runId)
  try {
    const [reviewsResult, adjudicationsResult] = await Promise.allSettled([
      managerApi.get<PagePayload<CompletionReview>>(`/completion-reviews?runId=${encodedRunId}&limit=100`),
      managerApi.get<PagePayload<CompletionAdjudication>>(`/completion-adjudications?runId=${encodedRunId}&limit=100`),
    ])
    if (runId !== shown.value?.runId) return
    if (reviewsResult.status === 'fulfilled') completionReviews.value = extractItems(reviewsResult.value).filter((review) => review.runId === runId)
    if (adjudicationsResult.status === 'fulfilled') completionAdjudications.value = extractItems(adjudicationsResult.value).filter((item) => item.runId === runId)
    const failures = [reviewsResult, adjudicationsResult]
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason))
    if (failures.length) completionLedgerError.value = failures.join('；')
  } finally {
    completionLedgerLoading.value = false
  }
}

let loadRevision = 0
async function load(): Promise<void> {
  if (!gameId.value) return
  const revision = ++loadRevision
  const runId = requestedRunId.value
  const explicitAccountId = requestedAccountId.value
  const scoped = accountScoped.value
  loading.value = true
  error.value = undefined
  detail.value = undefined
  try {
    let loaded = await managerApi.get<GameDetail>(`/games/${encodeURIComponent(gameId.value)}`)
    if (scoped) {
      let run = runId ? await managerApi.get<GameRunRecord>(`/game-runs/${encodeURIComponent(runId)}`) : undefined
      const accountId = explicitAccountId ?? (run ? accountIdOf(run) : 'default')
      if (!run) {
        const latest = accountTodos(loaded.todoInstances ?? [], loaded.gameId, accountId)
          .filter((todo) => todo.runId).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0]?.runId
        if (latest) run = await managerApi.get<GameRunRecord>(`/game-runs/${encodeURIComponent(latest)}`)
      }
      // The game-wide detail history is bounded and may omit the selected Run.
      const attempts = run ? extractItems(await managerApi.get<PagePayload<RunAttempt>>(
        `/game-runs/${encodeURIComponent(run.runId)}/attempts?limit=500`,
      )) : undefined
      loaded = scopeGameDetailToAccount({ ...loaded, runAttempts: attempts }, accountId, run)
    }
    if (revision !== loadRevision) return
    detail.value = loaded
    await loadCompletionLedger(detail.value.runId)
  } catch (caught) {
    if (revision !== loadRevision) return
    error.value = caught instanceof Error ? caught.message : String(caught)
    detail.value = undefined
    await loadCompletionLedger(scoped ? undefined : fallback.value?.runId)
  } finally {
    if (revision === loadRevision) loading.value = false
  }
}

async function refreshPage(): Promise<void> {
  await Promise.all([load(), automationAssessments.load()])
}

watch(() => route.params.gameId, (value) => {
  const next = typeof value === 'string' ? value : snapshot.value.games[0]?.gameId ?? ''
  if (next && next !== gameId.value) gameId.value = next
}, { immediate: true })
watch(gameId, (value) => {
  if (value && route.params.gameId !== value) void router.replace(`/games/${encodeURIComponent(value)}`)
  void load()
})
watch(() => [requestedRunId.value, requestedAccountId.value], () => void load())
watch(() => snapshot.value.games, (games) => {
  if (!gameId.value && games[0]) gameId.value = games[0].gameId
}, { immediate: true })
onMounted(load)
onMounted(() => { clock = window.setInterval(() => { now.value = Date.now() }, 30_000) })
onScopeDispose(() => { if (clock !== undefined) window.clearInterval(clock) })

async function command(label: string, path: string, body: Record<string, unknown> = {}): Promise<void> {
  if (path === '/game-runs' && body.mode === 'execute' && executionInProgress.value) return
  busy.value = true
  await manager.submitCommand(label, 'POST', path, {
    ...body,
    ...(path === '/game-runs' && shown.value?.accountId ? { accountId: shown.value.accountId } : {}),
  })
  busy.value = false
  await load()
}

async function captureScreenshot(): Promise<void> {
  if (!shown.value?.runId) return
  await command('截取游戏窗口', '/capability-invocations', {
    capability: 'observation.capture.request',
    arguments: { gameId: shown.value.gameId, runId: shown.value.runId },
    requestedBy: 'webgui',
  })
}

</script>

<template>
  <PageHeader
    eyebrow="GAME / RUN / ATTEMPT"
    title="游戏详情"
    description="查看逐项 Todo、尝试与证据，再对照运行和业务验收。恢复只调度当前周期未完成的 required Todo，并继续引用同一 GameRun。"
  >
    <v-select v-model="gameId" :items="gameOptions" label="选择游戏" variant="outlined" density="compact" hide-details style="min-width: 230px" />
    <v-btn variant="tonal" :loading="loading || completionLedgerLoading || automationAssessments.loading.value" @click="refreshPage">刷新</v-btn>
  </PageHeader>

  <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />
  <v-alert v-if="error" type="warning" variant="tonal" class="mb-4">{{ accountScoped ? '所选账号或运行读取失败，未展示其他账号的数据' : '详细查询失败，正在展示 snapshot 摘要' }}：{{ error }}</v-alert>
  <v-alert v-if="capabilities.error.value" type="warning" variant="tonal" class="mb-4">能力目录读取失败，所有控制按钮保持失败关闭：{{ capabilities.error.value }}</v-alert>
  <v-alert v-if="completionLedgerError" type="warning" variant="tonal" class="mb-4">完成复核台账读取不完整：{{ completionLedgerError }}。空列表不会被解释为没有阻塞或已经完成。</v-alert>
  <v-alert v-if="automationAssessments.error.value" type="warning" variant="tonal" class="mb-4">AutomationAssessment 读取失败：{{ automationAssessments.error.value }}。Todo 仍显示 Manager 的调度投影，不根据历史尝试补算。</v-alert>
  <v-alert v-if="executionInProgress" type="warning" variant="tonal" class="mb-4">Manager 当前执行占用：{{ executionBusyReason }} “执行单游戏”已禁用；运行详情与停止入口请在今日总览查看。</v-alert>
  <EmptyState v-if="!shown" title="尚无游戏数据" detail="Manager snapshot 中没有可展示的游戏。" />

  <div v-else class="content-grid">
    <v-card class="panel span-12">
      <div class="panel-title">
        <div><h2>{{ shown.displayName }}</h2><span class="soft-note mono">{{ shown.gameId }} · {{ shown.runId ?? 'no active run' }}</span></div>
        <StatusBadge :state="shown.stage ?? shown.bootstrapStage ?? shown.runtimeState" />
      </div>
      <div class="panel-body">
        <div class="action-row">
          <v-btn variant="tonal" :loading="busy" :disabled="!dailyPlanEnabled || !manager.supports('/game-runs', 'post')" @click="command('规划单游戏', '/game-runs', { gameId: shown.gameId, cadence: 'daily', mode: 'plan', requestedBy: 'webgui' })">规划单游戏</v-btn>
          <v-btn color="primary" :loading="busy" :disabled="executionInProgress || !executionEnabled || !dailyRunEnabled || !manager.supports('/game-runs', 'post')" @click="command('执行单游戏', '/game-runs', { gameId: shown.gameId, cadence: 'daily', mode: 'execute', requestedBy: 'webgui' })">执行单游戏</v-btn>
          <v-btn
            variant="tonal"
            :disabled="!shown.runId || !resumeEnabled || !resumeProjection || !manager.supports('/game-runs/{runId}/resume-requests', 'post')"
            :loading="busy"
            @click="command('恢复运行', `/game-runs/${encodeURIComponent(shown.runId ?? '')}/resume-requests`, { reason: 'explicit_operator_resume', requestedBy: 'webgui' })"
          >明确恢复</v-btn>
          <v-btn
            color="warning"
            variant="tonal"
            :disabled="!shown.runId || takeoverActive || !manager.supports('/game-runs/{runId}/takeover-requests', 'post')"
            :loading="busy"
            @click="command('申请人工接管', `/game-runs/${encodeURIComponent(shown.runId ?? '')}/takeover-requests`, { reason: 'operator_review', requestedBy: 'webgui' })"
          >申请人工接管</v-btn>
          <v-btn
            color="success"
            variant="tonal"
            :disabled="!shown.runId || !takeoverActive || !manager.supports('/game-runs/{runId}/takeover-release-requests', 'post')"
            :loading="busy"
            @click="command('释放人工接管', `/game-runs/${encodeURIComponent(shown.runId ?? '')}/takeover-release-requests`, { reason: 'operator_explicit_release', requestedBy: 'webgui' })"
          >释放人工接管</v-btn>
          <v-btn
            color="info"
            variant="tonal"
            :disabled="!shown.runId || !screenshotEnabled || !manager.supports('/capability-invocations', 'post')"
            :loading="busy"
            @click="captureScreenshot"
          >截图</v-btn>
        </div>
        <p class="soft-note mt-4">按钮返回的是命令 receipt。执行能力当前{{ executionEnabled && dailyRunEnabled ? '已开放' : '关闭' }}；{{ resumeProjectionText }}；恢复请求还要求 Manager capability <code>run.resume.request</code> 为 enabled。人工接管与释放走专用 typed run-control API。只有后续 Manager 事件与证据事务可以改变运行或验收状态。</p>
      </div>
    </v-card>

    <v-card class="panel span-4">
      <div class="panel-title"><h2>运行轴</h2></div>
      <div class="panel-body"><StatusBadge :state="shown.runtimeState" /><p class="soft-note mt-3">{{ shown.nextAction ?? '等待 Manager 决定下一动作' }}</p></div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title">
        <h2>最新游戏窗口截图</h2>
        <span class="soft-note mono">{{ latestScreenshot?.artifactId ?? '尚未采集' }}</span>
      </div>
      <div class="panel-body">
        <img
          v-if="latestScreenshot"
          class="artifact-image"
          :src="artifactContentPath(latestScreenshot.artifactId)"
          :alt="`${shown.displayName} 最新窗口截图`"
        />
        <EmptyState v-else title="还没有截图" detail="运行建立后，点击上方“截图”由 Manager 采集白名单游戏窗口。" />
        <p class="soft-note mt-3">只截取与当前 GameId 和 GameRun 匹配的已登记游戏窗口；网页复核仅使用 opaque artifact。</p>
      </div>
    </v-card>

    <v-card class="panel span-4">
      <div class="panel-title"><h2>验收轴</h2></div>
      <div class="panel-body"><StatusBadge :state="shown.acceptanceState" /><p class="soft-note mt-3">证据 {{ shown.evidenceCount ?? shown.artifacts?.length ?? 0 }} 项</p></div>
    </v-card>
    <v-card class="panel span-4">
      <div class="panel-title"><h2>处置轴</h2></div>
      <div class="panel-body"><StatusBadge :state="shown.reviewState" /><p class="soft-note mt-3">最近更新 {{ formatTime(shown.updatedAt) }}</p></div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title">
        <h2>同一 GameRun 完成复核</h2>
        <span class="soft-note">{{ shown.runId ?? '当前没有可查询的 runId' }} · 人工门尝试 {{ humanRequiredAttemptCount }}</span>
      </div>
      <div class="panel-body">
        <CompletionLedger
          v-if="completionReviews.length || completionAdjudications.length || completionLedgerLoading || completionLedgerError || completionBlockers.length"
          :reviews="completionReviews"
          :adjudications="completionAdjudications"
          :blockers="completionBlockers"
        />
        <EmptyState v-else title="尚无完成复核记录" detail="当前运行尚未产生复核或裁定记录；验收状态以上方本次运行的完成合同为准，等待同次执行的证据。" />
      </div>
    </v-card>

    <v-card class="panel span-6">
      <div class="panel-title">
        <h2>每日 Todo</h2>
        <span class="soft-note">{{ dailySummary.requiredCompleted }}/{{ dailySummary.requiredTotal }} · 距重置 {{ resetCountdown(dailySummary.nextResetAt, now) }}</span>
      </div>
      <div class="panel-body">
        <v-progress-linear color="primary" rounded height="7" :model-value="dailySummary.progress.percent" class="mb-4" />
        <TodoChecklist :items="dailyTodos" :attempts="shown.todoAttempts ?? []" :assessments="automationAssessments.items.value" />
        <div class="risk-box mt-4">Todo 全部勾选只说明逐项状态；当前业务验收仍是 <strong>{{ shown.acceptanceState }}</strong>。</div>
      </div>
    </v-card>

    <v-card class="panel span-6">
      <div class="panel-title">
        <h2>每周 Todo</h2>
        <span class="soft-note">{{ weeklySummary.requiredCompleted }}/{{ weeklySummary.requiredTotal }} · {{ formatTime(weeklySummary.nextResetAt) }}</span>
      </div>
      <div class="panel-body">
        <v-progress-linear color="secondary" rounded height="7" :model-value="weeklySummary.progress.percent" class="mb-4" />
        <TodoChecklist :items="weeklyTodos" :attempts="shown.todoAttempts ?? []" :assessments="automationAssessments.items.value" />
      </div>
    </v-card>

    <v-card class="panel span-6">
      <div class="panel-title"><h2>窗口与租约</h2></div>
      <div class="panel-body">
        <JsonPanel title="WindowBinding" :value="shown.windowBinding" open />
        <JsonPanel title="ControllerLease" :value="shown.controllerLease" open />
      </div>
    </v-card>
    <v-card class="panel span-6">
      <div class="panel-title">
        <h2>执行历史与问题信号</h2>
        <span class="soft-note">已载入运行尝试 {{ attemptCounts.runAttemptCount ?? '尚未读取' }} · Todo 尝试 {{ attemptCounts.todoAttemptCount ?? '尚未读取' }} · 人工门尝试 {{ humanRequiredAttemptCount }}</span>
      </div>
      <div class="panel-body">
        <JsonPanel title="Checkpoints" :value="shown.checkpoints ?? []" open />
        <JsonPanel title="Game runs" :value="shown.attempts ?? []" />
        <JsonPanel title="Run attempts" :value="shown.runAttempts ?? []" />
        <JsonPanel title="Todo attempts" :value="shown.todoAttempts ?? []" />
        <JsonPanel v-if="shown.attemptAnalysis" title="Problem signals" :value="shown.attemptAnalysis.problemSignals" open />
        <p class="soft-note mt-3">反复尝试、阻塞和可重试失败只用于诊断；Todo 完成和 accepted_done 仍必须分别通过同一次执行的证据合同。</p>
      </div>
    </v-card>
  </div>
</template>

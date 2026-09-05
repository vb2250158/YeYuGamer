<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import type { AttemptAnalysis, DiagnosticBundle, LogEntry } from '../api/contracts'
import { managerApi } from '../api/client'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import JsonPanel from '../components/JsonPanel.vue'
import EmptyState from '../components/EmptyState.vue'
import { formatTime } from '../utils/format'
import { artifactContentPath } from '../utils/managerResources'
import { presentLogEntry } from '../utils/logPresentation'

const manager = useManagerStore()
const { events } = storeToRefs(manager)
// Diagnostics opens on the bounded ledger tail.  The legacy /logs default is
// intentionally forward-from-zero for API compatibility, so the UI must opt
// into the explicit recent cursor mode.
const resource = useResource<LogEntry>('/logs?recent=true&limit=250')
const bundles = useResource<DiagnosticBundle>('/diagnostic-bundles?limit=100')
const level = ref('all')
const query = ref('')
const busy = ref(false)
const bundleBusy = ref(false)
const diagnostics = ref<Record<string, unknown>>()
const diagnosticError = ref<string>()
const batchId = ref('')
const runId = ref('')
const incidentId = ref('')

const visibleLogs = computed(() => resource.items.value.filter((entry) => {
  const matchesLevel = level.value === 'all' || entry.level.toLowerCase() === level.value
  const haystack = `${entry.message} ${entry.source ?? ''} ${entry.entityType ?? ''} ${entry.eventType ?? ''} ${entry.phase ?? entry.stage ?? ''} ${entry.observedState ?? ''} ${entry.decision ?? ''} ${entry.reasonCode ?? ''} ${entry.reason ?? entry.detail ?? ''} ${entry.runId ?? ''}`.toLowerCase()
  return matchesLevel && haystack.includes(query.value.toLowerCase())
}))
const visibleLogRows = computed(() => visibleLogs.value.map((entry) => ({ entry, presentation: presentLogEntry(entry) })))
const attemptAnalysis = computed(() => diagnostics.value?.attemptAnalysis as AttemptAnalysis | undefined)

async function loadDiagnostics(): Promise<void> {
  busy.value = true
  diagnosticError.value = undefined
  try {
    diagnostics.value = await managerApi.get<Record<string, unknown>>('/diagnostics')
  } catch (caught) {
    diagnosticError.value = caught instanceof Error ? caught.message : String(caught)
  }
  busy.value = false
}

async function createBundle(): Promise<void> {
  const body: Record<string, unknown> = { requestedBy: 'webgui' }
  if (batchId.value.trim()) body.batchId = batchId.value.trim()
  if (runId.value.trim()) body.runId = runId.value.trim()
  if (incidentId.value.trim()) body.incidentId = incidentId.value.trim()
  bundleBusy.value = true
  const receipt = await manager.submitCommand('生成诊断包', 'POST', '/diagnostic-bundles', body)
  bundleBusy.value = false
  if (receipt) await bundles.load()
}
</script>

<template>
  <PageHeader
    eyebrow="LOGS / DIAGNOSTICS"
    title="日志与诊断"
    description="按游标查询结构化日志与事件；生成诊断包由 Manager 收集白名单内容，不让浏览器遍历本机路径。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value" @click="resource.load">刷新日志</v-btn>
    <v-btn color="primary" :loading="busy" @click="loadDiagnostics">读取诊断快照</v-btn>
  </PageHeader>

  <div class="content-grid">
    <v-card class="panel span-12">
      <div class="panel-title"><h2>Manager 诊断包</h2><span class="soft-note">{{ bundles.items.value.length }} 项</span></div>
      <div class="panel-body">
        <v-alert v-if="!manager.supports('/diagnostic-bundles', 'get')" type="warning" variant="tonal" class="mb-4">
          当前 Manager OpenAPI 没有 diagnostic-bundles 列表端点；空表不能解释为“尚无诊断包”。
        </v-alert>
        <div class="form-row diagnostic-scope">
          <v-text-field v-model="batchId" label="Batch ID（可选）" variant="outlined" density="compact" hide-details />
          <v-text-field v-model="runId" label="Run ID（可选）" variant="outlined" density="compact" hide-details />
          <v-text-field v-model="incidentId" label="Incident ID（可选）" variant="outlined" density="compact" hide-details />
          <v-btn color="primary" :loading="bundleBusy" :disabled="!manager.supports('/diagnostic-bundles', 'post')" @click="createBundle">生成诊断包</v-btn>
        </div>
        <p class="soft-note mt-3">留空表示生成 Manager 全局快照。浏览器只提交 opaque ID；文件收集范围和 artifact 路径由 Manager 决定。</p>
        <v-progress-linear v-if="bundles.loading.value" class="mt-4" indeterminate color="primary" />
        <v-alert v-if="bundles.error.value" class="mt-4" type="warning" variant="tonal">{{ bundles.error.value }}</v-alert>
        <div v-if="bundles.items.value.length" class="table-scroll mt-4">
          <table class="data-table">
            <thead><tr><th>诊断包</th><th>状态</th><th>范围</th><th>生成时间</th><th>内容</th></tr></thead>
            <tbody>
              <tr v-for="bundle in bundles.items.value" :key="bundle.resourceId">
                <td class="primary-cell"><strong class="mono">{{ bundle.resourceId }}</strong><small class="mono">{{ bundle.document.hash ?? 'hash pending' }}</small></td>
                <td>{{ bundle.state }}</td>
                <td>{{ bundle.document.runId ?? bundle.document.batchId ?? bundle.document.incidentId ?? '全局' }}</td>
                <td>{{ formatTime(bundle.createdAt) }}</td>
                <td>
                  <v-btn
                    v-if="bundle.document.artifactId && manager.supports('/artifacts/{artifactId}/content', 'get')"
                    size="small"
                    variant="tonal"
                    :href="artifactContentPath(bundle.document.artifactId)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >打开 Manager artifact</v-btn>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState v-else-if="!bundles.loading.value && manager.supports('/diagnostic-bundles', 'get')" title="尚无诊断包" detail="需要时由 Manager 生成；页面不会遍历本机目录。" />
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-body pt-5">
        <div class="form-row">
          <v-text-field v-model="query" label="搜索消息 / source / runId" variant="outlined" density="compact" hide-details clearable />
          <v-select v-model="level" :items="['all','debug','info','warning','error','critical']" label="级别" variant="outlined" density="compact" hide-details />
        </div>
      </div>
    </v-card>

    <v-card class="panel span-8">
      <div class="panel-title"><div><h2>结构化日志</h2><p class="soft-note log-contract-note">观察结果与判定分栏显示；旧日志缺字段时明确标为“未提供”，原始 message 只作诊断文本。</p></div><span class="soft-note">{{ visibleLogs.length }} / {{ resource.items.value.length }}</span></div>
      <ResourceState :loading="resource.loading.value" :error="resource.error.value" :empty="!visibleLogs.length" empty-title="没有匹配日志" @retry="resource.load">
        <div class="log-stream">
          <div v-for="({ entry, presentation }, index) in visibleLogRows" :key="entry.logId ?? `${entry.timestamp}-${index}`" class="semantic-log-row">
            <div class="semantic-log-meta">
              <span>{{ formatTime(entry.timestamp) }}</span>
              <span :class="`level-${entry.level.toLowerCase()}`">{{ entry.level }}</span>
              <span>{{ presentation.source }}</span>
              <span v-if="!presentation.structured" class="legacy-log-mark">字段不完整</span>
            </div>
            <dl class="semantic-log-facts">
              <div><dt>阶段</dt><dd>{{ presentation.phase }}</dd></div>
              <div><dt>实际观察</dt><dd>{{ presentation.observedState }}</dd></div>
              <div><dt>判定</dt><dd>{{ presentation.decision }}</dd></div>
              <div><dt>原因</dt><dd>{{ presentation.reason }}</dd></div>
            </dl>
            <details class="raw-log-message"><summary>原始诊断文本</summary><p>{{ presentation.rawMessage }}</p></details>
          </div>
        </div>
      </ResourceState>
    </v-card>

    <v-card class="panel span-4">
      <div class="panel-title"><h2>SSE 最近事件</h2><span class="soft-note">{{ events.length }}</span></div>
      <div class="event-list">
        <div v-for="event in events.slice(0, 30)" :key="event.eventId">
          <strong>{{ event.type }}</strong><small>{{ event.eventId }} · v{{ event.stateVersion ?? '—' }}</small>
        </div>
        <div v-if="!events.length" class="muted-copy">尚未收到实时事件；页面仍以当前 snapshot 为准。</div>
      </div>
      <div class="panel-body panel-divider pt-4">
        <v-alert v-if="diagnosticError" type="error" variant="tonal" class="mb-3">{{ diagnosticError }}</v-alert>
        <div v-if="attemptAnalysis" class="risk-box mb-3">
          Todo 尝试 {{ attemptAnalysis.summary.todoAttemptCount }} · 阻塞 {{ attemptAnalysis.summary.blockedTodoAttemptCount }}
          · 待复核 {{ attemptAnalysis.summary.reviewRequiredTodoAttemptCount }} · 可重试失败 {{ attemptAnalysis.summary.retryableFailureCount }}
        </div>
        <JsonPanel v-if="attemptAnalysis" title="Todo problem signals" :value="attemptAnalysis.problemSignals" open />
        <JsonPanel title="Manager diagnostics" :value="diagnostics ?? { status: '尚未读取' }" :open="!!diagnostics" />
      </div>
    </v-card>
  </div>
</template>

<style scoped>
.event-list{padding:0 18px 18px;max-height:560px;overflow:auto}.event-list>div{padding:10px 2px;border-bottom:1px solid var(--line)}.event-list strong,.event-list small{display:block}.event-list strong{font-size:.76rem}.event-list small{margin-top:4px;color:var(--muted);font:.64rem "Cascadia Code",monospace}
.log-contract-note{margin:5px 0 0}.semantic-log-row{display:grid;gap:10px;padding:13px 14px;border-bottom:1px solid rgba(142,190,230,.08)}.semantic-log-meta{display:flex;flex-wrap:wrap;gap:8px 14px;color:#91a9c3;font:.69rem "Cascadia Code",monospace}.legacy-log-mark{color:#ffc66d}.semantic-log-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:0}.semantic-log-facts>div{min-width:0;padding:8px 10px;border:1px solid rgba(142,190,230,.1);border-radius:8px;background:rgba(3,12,22,.28)}.semantic-log-facts dt{color:#708aa8;font-size:.64rem}.semantic-log-facts dd{margin:4px 0 0;color:#c9d9ea;font-size:.74rem;overflow-wrap:anywhere}.raw-log-message summary{color:#7998b7;cursor:pointer;font-size:.68rem}.raw-log-message p{margin:7px 0 0;color:#9eb2c9;font:.7rem/1.5 "Cascadia Code",monospace;overflow-wrap:anywhere}
.diagnostic-scope{grid-template-columns:repeat(3,minmax(0,1fr)) auto;align-items:center}
@media(max-width:1000px){.diagnostic-scope{grid-template-columns:1fr 1fr}}
@media(max-width:900px){.semantic-log-facts{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.diagnostic-scope,.semantic-log-facts{grid-template-columns:1fr}}
</style>

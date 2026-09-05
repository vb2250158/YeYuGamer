<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Incident, RepairSession, RepairVerificationVerdict } from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatTime } from '../utils/format'
import { parseOpaqueEvidenceIds } from '../utils/managerResources'

const manager = useManagerStore()
const resource = useResource<Incident>('/incidents')
const repairs = useResource<RepairSession>('/repair-sessions')
const busyId = ref<string>()
const verificationVerdicts = ref<Record<string, RepairVerificationVerdict>>({})
const verificationNotes = ref<Record<string, string>>({})
const evidenceInputs = ref<Record<string, string>>({})

watch(repairs.items, (items) => {
  for (const session of items) verificationVerdicts.value[session.resourceId] ??= 'needs_more_evidence'
}, { immediate: true })

function verificationReady(session: RepairSession): boolean {
  const verdict = verificationVerdicts.value[session.resourceId] ?? 'needs_more_evidence'
  const note = verificationNotes.value[session.resourceId]?.trim() ?? ''
  if (!note) return false
  return verdict !== 'passed' || parseOpaqueEvidenceIds(evidenceInputs.value[session.resourceId] ?? '').length > 0
}

async function refreshAll(): Promise<void> {
  await Promise.all([resource.load(), repairs.load()])
}

async function createRepair(incident: Incident): Promise<void> {
  busyId.value = incident.incidentId
  try {
    const receipt = await manager.submitCommand('创建维修会话', 'POST', `/incidents/${encodeURIComponent(incident.incidentId)}/repair-sessions`, {
      reason: 'operator_requested_from_webgui',
    })
    if (receipt) await refreshAll()
  } finally {
    busyId.value = undefined
  }
}

async function verifyRepair(session: RepairSession): Promise<void> {
  if (!verificationReady(session)) return
  busyId.value = session.resourceId
  try {
    const receipt = await manager.submitCommand('提交维修复验', 'POST', `/repair-sessions/${encodeURIComponent(session.resourceId)}/verification-requests`, {
      verdict: verificationVerdicts.value[session.resourceId] ?? 'needs_more_evidence',
      note: verificationNotes.value[session.resourceId]?.trim() ?? '',
      evidenceIds: parseOpaqueEvidenceIds(evidenceInputs.value[session.resourceId] ?? ''),
      requestedBy: 'webgui',
    })
    if (receipt) await repairs.load()
  } finally {
    busyId.value = undefined
  }
}
</script>

<template>
  <PageHeader
    eyebrow="INCIDENT / FINGERPRINT"
    title="Incident 与维修"
    description="把重复失败聚合成可追踪的指纹。维修动作仍需登记 capability，并经过 Replay、Shadow、验证与回滚门。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value || repairs.loading.value" @click="refreshAll">刷新</v-btn>
  </PageHeader>

  <ResourceState
    :loading="resource.loading.value"
    :error="resource.error.value"
    :empty="!resource.items.value.length"
    empty-title="当前没有 Incident"
    empty-detail="没有 Incident 只说明故障聚合队列为空，不代表业务验收完成。"
    @retry="resource.load"
  >
    <div class="content-grid">
      <v-card v-for="incident in resource.items.value" :key="incident.incidentId" class="panel span-6">
        <div class="panel-title">
          <div><h2>{{ incident.title ?? incident.fingerprint ?? incident.incidentId }}</h2><span class="soft-note mono">{{ incident.incidentId }}</span></div>
          <StatusBadge :state="incident.severity ?? incident.state" />
        </div>
        <div class="panel-body">
          <dl class="fact-grid">
            <div><dt>游戏</dt><dd>{{ incident.gameId ?? '全局' }}</dd></div>
            <div><dt>状态</dt><dd><StatusBadge :state="incident.state" small /></dd></div>
            <div><dt>出现次数</dt><dd>{{ incident.occurrenceCount ?? 1 }}</dd></div>
            <div><dt>重试资格</dt><dd>{{ incident.retryEligibility ?? '由策略决定' }}</dd></div>
            <div><dt>最后出现</dt><dd>{{ formatTime(incident.lastSeenAt) }}</dd></div>
          </dl>
          <div class="risk-box mt-4 mono">{{ incident.fingerprint ?? 'fingerprint pending' }}</div>
          <div class="action-row mt-4">
            <v-btn variant="tonal" color="warning" :loading="busyId === incident.incidentId" :disabled="!manager.supports('/incidents/{incidentId}/repair-sessions', 'post')" @click="createRepair(incident)">创建维修会话</v-btn>
            <span v-if="!manager.supports('/incidents/{incidentId}/repair-sessions', 'post')" class="soft-note">当前 Manager 版本仅开放 Incident 只读投影。</span>
          </div>
        </div>
      </v-card>
    </div>
  </ResourceState>

  <v-card class="panel mt-5">
    <div class="panel-title"><h2>维修会话账本</h2><span class="soft-note">{{ repairs.items.value.length }} 项</span></div>
    <v-alert v-if="!manager.supports('/repair-sessions', 'get')" type="warning" variant="tonal" class="mx-4 mb-4">
      当前 Manager OpenAPI 没有 repair-sessions 列表端点；页面不会把缺少合同误显示为“尚无维修会话”。
    </v-alert>
    <ResourceState
      :loading="repairs.loading.value"
      :error="repairs.error.value"
      :empty="manager.supports('/repair-sessions', 'get') && !repairs.items.value.length"
      empty-title="尚无维修会话"
      empty-detail="从 Incident 创建的隔离维修会话会在这里出现；建立会话本身不会修改生产 Adapter。"
      @retry="repairs.load"
    >
      <div class="repair-list">
        <section v-for="session in repairs.items.value" :key="session.resourceId" class="repair-session">
          <div class="panel-title repair-heading">
            <div><h2 class="mono">{{ session.resourceId }}</h2><span class="soft-note">Incident {{ session.document.incidentId }} · {{ session.document.gameId ?? '全局' }}</span></div>
            <StatusBadge :state="session.state" />
          </div>
          <div class="panel-body">
            <div class="form-row">
              <v-select
                v-model="verificationVerdicts[session.resourceId]"
                :items="[
                  { title: '需要更多证据', value: 'needs_more_evidence' },
                  { title: '复验通过', value: 'passed' },
                  { title: '复验失败', value: 'failed' },
                ]"
                label="复验结论"
                variant="outlined"
                density="compact"
                hide-details
              />
              <v-text-field
                v-model="evidenceInputs[session.resourceId]"
                label="Evidence ID（逗号或空格分隔）"
                variant="outlined"
                density="compact"
                hide-details
              />
            </div>
            <v-textarea v-model="verificationNotes[session.resourceId]" class="mt-3" label="复验说明" variant="outlined" rows="2" hide-details />
            <v-alert v-if="!verificationReady(session)" type="warning" variant="tonal" class="mt-3">
              所有复验都必须填写说明；选择“复验通过”时还必须引用至少一个 Manager 已登记的 Evidence ID。
            </v-alert>
            <div class="action-row mt-3">
              <v-btn
                color="primary"
                variant="tonal"
                :loading="busyId === session.resourceId"
                :disabled="!verificationReady(session) || !manager.supports('/repair-sessions/{repairSessionId}/verification-requests', 'post')"
                @click="verifyRepair(session)"
              >提交复验</v-btn>
              <span class="soft-note">最近结论：{{ session.document.lastVerdict ?? '尚未复验' }} · 创建于 {{ formatTime(session.createdAt) }}</span>
            </div>
            <p class="soft-note mt-3">复验只更新隔离 repair-session 账本；即使 verdict=passed，也不代表生产 Adapter 已晋级或游戏任务已完成。</p>
          </div>
        </section>
      </div>
    </ResourceState>
  </v-card>
</template>

<style scoped>
.fact-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; margin: 0; }
.fact-grid div { min-width: 0; }.fact-grid dt { color: var(--muted); font-size: .68rem; }.fact-grid dd { margin: 5px 0 0; color: #d6e2f0; font-size: .8rem; word-break: break-word; }
.repair-list{display:grid;gap:12px;padding:0 18px 18px}.repair-session{border:1px solid var(--line);border-radius:13px;background:rgba(8,22,37,.42)}.repair-heading{padding-bottom:14px}.repair-heading h2{font-size:.78rem}
</style>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AdapterDiagnosticCanary, AdapterInfo } from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import JsonPanel from '../components/JsonPanel.vue'

const manager = useManagerStore()
const resource = useResource<AdapterInfo>('/adapters')
const canaries = useResource<AdapterDiagnosticCanary>('/adapter-diagnostic-canaries?limit=100')
const busyKey = ref<string>()

const latestCanaries = computed(() => {
  const values = new Map<string, AdapterDiagnosticCanary>()
  for (const item of canaries.items.value) {
    if (!values.has(item.document.adapterId)) values.set(item.document.adapterId, item)
  }
  return values
})

function packageStatus(adapter: AdapterInfo): string {
  if (adapter.executionPackageStatus) return adapter.executionPackageStatus
  if (adapter.health === 'missing-entrypoint') return 'missing'
  if (adapter.health === 'unsafe-entrypoint') return 'unsafe-entrypoint'
  return adapter.executionReady ? 'installed' : 'unknown'
}

function hostIsHealthy(adapter: AdapterInfo): boolean {
  if (typeof adapter.hostHealthy === 'boolean') return adapter.hostHealthy
  return !String(adapter.health ?? '').startsWith('host-unhealthy')
}

function promotionRequestReady(adapter: AdapterInfo): boolean {
  return hostIsHealthy(adapter)
    && packageStatus(adapter) === 'installed-unpromoted'
    && !!versionRef(adapter, 'candidate')
    && !!adapter.implementationHash
}

function versionRef(adapter: AdapterInfo, mode: 'candidate' | 'active'): string | undefined {
  const direct = adapter[mode === 'candidate' ? 'candidateVersionId' : 'activeVersionId']
  return typeof direct === 'string' ? direct : mode === 'candidate' ? adapter.candidateVersion : adapter.activeVersion
}

function rollbackVersionRef(adapter: AdapterInfo): string | undefined {
  return adapter.rollbackVersionId ?? adapter.previousVerifiedVersionId
}

function adapterAvailability(adapter: AdapterInfo): string {
  if (!hostIsHealthy(adapter)) return 'Manager-owned Adapter Host 自检失败；所有执行与治理操作保持禁用。'
  if (packageStatus(adapter) === 'missing') return 'Manager-owned Host 健康，但本机 execution package 缺少固定 runner.exe 入口；不能据此执行游戏。'
  if (packageStatus(adapter) === 'unsafe-entrypoint') return 'Manager-owned Host 健康，但 execution package 入口未通过本机路径、文件类型或链接安全检查。'
  if (packageStatus(adapter) === 'installed-unpromoted') return '本机候选包已固定，但尚无 Manager 晋级收据；申请晋级时 Manager 会重新运行无进程 Canary 并核对测试证据。'
  if (adapter.executionReady === false || adapter.health === 'host-healthy-execution-disabled') return 'Manager-owned Host 健康，但全局执行门关闭或已有活动执行；execution readiness 为 false。'
  if (adapter.health && adapter.health !== 'healthy') return `Manager 报告 ${adapter.health}；请用无输入诊断 Canary 读取 Host 与 execution package 的分层结果。`
  return 'Host 与 execution package 状态均由 Manager 报告；可执行仍须满足最新 Canary、版本晋级和全局执行门。'
}

async function refreshAll(): Promise<void> {
  await Promise.all([resource.load(), canaries.load()])
}

async function diagnosticCanary(adapter: AdapterInfo): Promise<void> {
  busyKey.value = `${adapter.adapterId}:canary`
  try {
    const receipt = await manager.submitCommand(
      '运行 Adapter 诊断 Canary',
      'POST',
      `/adapters/${encodeURIComponent(adapter.adapterId)}/diagnostic-canary-requests`,
      {},
    )
    if (receipt) await refreshAll()
  } finally {
    busyKey.value = undefined
  }
}

async function request(adapter: AdapterInfo, action: 'promotion' | 'rollback'): Promise<void> {
  const version = action === 'promotion' ? versionRef(adapter, 'candidate') : rollbackVersionRef(adapter)
  if (!version) return
  busyKey.value = `${adapter.adapterId}:${action}`
  try {
    const receipt = await manager.submitCommand(
      action === 'promotion' ? '申请 Adapter 晋级' : '申请 Adapter 回滚',
      'POST',
      `/adapter-versions/${encodeURIComponent(version)}/${action}-requests`,
      {
        adapterId: adapter.adapterId,
        targetStage: action === 'promotion' ? 'promoted' : 'previous_verified',
        reason: action === 'promotion' ? 'latest_diagnostic_canary_ready' : 'explicit_previous_verified_version',
      },
    )
    if (receipt) await refreshAll()
  } finally {
    busyKey.value = undefined
  }
}
</script>

<template>
  <PageHeader
    eyebrow="ADAPTER / RELEASE GATES"
    title="Adapter 版本治理"
    description="查看工具哈希、能力数量与 Replay / Shadow / Canary 阶段。WebGUI 只能创建验证、晋级或回滚请求，不能上传后直接执行代码。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value || canaries.loading.value" @click="refreshAll">刷新</v-btn>
  </PageHeader>

  <ResourceState
    :loading="resource.loading.value"
    :error="resource.error.value"
    :empty="!resource.items.value.length"
    empty-title="没有安装 Adapter"
    empty-detail="Manager 尚未返回任何已哈希、已登记的 Adapter。"
    @retry="resource.load"
  >
    <v-alert v-if="!manager.supports('/adapter-diagnostic-canaries', 'get')" type="warning" variant="tonal" class="mb-4">
      当前 Manager OpenAPI 没有诊断 Canary 列表端点；卡片可显示不代表 Adapter Host Canary 已接入。
    </v-alert>
    <v-alert v-else-if="canaries.error.value" type="warning" variant="tonal" class="mb-4">
      当前 Manager 无法列出诊断 Canary：{{ canaries.error.value }}。页面不会把 Adapter 卡片可渲染误当成 Canary 已接入。
    </v-alert>
    <div class="content-grid">
      <v-card v-for="adapter in resource.items.value" :key="adapter.adapterId" class="panel span-6">
        <div class="panel-title">
          <div><h2>{{ adapter.displayName ?? adapter.adapterId }}</h2><span class="soft-note">{{ adapter.gameId ?? 'global' }}</span></div>
          <StatusBadge :state="adapter.health ?? adapter.stage" />
        </div>
        <div class="panel-body">
          <div class="version-track">
            <div><small>当前版本</small><strong>{{ adapter.activeVersion ?? '—' }}</strong><StatusBadge :state="adapter.stage ?? 'production'" small /></div>
            <span class="track-line">→</span>
            <div><small>候选版本</small><strong>{{ adapter.candidateVersion ?? '无候选' }}</strong><span class="soft-note">{{ adapter.capabilityCount ?? 0 }} capabilities</span></div>
          </div>
          <div class="status-layers mt-4">
            <div>
              <small>Manager-owned Host</small>
              <StatusBadge :state="hostIsHealthy(adapter) ? 'healthy' : 'failed'" small />
              <span class="soft-note">{{ adapter.hostId ?? '旧版 API 未返回 hostId' }} · {{ adapter.hostVersion ?? '版本未知' }}</span>
            </div>
            <div>
              <small>Execution package</small>
              <StatusBadge :state="adapter.executionReady ? 'ready' : packageStatus(adapter)" small />
              <span class="soft-note">executionReady={{ adapter.executionReady === true ? 'true' : 'false / 未返回' }}</span>
            </div>
          </div>
          <div class="risk-box mt-4 mono">{{ adapter.implementationHash ?? '当前兼容投影未提供 implementation hash' }}</div>
          <v-alert class="mt-4" :type="adapter.health === 'healthy' && adapter.executionReady ? 'info' : 'warning'" variant="tonal">{{ adapterAvailability(adapter) }}</v-alert>
          <div class="mt-4">
            <JsonPanel
              title="最新 Manager 诊断 Canary"
              :value="latestCanaries.get(adapter.adapterId)?.document ?? { canaryStatus: '尚未运行', diagnosticOnly: true, adapterProcessStarted: false, gameProcessStarted: false }"
              :open="!!latestCanaries.get(adapter.adapterId)"
            />
          </div>
          <div class="action-row mt-4">
            <v-btn variant="tonal" :loading="busyKey === `${adapter.adapterId}:canary`" :disabled="!manager.supports('/adapters/{adapterId}/diagnostic-canary-requests', 'post')" @click="diagnosticCanary(adapter)">运行诊断 Canary</v-btn>
            <v-btn color="warning" variant="tonal" :loading="busyKey === `${adapter.adapterId}:rollback`" :disabled="!rollbackVersionRef(adapter) || !manager.supports('/adapter-versions/{versionId}/rollback-requests', 'post')" @click="request(adapter, 'rollback')">申请回滚</v-btn>
            <v-btn color="primary" :loading="busyKey === `${adapter.adapterId}:promotion`" :disabled="!promotionRequestReady(adapter) || !manager.supports('/adapter-versions/{versionId}/promotion-requests', 'post')" @click="request(adapter, 'promotion')">申请晋级</v-btn>
          </div>
          <p class="soft-note mt-3">无输入诊断 Canary 只验证 Manager Host、固定入口、哈希与活动执行，明确不会启动 Adapter 或游戏进程。它的 executionReady=false 是诊断合同，不是晋级失败。申请晋级后，Manager 会在同一治理事务里运行新的无进程 Canary、核对候选测试证据并生成不可变收据；收据成立前仍禁止真实执行。</p>
        </div>
      </v-card>
    </div>
  </ResourceState>
</template>

<style scoped>
.version-track { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 16px; }.version-track > div { display:grid; gap:6px; padding:13px; border:1px solid var(--line); border-radius:12px; }.version-track small { color:var(--muted);font-size:.68rem}.version-track strong{font: .86rem "Cascadia Code",monospace}.track-line{color:#649fc7}
.status-layers { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }.status-layers>div{display:grid;gap:7px;padding:12px;border:1px solid var(--line);border-radius:12px}.status-layers small{color:var(--muted);font-size:.68rem}@media(max-width:700px){.status-layers{grid-template-columns:1fr}}
</style>

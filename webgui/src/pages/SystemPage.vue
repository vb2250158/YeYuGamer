<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { managerApi } from '../api/client'
import type { CapabilityDefinition, HealthStatus, ManagerIdentity, NotificationDelivery, PageResult } from '../api/contracts'
import { extractItems } from '../api/contracts'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import JsonPanel from '../components/JsonPanel.vue'
import { formatTime } from '../utils/format'

const manager = useManagerStore()
const { snapshot, connectionState } = storeToRefs(manager)
const meta = ref<ManagerIdentity>()
const health = ref<HealthStatus>()
const capabilities = ref<CapabilityDefinition[]>([])
const notifications = ref<NotificationDelivery[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref<string>()
const notificationFailures = computed(() => notifications.value.filter((item) => item.state === 'failed').length)
const notificationPending = computed(() => notifications.value.filter((item) => ['draft', 'sending'].includes(item.state)).length)

async function load(): Promise<void> {
  loading.value = true
  error.value = undefined
  try {
    const [nextMeta, nextHealth, nextCapabilities, nextNotifications] = await Promise.all([
      managerApi.get<ManagerIdentity>('/meta'),
      managerApi.get<HealthStatus>('/health'),
      managerApi.get<CapabilityDefinition[] | PageResult<CapabilityDefinition>>('/capabilities'),
      managerApi.get<PageResult<NotificationDelivery>>('/notifications?limit=100'),
    ])
    meta.value = nextMeta
    health.value = nextHealth
    capabilities.value = extractItems(nextCapabilities)
    notifications.value = extractItems(nextNotifications)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    loading.value = false
  }
}

async function lifecycle(action: 'stop' | 'restart'): Promise<void> {
  busy.value = true
  await manager.submitCommand(
    action === 'stop' ? '安全停止 Manager' : '重启 Manager',
    'POST',
    `/manager/${action}-requests`,
    { reason: 'operator_request_from_webgui', requestedBy: 'webgui' },
  )
  busy.value = false
}

onMounted(load)
</script>

<template>
  <PageHeader
    eyebrow="SYSTEM / CONTRACT"
    title="系统状态"
    description="检查 Manager、存储、事件流、前后端合同与能力目录。停止和重启请求会由 Manager 判断能否安全收口。"
  >
    <v-btn variant="tonal" :loading="loading" @click="load">刷新系统</v-btn>
  </PageHeader>

  <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
  <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />

  <div class="content-grid">
    <v-card class="panel metric-card span-3"><span>Manager</span><strong class="system-value">{{ meta?.version ?? snapshot.manager?.version ?? '—' }}</strong><small>{{ meta?.apiVersion ?? 'API 未知' }}</small></v-card>
    <v-card class="panel metric-card span-3"><span>连接</span><strong class="system-value"><StatusBadge :state="connectionState" /></strong><small>snapshot + SSE</small></v-card>
    <v-card class="panel metric-card span-3"><span>存储</span><strong class="system-value"><StatusBadge :state="health?.storage ?? 'unknown'" /></strong><small>{{ health?.checkedAt ? formatTime(health.checkedAt) : '尚未检查' }}</small></v-card>
    <v-card class="panel metric-card span-3"><span>能力目录</span><strong>{{ capabilities.length }}</strong><small>{{ capabilities.filter((item) => item.enabled !== false).length }} 已启用</small></v-card>

    <v-card class="panel span-7">
      <div class="panel-title"><h2>组件健康</h2><StatusBadge :state="health?.status ?? connectionState" /></div>
      <div class="panel-body">
        <JsonPanel title="Manager meta" :value="meta ?? snapshot.manager" open />
        <JsonPanel title="Health components" :value="health ?? snapshot.health" open />
      </div>
    </v-card>

    <v-card class="panel span-5">
      <div class="panel-title"><h2>生命周期</h2></div>
      <div class="panel-body">
        <div class="risk-box">活动 BatchRun 存在时，Manager 可以拒绝停止/重启，或先进入安全收口。WebGUI 不会强杀进程。</div>
        <div class="action-row mt-4">
          <v-btn color="warning" variant="tonal" :loading="busy" :disabled="!manager.supports('/manager/restart-requests', 'post')" @click="lifecycle('restart')">请求重启</v-btn>
          <v-btn color="error" variant="tonal" :loading="busy" :disabled="!manager.supports('/manager/stop-requests', 'post')" @click="lifecycle('stop')">请求安全停止</v-btn>
        </div>
      </div>
    </v-card>

    <v-card class="panel span-6">
      <div class="panel-title"><h2>Todo 健康</h2><StatusBadge :state="snapshot.todo ? 'healthy' : 'unknown'" /></div>
      <div class="panel-body">
        <div class="form-row">
          <div class="primary-cell"><small>required</small><strong>{{ snapshot.todo?.requiredCompleted ?? 0 }}/{{ snapshot.todo?.requiredTotal ?? 0 }}</strong></div>
          <div class="primary-cell"><small>unresolved</small><strong>{{ snapshot.todo?.requiredRemaining ?? 0 }}</strong></div>
          <div class="primary-cell"><small>blocked</small><strong>{{ snapshot.todo?.blocked ?? 0 }}</strong></div>
          <div class="primary-cell"><small>review</small><strong>{{ snapshot.todo?.reviewRequired ?? 0 }}</strong></div>
        </div>
        <p class="soft-note mt-4">Todo 状态来自 Manager SQLite；健康只说明状态可读，不代表任何游戏已通过业务验收。</p>
      </div>
    </v-card>

    <v-card class="panel span-6">
      <div class="panel-title"><h2>批次通知</h2><StatusBadge :state="notificationFailures ? 'failed' : notificationPending ? 'pending' : notifications.length ? 'healthy' : 'not_started'" /></div>
      <div class="panel-body">
        <p class="soft-note">{{ notifications.length }} 条 · 待投递 {{ notificationPending }} · 失败 {{ notificationFailures }}。系统页只显示 typed delivery 摘要，发送、重试与预览集中在通知页。</p>
        <div v-if="notifications.length" class="notification-list">
          <article v-for="item in notifications.slice(0, 8)" :key="item.notificationId">
            <div><strong>{{ item.subject }}</strong><small>{{ item.outcome }} · {{ item.dispatchGate }} · {{ formatTime(item.updatedAt) }}</small></div>
            <StatusBadge :state="item.state" small />
          </article>
        </div>
        <p v-else class="muted-copy">Manager 尚未登记 sealed batch 通知。</p>
        <v-btn class="mt-3" variant="tonal" color="primary" to="/notifications">打开通知中心</v-btn>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>Capability Registry</h2><span class="soft-note">无 shell / click / 任意路径能力</span></div>
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Capability</th><th>版本</th><th>风险</th><th>状态</th><th>实现哈希</th></tr></thead>
          <tbody>
            <tr v-for="capability in capabilities" :key="`${capability.capabilityId}@${capability.version}`">
              <td class="primary-cell"><strong>{{ capability.displayName ?? capability.capabilityId }}</strong><small>{{ capability.capabilityId }}</small></td>
              <td>{{ capability.version }}</td><td><StatusBadge :state="capability.risk" /></td><td><StatusBadge :state="capability.enabled === false ? 'disabled' : 'enabled'" /></td><td class="mono">{{ capability.implementationHash ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="!capabilities.length" class="muted-copy pa-5">Manager 尚未返回能力目录。</div>
      </div>
    </v-card>
  </div>
</template>

<style scoped>
.system-value{margin-top:18px!important;font-family:inherit!important;font-size:1.1rem!important}
.notification-list { display: grid; gap: 8px; margin-top: 13px; }
.notification-list article { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: rgba(7,20,34,.4); }
.notification-list strong,.notification-list small { display: block; }.notification-list strong { font-size: .76rem; }.notification-list small { margin-top: 3px; color: var(--muted); font-size: .66rem; }
</style>

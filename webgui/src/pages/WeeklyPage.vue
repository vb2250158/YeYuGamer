<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CapabilityDefinition, WeeklyTask } from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatTime } from '../utils/format'
import { enabledCapability, executionIsEnabled } from '../utils/managerResources'

const manager = useManagerStore()
const resource = useResource<WeeklyTask>('/weekly')
const capabilities = useResource<CapabilityDefinition>('/capabilities')
const busyKey = ref<string>()
const planCapability = computed(() => enabledCapability(capabilities.items.value, 'game.weekly.plan'))
const runCapability = computed(() => enabledCapability(capabilities.items.value, 'game.weekly.run'))
const executionEnabled = computed(() => executionIsEnabled(manager.snapshot))
const executionInProgress = computed(() => manager.executionInProgress)
const executionBusyReason = computed(() => manager.executionBusyReason)

async function refreshAll(): Promise<void> {
  await Promise.all([resource.load(), capabilities.load()])
}

async function submit(task: WeeklyTask, mode: 'plan' | 'run'): Promise<void> {
  if (mode === 'run' && executionInProgress.value) return
  const capability = mode === 'plan' ? planCapability.value : runCapability.value
  if (!capability) return
  busyKey.value = `${task.weeklyId}:${mode}`
  await manager.submitCommand(mode === 'plan' ? '规划周常' : '申请运行周常能力', 'POST', '/capability-invocations', {
    capability: capability.capabilityId,
    arguments: { weeklyId: task.weeklyId, gameId: task.gameId },
    requestedBy: 'webgui',
  })
  busyKey.value = undefined
  await resource.load()
}
</script>

<template>
  <PageHeader
    eyebrow="WEEKLY / RESET WINDOW"
    title="周常任务"
    description="周常有独立的周期、运行记录与完成证据，只能调用 Manager 已登记的周常 capability。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value || capabilities.loading.value" @click="refreshAll">刷新</v-btn>
  </PageHeader>

  <v-alert v-if="capabilities.error.value" type="warning" variant="tonal" class="mb-5">
    能力目录读取失败：{{ capabilities.error.value }}。页面无法确认能力状态，规划与运行按钮都会保持禁用。
  </v-alert>
  <v-alert v-else-if="!capabilities.loading.value && (!runCapability || !executionEnabled)" type="info" variant="tonal" class="mb-5">
    周常计划能力可独立使用。实际运行能力{{ !runCapability ? '尚未由 Manager 启用' : '受全局执行开关限制' }}，运行按钮会保持禁用。
  </v-alert>
  <v-alert v-if="executionInProgress" type="warning" variant="tonal" class="mb-5">
    Manager 当前执行占用：{{ executionBusyReason }} 周常“申请运行”已禁用。
  </v-alert>

  <ResourceState
    :loading="resource.loading.value"
    :error="resource.error.value"
    :empty="!resource.items.value.length"
    empty-title="没有登记周常"
    empty-detail="请先在 Manager 配置中登记周常任务和对应 capability。"
    @retry="resource.load"
  >
    <div class="content-grid">
      <v-card v-for="task in resource.items.value" :key="task.weeklyId" class="panel span-6">
        <div class="panel-title">
          <div><h2>{{ task.displayName }}</h2><span class="soft-note mono">{{ task.weeklyId }}</span></div>
          <StatusBadge :state="task.state ?? 'not_started'" />
        </div>
        <div class="panel-body">
          <div class="form-row">
            <div class="primary-cell"><small>游戏</small><strong>{{ task.gameId ?? '—' }}</strong></div>
            <div class="primary-cell"><small>验收</small><StatusBadge :state="task.acceptanceState ?? 'unknown'" small /></div>
            <div class="primary-cell"><small>上次运行</small><strong>{{ formatTime(task.lastRunAt) }}</strong></div>
            <div class="primary-cell"><small>下次重置</small><strong>{{ formatTime(task.nextResetAt) }}</strong></div>
          </div>
          <div class="risk-box mt-4 mono">
            计划：{{ planCapability ? `${planCapability.capabilityId}@${planCapability.version}` : 'Manager 未开放' }}<br />
            执行：{{ runCapability ? `${runCapability.capabilityId}@${runCapability.version}` : 'Manager 未开放' }}
          </div>
          <div class="action-row mt-4">
            <v-btn
              variant="tonal"
              :loading="busyKey === `${task.weeklyId}:plan`"
              :disabled="!task.enabled || !task.gameId || !planCapability || !manager.supports('/capability-invocations', 'post')"
              @click="submit(task, 'plan')"
            >安全规划</v-btn>
            <v-btn
              color="primary"
              :loading="busyKey === `${task.weeklyId}:run`"
              :disabled="executionInProgress || !task.enabled || !task.gameId || !runCapability || !executionEnabled || !manager.supports('/capability-invocations', 'post')"
              @click="submit(task, 'run')"
            >申请运行</v-btn>
          </div>
        </div>
      </v-card>
    </div>
  </ResourceState>
</template>

<style scoped>
.primary-cell small,.primary-cell strong{display:block}.primary-cell small{color:var(--muted);font-size:.68rem}.primary-cell strong{margin-top:5px;font-size:.8rem}
</style>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { managerApi } from '../api/client'
import type { ConfigDocument, JsonObject, PageResult, PolicyDocument, TodoCadence, TodoDefinition, TodoInstance, TodoResetOverride, TodoResetPolicy, TodoResetPreview } from '../api/contracts'
import { extractItems } from '../api/contracts'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import JsonPanel from '../components/JsonPanel.vue'
import { editableConfigPatch } from '../utils/managerResources'
import { resetRuleLabel, resetRulePresentation } from '../utils/todos'
import StatusBadge from '../components/StatusBadge.vue'

const manager = useManagerStore()
const config = ref<ConfigDocument>()
const policy = ref<PolicyDocument>()
const todoDefinitions = ref<TodoDefinition[]>([])
const todoInstances = ref<TodoInstance[]>([])
const todoPreview = ref<TodoResetPreview>()
const editor = ref('{}')
const loading = ref(false)
const busy = ref(false)
const error = ref<string>()
const validation = ref<string>()
const todoError = ref<string>()
const todoAction = ref<'reconcile' | 'reset'>('reconcile')
const todoCadence = ref<TodoCadence>('daily')
const todoGameId = ref('')
const resetPolicy = ref<TodoResetPolicy>({
  timezone: 'Asia/Shanghai', time: '04:00', weekStartDay: 'Monday', perGame: {}, perDefinition: {},
})
const resetOverrideGameId = ref('')
const overrideTimezone = ref('')
const overrideTime = ref('')
const overrideWeekStartDay = ref('')

const gameOptions = computed(() => [
  { title: '全部游戏', value: '' },
  ...manager.snapshot.games.map((game) => ({ title: game.displayName, value: game.gameId })),
])
const rulesByGame = computed(() => manager.snapshot.games.map((game) => {
  const definitions = todoDefinitions.value.filter((item) => item.gameId === game.gameId && item.cadence === todoCadence.value)
  const instances = todoInstances.value.filter((item) => item.gameId === game.gameId && item.cadence === todoCadence.value)
  const override = resetPolicy.value.perGame[game.gameId]
  const configurationBaseline = {
    timezone: override?.timezone || resetPolicy.value.timezone,
    time: override?.time || resetPolicy.value.time,
    cadence: todoCadence.value,
    ...(todoCadence.value === 'weekly' ? { weekStartDay: override?.weekStartDay || resetPolicy.value.weekStartDay } : {}),
  }
  const presentation = resetRulePresentation(instances, definitions, configurationBaseline)
  return { game, definitions, presentation }
}))
watch([todoAction, todoCadence, todoGameId], () => { todoPreview.value = undefined })
watch(resetOverrideGameId, (gameId) => {
  const override = resetPolicy.value.perGame[gameId] ?? {}
  overrideTimezone.value = override.timezone ?? ''
  overrideTime.value = override.time ?? ''
  overrideWeekStartDay.value = override.weekStartDay ?? ''
})

function objectValue(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}

function resetOverride(value: unknown): TodoResetOverride {
  const record = objectValue(value)
  return {
    ...(typeof record.timezone === 'string' ? { timezone: record.timezone } : {}),
    ...(typeof record.time === 'string' ? { time: record.time } : {}),
    ...(typeof (record.weekStartDay ?? record.week_start_day) === 'string'
      ? { weekStartDay: (record.weekStartDay ?? record.week_start_day) as string }
      : {}),
  }
}

function resetOverrides(value: unknown): Record<string, TodoResetOverride> {
  return Object.fromEntries(Object.entries(objectValue(value)).map(([key, override]) => [key, resetOverride(override)]))
}

function readResetPolicy(values: JsonObject): TodoResetPolicy {
  const record = objectValue(values.todoResetPolicy ?? values.todo_reset_policy)
  return {
    timezone: typeof record.timezone === 'string' ? record.timezone : 'Asia/Shanghai',
    time: typeof record.time === 'string' ? record.time : '04:00',
    weekStartDay: typeof (record.weekStartDay ?? record.week_start_day) === 'string'
      ? (record.weekStartDay ?? record.week_start_day) as string
      : 'Monday',
    perGame: resetOverrides(record.perGame ?? record.per_game),
    perDefinition: resetOverrides(record.perDefinition ?? record.per_definition),
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = undefined
  try {
    const [nextConfig, nextPolicy, nextTodoDefinitions, nextTodoInstances] = await Promise.all([
      managerApi.get<ConfigDocument>('/config'),
      managerApi.get<PolicyDocument>('/policy'),
      managerApi.get<PageResult<TodoDefinition>>('/todo-definitions'),
      managerApi.get<PageResult<TodoInstance>>('/todo-instances?current=true&limit=5000'),
    ])
    config.value = nextConfig
    policy.value = nextPolicy
    todoDefinitions.value = extractItems(nextTodoDefinitions)
    todoInstances.value = extractItems(nextTodoInstances)
    resetPolicy.value = readResetPolicy(nextConfig.config ?? {})
    editor.value = JSON.stringify(editableConfigPatch(nextConfig.config ?? {}), null, 2)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    loading.value = false
  }
}

function applyGameOverride(): void {
  if (!resetOverrideGameId.value) return
  const override: TodoResetOverride = {
    ...(overrideTimezone.value ? { timezone: overrideTimezone.value } : {}),
    ...(overrideTime.value ? { time: overrideTime.value } : {}),
    ...(overrideWeekStartDay.value ? { weekStartDay: overrideWeekStartDay.value } : {}),
  }
  const perGame = { ...resetPolicy.value.perGame }
  if (Object.keys(override).length) perGame[resetOverrideGameId.value] = override
  else delete perGame[resetOverrideGameId.value]
  resetPolicy.value = { ...resetPolicy.value, perGame }
  validation.value = `${resetOverrideGameId.value} 的 reset override 已放入本页草稿，尚未提交 Manager。`
}

function clearGameOverride(): void {
  overrideTimezone.value = ''
  overrideTime.value = ''
  overrideWeekStartDay.value = ''
  applyGameOverride()
}

async function saveResetPolicy(): Promise<void> {
  busy.value = true
  try {
    const receipt = await manager.submitCommand('保存 Todo 重置策略', 'PATCH', '/config', {
      todoResetPolicy: resetPolicy.value,
    })
    if (!receipt) return
    await Promise.all([manager.refresh({ quiet: true }), load()])
    validation.value = 'Manager 已受理 reset policy，并已重新读取服务端配置；当前周期实例不会由前端乐观改写。'
  } finally {
    busy.value = false
  }
}

async function previewTodoCommand(): Promise<void> {
  todoError.value = undefined
  todoPreview.value = undefined
  try {
    const query = new URLSearchParams({ action: todoAction.value, cadence: todoCadence.value })
    if (todoGameId.value) query.append('gameId', todoGameId.value)
    todoPreview.value = await managerApi.get<TodoResetPreview>(`/todo-reset-preview?${query}`)
  } catch (caught) {
    todoError.value = caught instanceof Error ? caught.message : String(caught)
  }
}

async function submitTodoCommand(): Promise<void> {
  if (!todoPreview.value || todoPreview.value.action !== todoAction.value) return
  busy.value = true
  try {
    const path = todoAction.value === 'reset' ? '/todo-reset-requests' : '/todo-reconcile-requests'
    const receipt = await manager.submitCommand(
      todoAction.value === 'reset' ? '显式重置 Todo 周期' : '显式对账 Todo 周期',
      'POST',
      path,
      {
        ...(todoGameId.value ? { gameIds: [todoGameId.value] } : {}),
        cadence: todoCadence.value,
        reason: 'operator_request_from_webgui_after_preview',
        requestedBy: 'webgui',
      },
      { expectedStateVersion: todoPreview.value.stateVersion },
    )
    if (receipt) {
      await Promise.all([manager.refresh({ quiet: true }), load()])
      await previewTodoCommand()
    }
  } finally {
    busy.value = false
  }
}

function parseEditor(): Record<string, unknown> | undefined {
  try {
    const value = JSON.parse(editor.value) as unknown
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('配置根必须是 JSON object')
    const properties = config.value?.schema?.properties
    const allowed = properties && typeof properties === 'object' && !Array.isArray(properties)
      ? new Set(Object.keys(properties))
      : undefined
    const unknownKeys = allowed ? Object.keys(value).filter((key) => !allowed.has(key)) : []
    if (unknownKeys.length) throw new Error(`配置含不可写字段：${unknownKeys.join('、')}`)
    validation.value = 'JSON 语法有效；服务端 schema 与策略仍需 Manager 验证。'
    return value as Record<string, unknown>
  } catch (caught) {
    validation.value = caught instanceof Error ? caught.message : String(caught)
    return undefined
  }
}

async function save(): Promise<void> {
  const next = parseEditor()
  if (!next) return
  busy.value = true
  const receipt = await manager.submitCommand('保存配置', 'PATCH', '/config', next)
  busy.value = false
  if (!receipt) {
    validation.value = '配置提交失败；请查看“命令与错误”，重新读取最新版本后再处理。'
    return
  }
  await Promise.all([manager.refresh({ quiet: true }), load()])
  validation.value = 'Manager 已记录配置命令，并已从服务端重新读取当前配置。'
}

onMounted(load)
</script>

<template>
  <PageHeader
    eyebrow="CONFIG / POLICY / CAS"
    title="设置与策略"
    description="配置保存使用版本比较与 Manager schema 验证；策略和永久禁止动作只读展示，不能由普通页面绕开。"
  >
    <v-btn variant="tonal" :loading="loading" @click="load">重新读取</v-btn>
    <v-btn variant="tonal" @click="parseEditor">校验 JSON</v-btn>
    <v-btn color="primary" :loading="busy" :disabled="!config || !!error || !manager.supports('/config', 'patch')" @click="save">提交保存</v-btn>
  </PageHeader>

  <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
  <v-alert v-if="validation" type="info" variant="tonal" class="mb-4">{{ validation }}</v-alert>
  <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />

  <div class="content-grid">
    <v-card class="panel span-7">
      <div class="panel-title"><h2>配置草稿</h2><span class="soft-note">base {{ config?.version ?? 'unknown' }}</span></div>
      <div class="panel-body">
        <textarea v-model="editor" class="code-editor" spellcheck="false" aria-label="配置 JSON" />
        <p class="soft-note mt-3">提交后不做乐观更新。若 stateVersion 或 baseVersion 已变化，Manager 应返回冲突并要求重新读取。</p>
      </div>
    </v-card>

    <v-card class="panel span-5">
      <div class="panel-title"><h2>生效策略</h2><span class="soft-note">{{ policy?.version ?? '—' }}</span></div>
      <div class="panel-body">
        <div class="risk-box">
          <strong>永久禁止类别</strong><br />{{ policy?.forbiddenClasses?.join(' · ') || '等待 Manager policy' }}
        </div>
        <JsonPanel title="Policy" :value="policy?.policy ?? {}" open />
        <JsonPanel title="Config schema" :value="config?.schema ?? {}" />
        <JsonPanel title="Manager 当前完整配置（只读）" :value="config?.config ?? {}" />
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>Todo 重置规则</h2><span class="soft-note">默认 {{ resetRuleLabel(manager.snapshot.todo?.defaultResetRule) }}</span></div>
      <div class="panel-body">
        <v-alert type="info" variant="tonal" class="mb-4">保存 reset policy 只修改 Manager 配置。当前周期实例与已完成状态不会由 WebGUI 乐观改写；先 preview，再显式 reconcile/reset 才会生成相应命令。</v-alert>
        <div class="todo-command-grid mb-4">
          <v-text-field v-model="resetPolicy.timezone" label="默认时区" variant="outlined" />
          <v-text-field v-model="resetPolicy.time" label="默认重置时间" placeholder="04:00" variant="outlined" />
          <v-select v-model="resetPolicy.weekStartDay" :items="['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']" label="每周起点" variant="outlined" />
          <v-btn color="primary" variant="tonal" :loading="busy" :disabled="!config || !manager.supports('/config', 'patch')" @click="saveResetPolicy">保存 reset policy</v-btn>
        </div>
        <div class="todo-command-grid override-command-grid mb-4">
          <v-select v-model="resetOverrideGameId" :items="gameOptions.slice(1)" label="按游戏覆盖" variant="outlined" />
          <v-text-field v-model="overrideTimezone" label="覆盖时区（留空继承）" variant="outlined" />
          <v-text-field v-model="overrideTime" label="覆盖时间（留空继承）" placeholder="04:00" variant="outlined" />
          <v-select v-model="overrideWeekStartDay" :items="['','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']" label="覆盖每周起点" variant="outlined" />
          <div class="action-row command-buttons"><v-btn variant="tonal" :disabled="!resetOverrideGameId" @click="applyGameOverride">写入草稿</v-btn><v-btn variant="text" :disabled="!resetOverrideGameId" @click="clearGameOverride">清除覆盖</v-btn></div>
        </div>
        <div class="reset-rule-grid">
          <article v-for="entry in rulesByGame" :key="entry.game.gameId" class="reset-rule-card">
            <div><strong>{{ entry.game.displayName }}</strong><small>{{ entry.game.gameId }} · {{ entry.definitions.length }} 项 {{ todoCadence }}</small></div>
            <span v-if="entry.presentation.mode === 'frozen'">当前周期冻结 {{ entry.presentation.rules.map(resetRuleLabel).join(' / ') }}</span>
            <span v-else-if="entry.definitions.length">配置基线 {{ entry.presentation.rules.map(resetRuleLabel).join(' / ') }}<template v-if="entry.presentation.definitionRules.length"> · 定义例外 {{ entry.presentation.definitionRules.map(resetRuleLabel).join(' / ') }}</template> · 最终规则以 preview 为准</span>
            <span v-else>没有该周期定义</span>
          </article>
        </div>
      </div>
    </v-card>

    <v-card class="panel span-12">
      <div class="panel-title"><h2>Todo 周期命令</h2><StatusBadge :state="todoPreview ? 'ready' : 'not_started'" /></div>
      <div class="panel-body">
        <v-alert v-if="todoError" type="error" variant="tonal" class="mb-4">{{ todoError }}</v-alert>
        <div class="todo-command-grid">
          <v-select v-model="todoAction" :items="[{ title: '对账缺失实例', value: 'reconcile' }, { title: '显式重置周期', value: 'reset' }]" label="动作" variant="outlined" />
          <v-select v-model="todoCadence" :items="[{ title: '每日', value: 'daily' }, { title: '每周', value: 'weekly' }]" label="周期" variant="outlined" />
          <v-select v-model="todoGameId" :items="gameOptions" label="游戏范围" variant="outlined" />
          <div class="action-row command-buttons">
            <v-btn variant="tonal" :disabled="!manager.supports('/todo-reset-preview', 'get')" @click="previewTodoCommand">先预览</v-btn>
            <v-btn color="warning" variant="tonal" :loading="busy" :disabled="!todoPreview || todoPreview.action !== todoAction || !manager.supports(todoAction === 'reset' ? '/todo-reset-requests' : '/todo-reconcile-requests', 'post')" @click="submitTodoCommand">提交显式命令</v-btn>
          </div>
        </div>
        <div v-if="todoPreview" class="preview-summary mt-3">
          <span>定义 {{ todoPreview.definitionCount }}</span>
          <span>当前已存在 {{ todoPreview.existingCount }}</span>
          <span>将新建 {{ todoPreview.wouldCreateCount }}</span>
          <span>下周期生效 {{ todoPreview.items.filter((item) => item.policyChangeDeferred).length }}</span>
          <span>基于 stateVersion {{ todoPreview.stateVersion }}</span>
        </div>
        <JsonPanel v-if="todoPreview" title="Manager reset/reconcile preview" :value="todoPreview" open />
      </div>
    </v-card>
  </div>
</template>

<style scoped>
.reset-rule-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; }
.reset-rule-card { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 12px; border: 1px solid var(--line); border-radius: 11px; background: rgba(7,20,34,.42); }
.reset-rule-card strong,.reset-rule-card small { display: block; }.reset-rule-card small,.reset-rule-card > span { margin-top: 3px; color: var(--muted); font-size: .7rem; }
.todo-command-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)) auto; gap: 12px; align-items: start; }
.override-command-grid { grid-template-columns: repeat(4,minmax(0,1fr)) auto; }
.command-buttons { padding-top: 3px; }
.preview-summary { display: flex; flex-wrap: wrap; gap: 9px 18px; margin-bottom: 12px; color: #9db4ce; font-size: .75rem; }
@media (max-width: 900px) { .reset-rule-grid,.todo-command-grid { grid-template-columns: 1fr; } }
</style>

<script setup lang="ts">
import { computed } from 'vue'
import type { AutomationAssessment, TodoAttempt, TodoInstance } from '../api/contracts'
import StatusBadge from './StatusBadge.vue'
import {
  completedTodo,
  dispatchDispositionLabel,
  latestAutomationAssessment,
  resetRuleLabel,
  schedulableTodo,
} from '../utils/todos'
import { formatTime, humanize } from '../utils/format'

const props = withDefaults(defineProps<{
  items: TodoInstance[]
  attempts?: TodoAttempt[]
  assessments?: AutomationAssessment[]
  compact?: boolean
  showGame?: boolean
  productMode?: boolean
}>(), {
  attempts: () => [],
  assessments: () => [],
  compact: false,
  showGame: false,
  productMode: false,
})

const assessmentsByTodo = computed(() => new Map(props.items.map((item) => [
  item.todoInstanceId,
  latestAutomationAssessment(item.todoInstanceId, props.assessments),
])))

function attemptsFor(todoInstanceId: string): TodoAttempt[] {
  return props.attempts
    .filter((attempt) => attempt.todoInstanceId === todoInstanceId)
    .sort((left, right) => right.attemptNumber - left.attemptNumber)
}

function assessmentFor(todoInstanceId: string): AutomationAssessment | undefined {
  return assessmentsByTodo.value.get(todoInstanceId)
}

function availableActions(item: TodoInstance): string[] {
  const availability = item.actionAvailability
  if (!availability) return []
  return [
    availability.execute ? '执行' : '',
    availability.resume ? '恢复' : '',
    availability.review ? '复核' : '',
    availability.releaseHuman ? '释放人工接管' : '',
    availability.submitManualEvidence ? '提交人工证据' : '',
  ].filter(Boolean)
}

function productMessage(item: TodoInstance): string {
  if (item.status === 'completed') return '这一步已经完成。'
  if (item.status === 'in_progress') return '正在自动处理这一步。'
  if (item.status === 'review_required') return '需要确认这一步的完成截图。'
  if (item.status === 'human_required') return '需要你按画面提示完成登录或确认。'
  if (item.status === 'blocked') return '这一步遇到问题，请按页面顶部提示处理。'
  if (item.status === 'skipped') return '这一步本次没有执行。'
  return '等待本次每日执行。'
}
</script>

<template>
  <div v-if="items.length" :class="['todo-checklist', { compact }]">
    <article v-for="item in items" :key="item.todoInstanceId" class="todo-item">
      <v-checkbox-btn
        :model-value="completedTodo(item)"
        readonly
        color="success"
        density="compact"
        :aria-label="`${item.title}：${humanize(item.status)}`"
      />
      <div class="todo-main">
        <div class="todo-title-line">
          <strong :class="{ completed: completedTodo(item) }">{{ item.title }}</strong>
          <span v-if="item.required" class="required-mark">必做</span>
          <span
            v-if="!productMode"
            :class="['dispatch-mark', { schedulable: schedulableTodo(item) }]"
            :title="item.dispatchDisposition"
          >{{ dispatchDispositionLabel(item.dispatchDisposition) || 'Manager 投影缺失' }}</span>
        </div>
        <small v-if="!productMode">{{ showGame ? `${item.gameId} · ` : '' }}{{ item.operation }} · {{ item.category }}</small>
        <p v-if="productMode" class="todo-product-message">{{ productMessage(item) }}</p>
        <template v-else>
          <p class="dispatch-reason">调度依据：{{ item.dispatchReason || 'Manager 未提供 dispatchReason' }}</p>
          <p class="todo-next-action">下一动作：{{ item.actionAvailability?.nextAction || 'Manager 未提供 nextAction' }}</p>
          <p class="todo-available-actions">Manager 开放动作：{{ availableActions(item).join(' · ') || '无' }}</p>
        </template>
        <div v-if="!compact && !productMode" class="todo-detail-grid">
          <span>尝试 {{ item.attempts }}</span>
          <span>难度 {{ item.automationDifficulty }}</span>
          <span>风险 {{ item.risk }}</span>
          <span>自动化 {{ item.automationState }}</span>
          <span>能力 {{ item.adapterCapabilityRef ?? '未绑定' }}</span>
          <span>执行 {{ item.actionAvailability?.execute ? '开放' : '关闭' }}</span>
          <span>恢复 {{ item.actionAvailability?.resume ? '开放' : '关闭' }}</span>
          <span>复核 {{ item.actionAvailability?.review ? '开放' : '关闭' }}</span>
          <span>释放人工 {{ item.actionAvailability?.releaseHuman ? '开放' : '关闭' }}</span>
          <span>人工证据 {{ item.actionAvailability?.submitManualEvidence ? '开放' : '关闭' }}</span>
          <span>重置 {{ resetRuleLabel(item.resetRule) }}</span>
          <span>周期止于 {{ formatTime(item.periodEndsAt) }}</span>
        </div>
        <p v-if="!productMode && item.reason" class="todo-reason">{{ item.reason }}</p>
        <article v-if="!productMode && assessmentFor(item.todoInstanceId)" class="automation-assessment">
          <div class="assessment-heading">
            <strong>最新自动化评估：{{ assessmentFor(item.todoInstanceId)?.difficulty }}</strong>
            <span>
              置信度 {{ Math.round((assessmentFor(item.todoInstanceId)?.confidence ?? 0) * 100) }}%
              · {{ formatTime(assessmentFor(item.todoInstanceId)?.createdAt) }}
            </span>
          </div>
          <p v-if="assessmentFor(item.todoInstanceId)?.failureStage">失败阶段：{{ assessmentFor(item.todoInstanceId)?.failureStage }}</p>
          <p v-if="assessmentFor(item.todoInstanceId)?.recommendation">建议：{{ assessmentFor(item.todoInstanceId)?.recommendation }}</p>
          <details v-if="!compact && (assessmentFor(item.todoInstanceId)?.basis.length || assessmentFor(item.todoInstanceId)?.evidenceIds.length)">
            <summary>查看评估依据与证据</summary>
            <p v-if="assessmentFor(item.todoInstanceId)?.basis.length">依据：{{ assessmentFor(item.todoInstanceId)?.basis.join('；') }}</p>
            <p v-if="assessmentFor(item.todoInstanceId)?.evidenceIds.length">证据：{{ assessmentFor(item.todoInstanceId)?.evidenceIds.join(' · ') }}</p>
          </details>
        </article>
        <details v-if="!productMode" :open="!compact" class="todo-provenance">
          <summary>
            <span>definitionVersion {{ item.definitionVersion }}</span>
            <span>catalogVersion {{ item.catalogVersion }}</span>
            <span class="source-hash">sourceHash (SHA-256) <code>{{ item.sourceHash }}</code></span>
            <span v-if="compact">来源 {{ item.sourceRefs.length }} 项</span>
          </summary>
          <div class="provenance-content">
            <p>稳定定义 ID <code>{{ item.todoDefinitionId }}</code></p>
            <p>来源引用</p>
            <ul v-if="item.sourceRefs.length" class="source-ref-list">
              <li v-for="sourceRef in item.sourceRefs" :key="sourceRef"><code>{{ sourceRef }}</code></li>
            </ul>
            <p v-else class="missing-source">未登记来源引用</p>
          </div>
        </details>
        <p v-if="!productMode && !compact && item.evidenceRefs.length" class="todo-refs">证据：{{ item.evidenceRefs.join(' · ') }}</p>
        <details v-if="!productMode && !compact && attemptsFor(item.todoInstanceId).length" class="attempt-history">
          <summary>展开 {{ attemptsFor(item.todoInstanceId).length }} 次尝试</summary>
          <article v-for="attempt in attemptsFor(item.todoInstanceId)" :key="attempt.todoAttemptId" class="attempt-row">
            <div>
              <strong>#{{ attempt.attemptNumber }} · {{ attempt.state }}</strong>
              <span>{{ formatTime(attempt.startedAt) }}<template v-if="attempt.retryable"> · 可安全重试</template></span>
            </div>
            <p v-if="attempt.reason || attempt.reasonCode">{{ attempt.reasonCode }}<template v-if="attempt.reasonCode && attempt.reason"> · </template>{{ attempt.reason }}</p>
            <p v-if="attempt.evidenceRefs.length">证据：{{ attempt.evidenceRefs.join(' · ') }}</p>
          </article>
        </details>
      </div>
      <StatusBadge :state="item.status" small />
    </article>
  </div>
  <p v-else class="muted-copy">当前周期没有每日项目。</p>
</template>

<style scoped>
.todo-checklist { display: grid; gap: 9px; }
.todo-item { display: grid; grid-template-columns: 34px minmax(0,1fr) auto; align-items: start; gap: 8px; padding: 13px; border: 1px solid var(--line); border-radius: 12px; background: rgba(7,20,34,.5); }
.todo-main { min-width: 0; }
.todo-title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.todo-title-line strong { color: #e9f4ff; font-size: .83rem; }
.todo-title-line strong.completed { color: #7fa995; text-decoration: line-through; }
.todo-main small { display: block; margin-top: 4px; color: var(--muted); font: .68rem "Cascadia Code", Consolas, monospace; }
.required-mark,.dispatch-mark { padding: 2px 6px; border-radius: 999px; font-size: .62rem; }
.required-mark { background: rgba(255,198,109,.1); color: #ffd28b; }
.dispatch-mark { background: rgba(142,190,230,.1); color: #9eb9d2; }
.dispatch-mark.schedulable { background: rgba(103,221,178,.1); color: #7ee6c2; }
.todo-detail-grid { display: flex; flex-wrap: wrap; gap: 5px 13px; margin-top: 9px; color: #86a1be; font-size: .69rem; }
.dispatch-reason,.todo-next-action,.todo-available-actions,.todo-reason,.todo-refs { margin: 7px 0 0; color: #cbb98f; font-size: .71rem; line-height: 1.5; overflow-wrap: anywhere; }
.todo-next-action { color: #9cc5e7; }
.todo-available-actions { color: #83a0bb; }
.todo-product-message { margin: 5px 0 0; color: #9eb8d0; font-size: .71rem; line-height: 1.5; }
.todo-refs { color: #7994b2; }
.automation-assessment { margin-top: 9px; padding: 8px 9px; border: 1px solid rgba(123,182,231,.16); border-radius: 8px; background: rgba(12,31,50,.48); }
.assessment-heading { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px; }
.assessment-heading strong { color: #b9d8ef; font-size: .7rem; }
.assessment-heading span,.automation-assessment p,.automation-assessment summary { color: #809bb6; font-size: .66rem; line-height: 1.45; }
.automation-assessment p { margin: 5px 0 0; overflow-wrap: anywhere; }
.automation-assessment details { margin-top: 6px; }
.automation-assessment summary { cursor: pointer; color: #8bb8df; }
.todo-provenance { margin-top: 8px; border: 1px solid rgba(142,190,230,.12); border-radius: 8px; background: rgba(11,29,47,.38); }
.todo-provenance summary { padding: 7px 9px; cursor: pointer; color: #82a0bd; font-size: .67rem; line-height: 1.45; overflow-wrap: anywhere; }
.todo-provenance summary > span { margin-right: 11px; }
.todo-provenance summary > span:last-child { margin-right: 0; }
.todo-provenance code { color: #a9c7e1; font: .65rem "Cascadia Code", Consolas, monospace; overflow-wrap: anywhere; word-break: break-all; }
.source-hash { display: inline; }
.provenance-content { padding: 0 9px 9px; border-top: 1px solid rgba(142,190,230,.08); color: #7994b2; font-size: .68rem; }
.provenance-content p { margin: 7px 0 0; overflow-wrap: anywhere; }
.source-ref-list { display: grid; gap: 4px; margin: 5px 0 0; padding-left: 18px; }
.source-ref-list li { padding-left: 2px; }
.missing-source { color: #c9a37b; }
.attempt-history { margin-top: 9px; border-top: 1px solid rgba(142,190,230,.1); padding-top: 8px; }
.attempt-history summary { cursor: pointer; color: #8bb8df; font-size: .7rem; }
.attempt-row { margin-top: 7px; padding: 8px 9px; border-radius: 8px; background: rgba(14,37,59,.46); }
.attempt-row div { display: flex; justify-content: space-between; gap: 8px; color: #aec6dc; font-size: .68rem; }
.attempt-row div strong { color: #d7e9f8; }.attempt-row p { margin: 5px 0 0; color: #839db6; font-size: .66rem; overflow-wrap: anywhere; }
.compact .todo-item { padding: 9px 11px; }
.compact .todo-main small { margin-top: 2px; }
.compact .todo-provenance { margin-top: 6px; }
@media (max-width: 620px) { .todo-item { grid-template-columns: 30px minmax(0,1fr); }.todo-item > :last-child { grid-column: 2; } }
</style>

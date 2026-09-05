<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { managerApi } from '../api/client'
import {
  extractItems,
  type NotificationAttempt,
  type NotificationDelivery,
  type NotificationPolicy,
  type NotificationPreview,
  type PageResult,
} from '../api/contracts'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatTime } from '../utils/format'
import {
  notificationCanRetry,
  notificationCanSend,
  notificationReason,
  notificationRetryIsAmbiguous,
  notificationRetryRequest,
  notificationSendRequest,
} from '../utils/notifications'

const manager = useManagerStore()
const deliveries = ref<NotificationDelivery[]>([])
const policy = ref<NotificationPolicy>()
const selectedId = ref('')
const selectedDetail = ref<NotificationDelivery>()
const preview = ref<NotificationPreview>()
const attempts = ref<NotificationAttempt[]>([])
const loading = ref(false)
const detailLoading = ref(false)
const error = ref<string>()
const detailError = ref<string>()
const reason = ref('')
const busyAction = ref<'send' | 'retry'>()
const ambiguousDialog = ref(false)
const pendingRetryId = ref('')
let detailGeneration = 0

const selected = computed(() => {
  if (selectedDetail.value?.notificationId === selectedId.value) return selectedDetail.value
  return deliveries.value.find((item) => item.notificationId === selectedId.value)
})
const draftCount = computed(() => deliveries.value.filter((item) => item.state === 'draft').length)
const sendingCount = computed(() => deliveries.value.filter((item) => item.state === 'sending').length)
const failedCount = computed(() => deliveries.value.filter((item) => item.state === 'failed').length)
const reasonReady = computed(() => notificationReason(reason.value).length > 0)
const selectedRetryIsAmbiguous = computed(() => notificationRetryIsAmbiguous(selected.value, attempts.value))
const sendSupported = computed(() => manager.supports('/notifications/{notificationId}/send-requests', 'post'))
const retrySupported = computed(() => manager.supports('/notifications/{notificationId}/retry-requests', 'post'))

function errorText(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

async function loadSelected(): Promise<void> {
  const notificationId = selectedId.value
  const generation = ++detailGeneration
  selectedDetail.value = undefined
  preview.value = undefined
  attempts.value = []
  detailError.value = undefined
  if (!notificationId) {
    detailLoading.value = false
    return
  }
  detailLoading.value = true
  const encoded = encodeURIComponent(notificationId)
  const results = await Promise.allSettled([
    managerApi.get<NotificationDelivery>(`/notifications/${encoded}`),
    managerApi.get<NotificationPreview>(`/notifications/${encoded}/preview`),
    managerApi.get<NotificationAttempt[] | PageResult<NotificationAttempt>>(`/notifications/${encoded}/attempts`),
  ])
  if (generation !== detailGeneration) return
  if (results[0].status === 'fulfilled') selectedDetail.value = results[0].value
  if (results[1].status === 'fulfilled') preview.value = results[1].value
  if (results[2].status === 'fulfilled') attempts.value = extractItems(results[2].value)
  const failures = results
    .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    .map((result) => errorText(result.reason))
  detailError.value = failures.length ? [...new Set(failures)].join('；') : undefined
  detailLoading.value = false
}

async function loadIndex(): Promise<void> {
  loading.value = true
  error.value = undefined
  try {
    const [nextPolicy, nextDeliveries] = await Promise.all([
      managerApi.get<NotificationPolicy>('/notification-policy'),
      managerApi.get<NotificationDelivery[] | PageResult<NotificationDelivery>>('/notifications?limit=100'),
    ])
    policy.value = nextPolicy
    deliveries.value = extractItems(nextDeliveries)
    const nextSelectedId = deliveries.value.some((item) => item.notificationId === selectedId.value)
      ? selectedId.value
      : deliveries.value[0]?.notificationId ?? ''
    if (nextSelectedId !== selectedId.value) selectedId.value = nextSelectedId
    else await loadSelected()
  } catch (caught) {
    error.value = errorText(caught)
  } finally {
    loading.value = false
  }
}

async function requestSend(): Promise<void> {
  const delivery = selected.value
  if (!delivery || !notificationCanSend(delivery) || !reasonReady.value) return
  busyAction.value = 'send'
  try {
    const receipt = await manager.submitCommand(
      '请求发送通知',
      'POST',
      `/notifications/${encodeURIComponent(delivery.notificationId)}/send-requests`,
      { ...notificationSendRequest(reason.value) },
    )
    if (receipt) {
      reason.value = ''
      await loadIndex()
    }
  } finally {
    busyAction.value = undefined
  }
}

async function submitRetry(notificationId: string, confirmAmbiguous: boolean): Promise<void> {
  if (!reasonReady.value) return
  busyAction.value = 'retry'
  try {
    const receipt = await manager.submitCommand(
      '请求重试通知',
      'POST',
      `/notifications/${encodeURIComponent(notificationId)}/retry-requests`,
      { ...notificationRetryRequest(reason.value, confirmAmbiguous) },
    )
    if (receipt) {
      reason.value = ''
      await loadIndex()
    }
  } finally {
    busyAction.value = undefined
  }
}

async function requestRetry(): Promise<void> {
  const delivery = selected.value
  if (!delivery || !notificationCanRetry(delivery) || !reasonReady.value) return
  if (notificationRetryIsAmbiguous(delivery, attempts.value)) {
    pendingRetryId.value = delivery.notificationId
    ambiguousDialog.value = true
    return
  }
  await submitRetry(delivery.notificationId, false)
}

async function confirmAmbiguousRetry(): Promise<void> {
  const notificationId = pendingRetryId.value
  ambiguousDialog.value = false
  pendingRetryId.value = ''
  if (notificationId) await submitRetry(notificationId, true)
}

watch(selectedId, () => void loadSelected())
watch(
  () => manager.events[0],
  (event) => {
    if (event?.type.startsWith('notification.')) void loadIndex()
  },
)
onMounted(loadIndex)
</script>

<template>
  <PageHeader
    eyebrow="NOTIFICATION / OUTBOX"
    title="通知投递"
    description="查看 BatchSeal 生成的 typed delivery、预览、附件决策和投递尝试。发送请求只引用收件人绑定；传输地址、凭据和本机路径不会进入页面。"
  >
    <v-btn variant="tonal" :loading="loading" @click="loadIndex">刷新通知</v-btn>
  </PageHeader>

  <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
  <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />

  <div class="content-grid policy-grid">
    <v-card class="panel metric-card span-3">
      <span>Policy</span>
      <strong class="policy-value"><StatusBadge :state="policy ? (policy.enabled ? 'enabled' : 'disabled') : 'unknown'" /></strong>
      <small>enabled</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>自动投递</span>
      <strong class="policy-value"><StatusBadge :state="policy ? (policy.automaticDispatch ? 'automatic' : 'manual_review') : 'unknown'" /></strong>
      <small>automaticDispatch</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>凭据状态</span>
      <strong class="policy-value"><StatusBadge :state="policy?.secretState ?? 'unknown'" /></strong>
      <small>secretState</small>
    </v-card>
    <v-card class="panel metric-card span-3">
      <span>收件人绑定</span>
      <strong class="binding-value mono">{{ policy?.recipientBindingId ?? '—' }}</strong>
      <small>recipientBindingId · 仅 opaque ID</small>
    </v-card>
  </div>

  <ResourceState
    :loading="loading"
    :error="error"
    :empty="!deliveries.length"
    empty-title="尚无通知投递"
    empty-detail="只有 BatchSeal 生成 delivery 后才会出现在这里；空列表不代表游戏任务已经完成。"
    @retry="loadIndex"
  >
    <div class="content-grid mt-5">
      <v-card class="panel span-5">
        <div class="panel-title">
          <div><h2>Delivery 索引</h2><span class="soft-note">草稿 {{ draftCount }} · 发送中 {{ sendingCount }} · 失败 {{ failedCount }}</span></div>
          <span class="soft-note">{{ deliveries.length }} 条</span>
        </div>
        <div class="delivery-list">
          <button
            v-for="delivery in deliveries"
            :key="delivery.notificationId"
            :class="['delivery-row', { active: delivery.notificationId === selectedId }]"
            type="button"
            @click="selectedId = delivery.notificationId"
          >
            <div>
              <strong>{{ delivery.subject }}</strong>
              <small>{{ delivery.outcome }} · {{ delivery.dispatchGate }} · {{ formatTime(delivery.updatedAt) }}</small>
              <code>{{ delivery.notificationId }}</code>
            </div>
            <StatusBadge :state="delivery.state" small />
          </button>
        </div>
      </v-card>

      <v-card v-if="selected" class="panel span-7">
        <div class="panel-title">
          <div><h2>{{ selected.subject }}</h2><span class="soft-note mono">{{ selected.notificationId }}</span></div>
          <StatusBadge :state="selected.state" />
        </div>
        <div class="panel-body">
          <v-progress-linear v-if="detailLoading" indeterminate color="primary" class="mb-4" />
          <v-alert v-if="detailError" type="warning" variant="tonal" class="mb-4">
            部分通知详情暂不可读：{{ detailError }}
          </v-alert>

          <dl class="delivery-facts">
            <div><dt>Delivery state</dt><dd><StatusBadge :state="selected.state" small /></dd></div>
            <div><dt>Dispatch gate</dt><dd><StatusBadge :state="selected.dispatchGate" small /></dd></div>
            <div><dt>Outcome</dt><dd><StatusBadge :state="selected.outcome" small /></dd></div>
            <div><dt>Attempts</dt><dd>{{ selected.attemptCount }} / 3</dd></div>
            <div><dt>BatchSeal</dt><dd class="mono">{{ selected.batchId }} · v{{ selected.sealVersion }}</dd></div>
            <div><dt>Next attempt</dt><dd>{{ formatTime(selected.nextAttemptAt) }}</dd></div>
            <div><dt>Last error</dt><dd class="mono">{{ selected.lastErrorClass || '—' }}</dd></div>
            <div><dt>Sent at</dt><dd>{{ formatTime(selected.sentAt) }}</dd></div>
          </dl>

          <v-alert v-if="selectedRetryIsAmbiguous" type="error" variant="tonal" class="mt-4">
            上一次投递结果不确定，消息可能已经送达。重试前必须再次确认，避免重复发送。
          </v-alert>
          <v-alert v-else-if="policy?.secretState !== 'configured'" type="warning" variant="tonal" class="mt-4">
            当前凭据状态为 {{ policy?.secretState ?? 'unknown' }}；发送请求会保留在安全 gate，不会伪装成已送达。
          </v-alert>

          <v-textarea
            v-model="reason"
            label="发送或重试原因"
            placeholder="说明已经复核了哪些状态"
            variant="outlined"
            rows="2"
            maxlength="500"
            counter
            class="mt-4"
          />
          <div class="action-row">
            <v-btn
              v-if="selected.state === 'draft'"
              color="primary"
              :loading="busyAction === 'send'"
              :disabled="!reasonReady || !notificationCanSend(selected) || !sendSupported"
              @click="requestSend"
            >显式请求发送</v-btn>
            <v-btn
              v-if="selected.state === 'failed'"
              color="warning"
              :loading="busyAction === 'retry'"
              :disabled="!reasonReady || !notificationCanRetry(selected) || !retrySupported"
              @click="requestRetry"
            >{{ selectedRetryIsAmbiguous ? '复核后重试' : '请求重试' }}</v-btn>
            <span v-if="selected.attemptCount >= 3" class="soft-note">已达到 3 次投递预算，页面不会继续提交。</span>
          </div>

          <section class="detail-section">
            <div class="section-heading"><h3>内容预览</h3><span>HTML 只按纯文本显示，不执行标签或事件</span></div>
            <div v-if="preview" class="preview-grid">
              <article><h4>纯文本</h4><pre>{{ preview.textBody }}</pre></article>
              <article><h4>HTML 源码（已转义）</h4><pre>{{ preview.htmlBody }}</pre></article>
            </div>
            <p v-else class="muted-copy">预览尚不可用。</p>
          </section>

          <section class="detail-section">
            <div class="section-heading"><h3>附件决策</h3><span>只显示 Manager artifact ID 与决策，不显示路径</span></div>
            <div v-if="preview?.attachmentDecisions.length" class="table-scroll">
              <table class="data-table compact-table">
                <thead><tr><th>Artifact ID</th><th>决策</th><th>原因</th></tr></thead>
                <tbody>
                  <tr v-for="decision in preview.attachmentDecisions" :key="decision.artifactId">
                    <td class="mono">{{ decision.artifactId }}</td>
                    <td><StatusBadge :state="decision.accepted ? 'accepted' : 'rejected'" small /></td>
                    <td>{{ decision.reason }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="muted-copy">没有可附加的严格证据。</p>
          </section>

          <section class="detail-section">
            <div class="section-heading"><h3>Attempt history</h3><span>{{ attempts.length }} 次</span></div>
            <div v-if="attempts.length" class="table-scroll">
              <table class="data-table compact-table">
                <thead><tr><th>#</th><th>状态</th><th>结果</th><th>错误</th><th>开始</th><th>完成</th></tr></thead>
                <tbody>
                  <tr v-for="attempt in attempts" :key="attempt.attemptId">
                    <td>{{ attempt.attemptNumber }}</td>
                    <td><StatusBadge :state="attempt.state" small /></td>
                    <td><StatusBadge :state="attempt.outcome ?? 'unknown'" small /></td>
                    <td class="mono">{{ attempt.errorClass || '—' }}</td>
                    <td>{{ formatTime(attempt.startedAt) }}</td>
                    <td>{{ formatTime(attempt.completedAt) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="muted-copy">尚无实际投递尝试。</p>
          </section>
        </div>
      </v-card>
    </div>
  </ResourceState>

  <v-dialog v-model="ambiguousDialog" max-width="560" persistent>
    <v-card class="panel">
      <div class="panel-title"><h2>再次确认可能重复投递</h2><StatusBadge state="approval_required" /></div>
      <div class="panel-body">
        <p class="dialog-copy">上一次 attempt 的结果是 ambiguous，外部收件端可能已经收到消息。只有在核对收件状态后仍决定重试，才继续提交 `confirmAmbiguous=true`。</p>
        <div class="action-row mt-4">
          <v-btn variant="text" @click="ambiguousDialog = false; pendingRetryId = ''">取消</v-btn>
          <v-btn color="error" :loading="busyAction === 'retry'" @click="confirmAmbiguousRetry">已复核，确认重试</v-btn>
        </div>
      </div>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.policy-grid { margin-bottom: 18px; }
.policy-value { margin-top: 18px !important; font-family: inherit !important; font-size: 1rem !important; }
.binding-value { margin-top: 16px !important; font-family: "Cascadia Code", Consolas, monospace !important; font-size: .9rem !important; overflow-wrap: anywhere; }
.delivery-list { max-height: 760px; padding: 0 12px 16px; overflow: auto; }
.delivery-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; width: 100%; margin: 6px 0; padding: 13px; border: 1px solid transparent; border-radius: 12px; background: rgba(8,22,37,.45); color: inherit; text-align: left; cursor: pointer; }
.delivery-row:hover,.delivery-row.active { border-color: rgba(115,201,255,.24); background: rgba(55,102,142,.16); }
.delivery-row div { min-width: 0; }.delivery-row strong,.delivery-row small,.delivery-row code { display: block; }
.delivery-row strong { color: #edf6ff; font-size: .8rem; }.delivery-row small { margin-top: 5px; color: var(--muted); font-size: .68rem; }.delivery-row code { margin-top: 7px; overflow: hidden; color: #6f89a8; font-size: .64rem; text-overflow: ellipsis; }
.delivery-facts { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 13px; margin: 0; }
.delivery-facts div { min-width: 0; padding: 11px 12px; border: 1px solid rgba(142,190,230,.09); border-radius: 10px; background: rgba(5,17,30,.32); }
.delivery-facts dt { color: var(--muted); font-size: .67rem; }.delivery-facts dd { margin: 6px 0 0; color: #d6e2f0; font-size: .78rem; overflow-wrap: anywhere; }
.detail-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--line); }
.section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 14px; margin-bottom: 12px; }.section-heading h3 { margin: 0; font-size: .88rem; }.section-heading span { color: var(--muted); font-size: .7rem; }
.preview-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }
.preview-grid article { min-width: 0; border: 1px solid var(--line); border-radius: 12px; background: rgba(3,12,22,.45); overflow: hidden; }.preview-grid h4 { margin: 0; padding: 10px 12px; border-bottom: 1px solid var(--line); color: #aac4df; font-size: .72rem; }.preview-grid pre { min-height: 150px; max-height: 360px; margin: 0; padding: 13px; overflow: auto; color: #a9bdd4; font-size: .7rem; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.compact-table { min-width: 640px; }.compact-table td { padding: 10px; font-size: .74rem; }
.dialog-copy { margin: 0; color: #c4d2e3; font-size: .82rem; line-height: 1.7; }
@media (max-width: 760px) { .delivery-facts,.preview-grid { grid-template-columns: 1fr; }.section-heading { align-items: flex-start; flex-direction: column; } }
</style>

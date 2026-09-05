<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useManagerStore } from '../stores/manager'
import { formatTime } from '../utils/format'
import StatusBadge from './StatusBadge.vue'
import type { CommandReceiptSummary } from '../utils/receiptCache'

const model = defineModel<boolean>({ required: true })
const manager = useManagerStore()
const { receipts, errors } = storeToRefs(manager)
const count = computed(() => receipts.value.length + errors.value.length)

function errorDetail(detail: string): string {
  return detail && detail !== 'undefined'
    ? detail
    : '页面没有收到这次请求的错误详情。请刷新快照确认当前批次状态，再决定是否重试。'
}

function receiptTime(receipt: CommandReceiptSummary): string | undefined {
  return ['succeeded', 'rejected', 'failed'].includes(receipt.state)
    ? receipt.completedAt ?? receipt.submittedAt
    : receipt.submittedAt
}

function receiptTimeLabel(receipt: CommandReceiptSummary): string {
  return ['succeeded', 'rejected', 'failed'].includes(receipt.state) && receipt.completedAt
    ? '完成'
    : '提交'
}
</script>

<template>
  <v-navigation-drawer v-model="model" location="right" width="400" temporary class="command-drawer">
    <div class="drawer-heading">
      <div>
        <p class="eyebrow">REQUEST / RECEIPT</p>
        <h2>命令与错误</h2>
      </div>
      <span class="count-orb">{{ count }}</span>
    </div>

    <section v-if="errors.length" class="drawer-section">
      <h3>需要处理</h3>
      <article v-for="error in errors" :key="error.id" class="error-record">
        <div class="d-flex justify-space-between ga-3">
          <strong>{{ error.title }}</strong>
          <button class="text-button" @click="manager.clearError(error.id)">移除</button>
        </div>
        <p>{{ errorDetail(error.detail) }}</p>
        <p v-if="error.nextAction" class="error-next-action"><strong>下一步：</strong>{{ error.nextAction }}</p>
        <small>{{ formatTime(error.occurredAt) }}<template v-if="error.requestId"> · {{ error.requestId }}</template><template v-if="error.idempotencyKey"> · {{ error.idempotencyKey }}</template></small>
      </article>
    </section>

    <section class="drawer-section">
      <div class="d-flex justify-space-between align-center">
        <h3>最近命令</h3>
        <button class="text-button" @click="manager.clearSettledReceipts">清理已记录</button>
      </div>
      <div v-if="!receipts.length" class="muted-copy">还没有提交命令。</div>
      <article v-for="receipt in receipts" :key="receipt.commandId" class="receipt-record">
        <div class="d-flex justify-space-between align-center ga-3">
          <code>{{ receipt.commandId }}</code>
          <StatusBadge :state="receipt.state" small />
        </div>
        <p>{{ receipt.message ?? 'Manager 已返回 receipt；业务完成仍以后续状态与证据为准。' }}</p>
        <small>{{ receiptTimeLabel(receipt) }} {{ formatTime(receiptTime(receipt)) }} · state v{{ receipt.acceptedStateVersion ?? '—' }}<template v-if="receipt.requestId"> · {{ receipt.requestId }}</template></small>
      </article>
    </section>
  </v-navigation-drawer>
</template>

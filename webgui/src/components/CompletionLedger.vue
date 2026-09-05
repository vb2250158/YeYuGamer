<script setup lang="ts">
import type { CompletionAdjudication, CompletionReview } from '../api/contracts'
import type { CompletionBlockerView } from '../utils/completion'
import { formatTime } from '../utils/format'
import StatusBadge from './StatusBadge.vue'

withDefaults(defineProps<{
  reviews?: CompletionReview[]
  adjudications?: CompletionAdjudication[]
  awaitingRunIds?: string[]
  reviewWorkItemIds?: Record<string, string>
  blockers?: CompletionBlockerView[]
}>(), {
  reviews: () => [],
  adjudications: () => [],
  awaitingRunIds: () => [],
  reviewWorkItemIds: () => ({}),
  blockers: () => [],
})

function metricsText(metrics: Record<string, boolean | number | string>): string {
  return Object.entries(metrics).map(([name, value]) => `${name}=${String(value)}`).join(' · ')
}
</script>

<template>
  <section class="completion-ledger">
    <div class="ledger-heading">
      <div>
        <strong>完成复核台账</strong>
        <span>CompletionReview 与 CompletionAdjudication 均为 Manager 只读记录</span>
      </div>
      <StatusBadge :state="awaitingRunIds.length ? 'review_required' : adjudications[0]?.contract.outcome ?? 'unknown'" small />
    </div>

    <div v-if="awaitingRunIds.length" class="ledger-section awaiting-section">
      <h3>等待 CompletionReview</h3>
      <article v-for="runId in awaitingRunIds" :key="runId" class="scope-row">
        <div><strong>{{ runId }}</strong><span>同一 GameRun 保持冻结，尚未满足批次封印条件</span></div>
        <code>{{ reviewWorkItemIds[runId] ?? 'work item 尚未返回' }}</code>
      </article>
    </div>

    <div v-if="blockers.length" class="ledger-section">
      <h3>Todo / human_required 阻塞投影</h3>
      <article v-for="blocker in blockers" :key="`${blocker.source}:${blocker.blockerId}`" class="ledger-row">
        <div class="row-main">
          <div class="row-title">
            <strong>{{ blocker.title }}</strong>
            <StatusBadge :state="blocker.status" small />
          </div>
          <span>{{ blocker.gameId ?? 'global' }} · {{ blocker.blockerId }} · 来源 {{ blocker.source }}</span>
          <p v-if="blocker.reason">{{ blocker.reason }}</p>
          <p v-if="blocker.humanRequiredCount">人工门命中 {{ blocker.humanRequiredCount }} 次</p>
          <p v-if="blocker.evidenceRefs.length">证据：{{ blocker.evidenceRefs.join(' · ') }}</p>
        </div>
      </article>
    </div>

    <div v-if="reviews.length" class="ledger-section">
      <h3>CompletionReview</h3>
      <article v-for="review in reviews" :key="review.completionReviewId" class="ledger-row">
        <div class="row-main">
          <div class="row-title">
            <strong>{{ review.gameId }} · {{ review.completionReviewId }}</strong>
            <StatusBadge :state="review.decision" small />
          </div>
          <span>{{ review.runId }} · {{ review.runAttemptId }} · {{ review.gameDayKey }}</span>
          <span v-if="review.completionPolicyId || review.attemptLineageIds">
            Policy {{ review.completionPolicyId ?? 'unknown' }}<template v-if="review.completionPolicyVersion">@{{ review.completionPolicyVersion }}</template>
            · Attempt lineage {{ review.attemptLineageIds?.length ?? 0 }}
          </span>
          <span>工作项 {{ review.workItemId }} · Reviewer {{ review.reviewerPrincipalId }} · {{ formatTime(review.reviewedAt) }}</span>
          <div v-if="review.todoReviews?.length" class="predicate-list">
            <div v-for="todoReview in review.todoReviews" :key="todoReview.todoInstanceId" class="predicate-row">
              <div class="row-title">
                <code>{{ todoReview.todoInstanceId }}</code>
                <StatusBadge :state="todoReview.verdict" small />
              </div>
              <span>原因码：{{ todoReview.reasonCode }}</span>
              <span>本 Todo 截图：{{ todoReview.artifactRefs.join(' · ') }}</span>
            </div>
          </div>
          <div class="predicate-list">
            <div v-for="predicate in review.predicates" :key="predicate.predicateId" class="predicate-row">
              <code>{{ predicate.predicateId }}</code>
              <span>{{ metricsText(predicate.metrics) }}</span>
              <span>证据：{{ predicate.artifactRefs.join(' · ') }}</span>
            </div>
          </div>
          <p v-if="review.artifactRefs.length">复核证据全集：{{ review.artifactRefs.join(' · ') }}</p>
        </div>
      </article>
    </div>

    <div v-if="adjudications.length" class="ledger-section">
      <h3>CompletionAdjudication</h3>
      <article v-for="item in adjudications" :key="item.completionAdjudicationId" class="ledger-row">
        <div class="row-main">
          <div class="row-title">
            <strong>{{ item.gameId }} · {{ item.completionAdjudicationId }}</strong>
            <StatusBadge :state="item.contract.outcome" small />
          </div>
          <span>{{ item.runId }} · {{ item.runAttemptId ?? 'no current attempt' }} · {{ item.gameDayKey }} · {{ formatTime(item.createdAt) }}</span>
          <p>{{ item.contract.message }}</p>
          <div class="contract-summary">
            <span>acceptedDone={{ item.contract.acceptedDone }}</span>
            <span>Review ID：{{ item.contract.reviewId ?? '无' }}</span>
            <span>当前 attempt 证据：{{ item.contract.currentAttemptEvidenceRefs?.length ?? 0 }}</span>
            <span>继承证据：{{ item.contract.carriedEvidenceRefs?.length ?? 0 }}</span>
            <span>缺失：{{ item.contract.missingPredicates.join(' · ') || '无' }}</span>
            <span>阻塞：{{ item.contract.blockingPredicates.join(' · ') || '无' }}</span>
            <span>Blocker IDs：{{ item.contract.blockerIds.join(' · ') || '无' }}</span>
          </div>
          <div class="predicate-list">
            <div v-for="evaluation in item.contract.evaluations" :key="evaluation.code" class="predicate-row">
              <div class="row-title"><code>{{ evaluation.code }}</code><StatusBadge :state="evaluation.state" small /></div>
              <span>{{ evaluation.message }}</span>
              <span v-if="evaluation.todoInstanceIds.length">Todo：{{ evaluation.todoInstanceIds.join(' · ') }}</span>
              <span v-if="evaluation.artifactRefs.length">证据：{{ evaluation.artifactRefs.join(' · ') }}</span>
            </div>
          </div>
          <p v-if="item.contract.artifactRefs.length">合同证据全集：{{ item.contract.artifactRefs.join(' · ') }}</p>
          <p v-if="item.contract.screenshotArtifactRefs.length">原始截图：{{ item.contract.screenshotArtifactRefs.join(' · ') }}</p>
          <p v-if="item.contract.acceptedEvidenceRefs.length">采纳证据：{{ item.contract.acceptedEvidenceRefs.join(' · ') }}</p>
          <p v-if="item.contract.supportingArtifactRefs.length">支持证据：{{ item.contract.supportingArtifactRefs.join(' · ') }}</p>
          <div v-if="item.contract.excludedEvidence.length" class="excluded-list">
            <strong>排除证据</strong>
            <span v-for="excluded in item.contract.excludedEvidence" :key="excluded.artifactId">
              {{ excluded.artifactId }}：{{ excluded.reasons.join(' · ') }}
            </span>
          </div>
        </div>
      </article>
    </div>

    <p v-if="!awaitingRunIds.length && !blockers.length && !reviews.length && !adjudications.length" class="empty-ledger">
      当前范围尚无 CompletionReview、CompletionAdjudication 或显式 Todo blocker；这不等于 accepted_done。
    </p>
  </section>
</template>

<style scoped>
.completion-ledger { display: grid; gap: 14px; }
.ledger-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.ledger-heading strong,.ledger-heading span { display: block; }.ledger-heading strong { color: #e8f4ff; font-size: .85rem; }.ledger-heading span { margin-top: 4px; color: var(--muted); font-size: .68rem; }
.ledger-section { display: grid; gap: 8px; }.ledger-section h3 { margin: 0; color: #a9c8e5; font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; }
.awaiting-section { padding: 11px; border: 1px solid rgba(255,198,109,.2); border-radius: 11px; background: rgba(80,56,18,.1); }
.scope-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.scope-row strong,.scope-row span { display: block; }.scope-row strong { font-size: .74rem; }.scope-row span { margin-top: 3px; color: var(--muted); font-size: .66rem; }.scope-row code { color: #ffd28b; font-size: .65rem; overflow-wrap: anywhere; }
.ledger-row { padding: 11px; border: 1px solid var(--line); border-radius: 11px; background: rgba(7,20,34,.48); }
.row-main { display: grid; gap: 6px; min-width: 0; }.row-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; }.row-title strong { color: #e5f2fc; font-size: .76rem; }.row-main > span,.row-main > p { margin: 0; color: var(--muted); font-size: .67rem; line-height: 1.5; overflow-wrap: anywhere; }
.predicate-list { display: grid; gap: 6px; margin-top: 3px; }.predicate-row { display: grid; gap: 4px; padding: 8px; border-radius: 8px; background: rgba(18,44,67,.46); }.predicate-row code { color: #8bc9f0; font-size: .66rem; overflow-wrap: anywhere; }.predicate-row span { color: #90aac3; font-size: .65rem; overflow-wrap: anywhere; }
.contract-summary { display: flex; flex-wrap: wrap; gap: 6px 14px; color: #b9cde0; font-size: .66rem; }
.excluded-list { display: grid; gap: 3px; color: #b99d7c; font-size: .65rem; }.excluded-list strong { color: #d8b98d; }
.empty-ledger { margin: 0; color: var(--muted); font-size: .7rem; }
@media (max-width: 620px) { .scope-row,.ledger-heading { align-items: flex-start; flex-direction: column; }.row-title { align-items: flex-start; } }
</style>

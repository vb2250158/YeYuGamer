<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EvidenceArtifact } from '../api/contracts'
import { useResource } from '../composables/useResource'
import { useManagerStore } from '../stores/manager'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import JsonPanel from '../components/JsonPanel.vue'
import { formatTime } from '../utils/format'
import { artifactContentPath, evidenceCanBeReviewed } from '../utils/managerResources'

const manager = useManagerStore()
const resource = useResource<EvidenceArtifact>('/artifacts')
const selectedId = ref('')
const note = ref('')
const busy = ref(false)
const imageLoadFailed = ref(false)

watch(resource.items, (items) => {
  if (!items.some((item) => item.artifactId === selectedId.value)) selectedId.value = items[0]?.artifactId ?? ''
}, { immediate: true })
const selected = computed(() => resource.items.value.find((item) => item.artifactId === selectedId.value))
const reviewable = computed(() => selected.value ? evidenceCanBeReviewed(selected.value) : false)
const contentSupported = computed(() => manager.supports('/artifacts/{artifactId}/content', 'get'))
const contentUrl = computed(() => {
  if (!selected.value) return ''
  return artifactContentPath(selected.value.artifactId)
})
watch(selectedId, () => { imageLoadFailed.value = false })

async function review(verdict: 'accepted' | 'rejected'): Promise<void> {
  if (!selected.value || !reviewable.value) return
  busy.value = true
  try {
    const receipt = await manager.submitCommand('提交证据复核', 'POST', '/evidence-reviews', {
      artifactId: selected.value.artifactId,
      verdict,
      note: note.value,
    })
    if (receipt) await resource.load()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <PageHeader
    eyebrow="EVIDENCE / ARTIFACT"
    title="证据与完成合同"
    description="原始帧、派生图、哈希、来源和复核结论都来自 Manager artifact 白名单；页面不接受任意本机路径。"
  >
    <v-btn variant="tonal" :loading="resource.loading.value" @click="resource.load">刷新证据</v-btn>
  </PageHeader>

  <ResourceState
    :loading="resource.loading.value"
    :error="resource.error.value"
    :empty="!resource.items.value.length"
    empty-title="还没有证据 artifact"
    empty-detail="空证据列表意味着无法验收完成；请查看运行记录或补充人工复核。"
    @retry="resource.load"
  >
    <div class="content-grid">
      <v-card class="panel span-5">
        <div class="panel-title"><h2>证据索引</h2><span class="soft-note">{{ resource.items.value.length }} 项</span></div>
        <div class="evidence-list">
          <button
            v-for="artifact in resource.items.value"
            :key="artifact.artifactId"
            :class="['evidence-row', { active: artifact.artifactId === selectedId }]"
            @click="selectedId = artifact.artifactId"
          >
            <div><strong>{{ artifact.kind ?? 'artifact' }}</strong><small>{{ artifact.gameId ?? 'global' }} · {{ formatTime(artifact.capturedAt) }}</small></div>
            <StatusBadge :state="artifact.verdict ?? (artifact.raw ? 'raw' : 'derived')" small />
          </button>
        </div>
      </v-card>

      <v-card v-if="selected" class="panel span-7">
        <div class="panel-title"><h2>{{ selected.artifactId }}</h2><StatusBadge :state="selected.verdict ?? 'evidence_pending'" /></div>
        <div class="panel-body">
          <img
            v-if="selected.contentType?.startsWith('image/') && contentSupported && !imageLoadFailed"
            :src="contentUrl"
            class="artifact-image"
            alt="Manager 提供的证据 artifact"
            @error="imageLoadFailed = true"
          />
          <div v-else class="evidence-preview">
            <span>{{ selected.contentType ?? '未知内容类型' }} · {{ selected.fileName ?? 'opaque artifact' }}</span>
            <v-alert v-if="selected.contentType?.startsWith('image/') && imageLoadFailed" type="warning" variant="tonal" class="mt-3">
              Manager 内容端点没有返回可显示的图片；页面不会生成占位图冒充证据。
            </v-alert>
            <v-alert v-else-if="!contentSupported" type="info" variant="tonal" class="mt-3">
              当前 Manager OpenAPI 没有 artifact content 端点，无法安全预览内容。
            </v-alert>
            <v-btn
              v-if="contentSupported"
              class="mt-3"
              size="small"
              variant="tonal"
              :href="contentUrl"
              target="_blank"
              rel="noopener noreferrer"
            >通过 Manager 打开内容</v-btn>
          </div>
          <div class="form-row mt-4">
            <div class="primary-cell"><small>Hash</small><strong class="mono">{{ selected.hash ?? '—' }}</strong></div>
            <div class="primary-cell"><small>来源</small><strong>{{ selected.source ?? '—' }}</strong></div>
          </div>
          <v-alert v-if="!reviewable" type="info" variant="tonal" class="mt-4">
            该 artifact 仅用于诊断或已标记为不参与验收，不能改写为“接受/拒绝完成证据”。
          </v-alert>
          <v-textarea v-model="note" label="复核说明" variant="outlined" rows="3" class="mt-4" />
          <div class="action-row">
            <v-btn color="error" variant="tonal" :loading="busy" :disabled="!reviewable || !manager.supports('/evidence-reviews', 'post')" @click="review('rejected')">拒绝证据</v-btn>
            <v-btn color="success" :loading="busy" :disabled="!reviewable || !manager.supports('/evidence-reviews', 'post')" @click="review('accepted')">接受证据</v-btn>
          </div>
          <p class="soft-note mt-3">接受单个 artifact 不等于游戏完成；Manager 仍需检查同 run marker 与完整完成合同。</p>
          <JsonPanel title="Artifact 元数据" :value="selected" />
        </div>
      </v-card>
    </div>
  </ResourceState>
</template>

<style scoped>
.evidence-list { padding: 0 12px 16px; }.evidence-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; width: 100%; margin: 6px 0; padding: 12px; border: 1px solid transparent; border-radius: 11px; background: rgba(8,22,37,.45); color: inherit; text-align: left; cursor: pointer; }.evidence-row.active,.evidence-row:hover { border-color: rgba(115,201,255,.22); background: rgba(55,102,142,.16); }.evidence-row strong,.evidence-row small { display:block; }.evidence-row strong{font-size:.78rem}.evidence-row small{margin-top:4px;color:var(--muted);font-size:.67rem}.artifact-image{display:block;width:100%;max-height:480px;object-fit:contain;border:1px solid var(--line);border-radius:12px;background:#030912}.evidence-preview{display:flex;flex-direction:column}.primary-cell small,.primary-cell strong{display:block}.primary-cell small{color:var(--muted);font-size:.68rem}.primary-cell strong{margin-top:5px;font-size:.78rem;word-break:break-all}
</style>

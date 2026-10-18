<script setup lang="ts">
import { computed, ref } from 'vue'
import type { EvidenceArtifact, TodoInstance } from '../api/contracts'
import { managerApi } from '../api/client'
import { useManagerStore } from '../stores/manager'
import { artifactContentPath } from '../utils/managerResources'

const props = defineProps<{ todo: TodoInstance }>()
const manager = useManagerStore()
const artifacts = ref<EvidenceArtifact[]>([])
const file = ref<File>()
const note = ref('')
const busy = ref(false)
const error = ref('')
const uploadedId = ref('')
const uploadAllowed = computed(() => !['in_progress', 'completed'].includes(props.todo.status)
  && manager.supports('/todo-instances/{todo_id}/evidence-uploads', 'post'))

async function loadEvidence(event: Event): Promise<void> {
  if (!(event.target as HTMLDetailsElement).open) return
  error.value = ''
  try {
    const images: EvidenceArtifact[] = []
    for (const evidenceId of props.todo.evidenceRefs) {
      const artifact = await managerApi.get<EvidenceArtifact>(`/artifacts/${encodeURIComponent(evidenceId)}`)
      if (artifact.source === 'operator-upload') images.push(artifact)
    }
    artifacts.value = images
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  }
}

function chooseFile(event: Event): void {
  file.value = (event.target as HTMLInputElement).files?.[0]
}

async function upload(): Promise<void> {
  const selected = file.value
  if (!selected || !note.value.trim() || busy.value) return
  if (!selected.size || selected.size > 8 * 1024 * 1024 || !['image/png', 'image/jpeg'].includes(selected.type)) {
    error.value = '请选择不超过 8 MiB 的 PNG 或 JPEG 截图。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const contentBase64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '')
      reader.onerror = () => reject(new Error('读取截图失败'))
      reader.readAsDataURL(selected)
    })
    const receipt = await manager.submitCommand('上传人工审核截图', 'POST',
      `/todo-instances/${encodeURIComponent(props.todo.todoInstanceId)}/evidence-uploads`,
      { fileName: selected.name, contentBase64, note: note.value.trim() })
    if (receipt) {
      const result = receipt.result as { evidenceId?: string; artifact?: EvidenceArtifact } | undefined
      uploadedId.value = result?.evidenceId ?? ''
      if (result?.artifact) artifacts.value = [...artifacts.value, result.artifact]
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <details v-if="uploadAllowed || todo.evidenceRefs.length" class="manual-evidence" @toggle="loadEvidence">
    <summary>人工截图与审核</summary>
    <p>人工上传只关联本事项并置为待审核，不替代官方终态或程序领奖校验；截图时间与账号身份仍待核实。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-for="artifact in artifacts" :key="artifact.artifactId">
      <a :href="artifactContentPath(artifact.artifactId)" target="_blank" rel="noopener">
        <img :src="artifactContentPath(artifact.artifactId)" alt="本事项的人工审核截图" loading="lazy">
      </a>
      <a :href="`/evidence?artifactId=${encodeURIComponent(artifact.artifactId)}`">查看截图并记录审核</a>
    </div>
    <p v-if="uploadedId" role="status">已上传，待人工审核：{{ uploadedId }}</p>
    <template v-if="uploadAllowed">
      <input type="file" accept="image/png,image/jpeg" aria-label="选择人工审核截图" :disabled="busy" @change="chooseFile">
      <v-textarea v-model="note" label="截图来源与待核实说明" rows="2" maxlength="2000" />
      <v-btn size="small" :loading="busy" :disabled="!file || !note.trim()" @click="upload">上传并置为待审核</v-btn>
    </template>
  </details>
</template>

<style scoped>
.manual-evidence { margin-top: 10px; }
.manual-evidence p { font-size: .8rem; }
.manual-evidence img { display: block; max-width: 100%; max-height: 260px; object-fit: contain; margin: 10px 0; }
</style>

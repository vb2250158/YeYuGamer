<script setup lang="ts">
import EmptyState from './EmptyState.vue'

defineProps<{ loading?: boolean; error?: string; empty?: boolean; emptyTitle?: string; emptyDetail?: string }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <v-progress-linear v-if="loading" color="primary" indeterminate rounded />
  <v-alert v-else-if="error" type="error" variant="tonal" class="mb-4">
    <div class="d-flex align-center justify-space-between ga-3">
      <span>{{ error }}</span>
      <v-btn size="small" variant="text" @click="$emit('retry')">重试</v-btn>
    </div>
  </v-alert>
  <EmptyState v-else-if="empty" :title="emptyTitle ?? '暂无数据'" :detail="emptyDetail" />
  <slot v-else />
</template>

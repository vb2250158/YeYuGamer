<script setup lang="ts">
import { formatJson } from '../utils/format'

defineProps<{ title: string; value: unknown; open?: boolean }>()
</script>

<template>
  <details class="json-panel" :open="open">
    <summary>{{ title }}</summary>
    <pre>{{ formatJson(value) }}</pre>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { humanize, statusTone } from '../utils/format'

const props = defineProps<{ state?: string; small?: boolean }>()
const tone = computed(() => statusTone(props.state))
</script>

<template>
  <span class="status-badge" :class="[`tone-${tone}`, { small }]">
    <span class="status-dot" />
    {{ humanize(state) }}
  </span>
</template>

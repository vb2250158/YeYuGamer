<script setup lang="ts">
withDefaults(defineProps<{ title: string; detail?: string; symbol?: string }>(), { symbol: '◇' })
</script>

<template>
  <div class="empty-state">
    <span class="empty-symbol">{{ symbol }}</span>
    <strong>{{ title }}</strong>
    <p v-if="detail">{{ detail }}</p>
    <slot />
  </div>
</template>

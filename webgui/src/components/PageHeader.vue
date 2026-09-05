<script setup lang="ts">
defineProps<{ title: string; eyebrow?: string; description?: string }>()
</script>

<template>
  <header class="page-header">
    <div>
      <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
      <h1>{{ title }}</h1>
      <p v-if="description" class="page-description">{{ description }}</p>
    </div>
    <div class="page-actions"><slot /></div>
  </header>
</template>

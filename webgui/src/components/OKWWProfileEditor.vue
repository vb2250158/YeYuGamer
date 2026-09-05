<script setup lang="ts">
import type { OKWWProfile } from '../api/contracts'

const props = defineProps<{ modelValue: OKWWProfile; disabled: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [profile: OKWWProfile] }>()
const tacetOptions = Array.from({ length: 19 }, (_, index) => ({ title: `F2 列表第 ${index + 1} 个`, value: index + 1 }))
function update<K extends keyof OKWWProfile>(key: K, value: OKWWProfile[K]): void {
  if (!props.disabled) emit('update:modelValue', { ...props.modelValue, [key]: value })
}
function updateFarm(value: unknown): void {
  if (value === 'Tacet Suppression' || value === 'Forgery Challenge' || value === 'Simulation Challenge') update('whichToFarm', value)
}
function updateMaterial(value: unknown): void {
  if (value === 'Resonator EXP' || value === 'Weapon EXP' || value === 'Shell Credit') update('materialSelection', value)
}
function updateIndex(key: 'tacetSuppressionNumber' | 'forgeryChallengeNumber', value: unknown): void {
  const index = Number(value)
  if (Number.isInteger(index) && index >= 1) update(key, index)
}
</script>

<template>
  <section class="account-profile">
    <strong>这个账号的体力路线与材料</strong>
    <div class="account-profile-fields">
      <v-select :model-value="modelValue.whichToFarm" label="体力路线"
        :items="[{ title: '无音区', value: 'Tacet Suppression' }, { title: '锻造挑战', value: 'Forgery Challenge' }, { title: '模拟领域', value: 'Simulation Challenge' }]"
        density="compact" variant="outlined" hide-details :disabled="disabled" @update:model-value="updateFarm" />
      <v-select v-if="modelValue.whichToFarm === 'Tacet Suppression'" :model-value="modelValue.tacetSuppressionNumber" label="无音区目标编号"
        :items="tacetOptions" density="compact" variant="outlined" hide-details :disabled="disabled" @update:model-value="updateIndex('tacetSuppressionNumber', $event)" />
      <v-text-field v-else-if="modelValue.whichToFarm === 'Forgery Challenge'" :model-value="modelValue.forgeryChallengeNumber" label="锻造挑战目标编号"
        type="number" min="1" density="compact" variant="outlined" hide-details :disabled="disabled" @update:model-value="updateIndex('forgeryChallengeNumber', $event)" />
      <v-select v-else :model-value="modelValue.materialSelection" label="模拟领域材料"
        :items="[{ title: '共鸣者经验', value: 'Resonator EXP' }, { title: '武器经验', value: 'Weapon EXP' }, { title: '贝币', value: 'Shell Credit' }]"
        density="compact" variant="outlined" hide-details :disabled="disabled" @update:model-value="updateMaterial" />
    </div>
    <v-checkbox :model-value="modelValue.farmNightmareNestForDailyEcho" label="通关 1 次梦魇聚落或残象聚落（每日活跃项）"
      density="compact" hide-details :disabled="disabled" @update:model-value="update('farmNightmareNestForDailyEcho', Boolean($event))" />
  </section>
</template>

<style scoped>
.account-profile { display: grid; gap: 12px; }
.account-profile-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 700px) { .account-profile-fields { grid-template-columns: 1fr; } }
</style>

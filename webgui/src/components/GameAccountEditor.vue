<script setup lang="ts">
import type { GameAccount } from '../api/contracts'
import { accountBindingLabel, moveGameAccount } from '../utils/gameAccounts'

const props = defineProps<{ modelValue: GameAccount[]; disabled: boolean; newAccount?: GameAccount }>()
const emit = defineEmits<{ 'update:modelValue': [accounts: GameAccount[]] }>()
function update(index: number, patch: Partial<GameAccount>): void {
  if (props.disabled) return
  emit('update:modelValue', props.modelValue.map((account, i) => i === index ? { ...account, ...patch } : { ...account }))
}
function move(index: number, direction: -1 | 1): void {
  if (props.disabled) return
  emit('update:modelValue', moveGameAccount(props.modelValue, index, direction))
}
function add(): void {
  if (props.disabled) return
  emit('update:modelValue', [...props.modelValue, { ...props.newAccount, label: `账号 ${props.modelValue.length + 1}`, enabled: true, saved_account_label: '' }])
}
</script>

<template>
  <section class="account-editor">
    <div class="account-heading"><strong>鸣潮账号与执行顺序</strong><v-btn size="small" variant="tonal" :disabled="disabled" @click="add">添加账号</v-btn></div>
    <p class="soft-note">同一客户端按下方顺序执行。每个账号分别设置路线、材料和每日步骤；保存后应用于下一次每日。登录绑定只填写官方已记住账号的掩码标签。</p>
    <p class="soft-note">已有执行记录的账号请勿更换登录标签；换号请新增账号并停用旧项。首次配置多账号时，若“当前账号”已有执行记录，请停用它，再分别添加已记住的账号，避免沿用旧号的完成记录。</p>
    <article v-for="(account, index) in modelValue" :key="account.account_id ?? `new-${index}`" class="account-card" :aria-label="account.label || `账号 ${index + 1}`">
      <div class="account-row">
      <v-switch :model-value="account.enabled" :label="`第 ${index + 1} 个`" color="primary" hide-details :disabled="disabled" @update:model-value="update(index, { enabled: Boolean($event) })" />
      <v-text-field :model-value="account.label" label="账号别名" variant="outlined" density="compact" hide-details :disabled="disabled" @update:model-value="update(index, { label: String($event ?? '') })" />
      <v-text-field :model-value="account.saved_account_label" label="官方已记住账号标签（原样填写）" variant="outlined" density="compact" hide-details :disabled="disabled" autocomplete="off" @update:model-value="update(index, { saved_account_label: String($event ?? '') })" />
      <span class="soft-note" role="status">{{ accountBindingLabel(account) }}</span>
      <div class="account-order">
        <v-btn size="small" variant="text" :aria-label="`${account.label}上移`" :disabled="disabled || index === 0" @click="move(index, -1)">上移</v-btn>
        <v-btn size="small" variant="text" :aria-label="`${account.label}下移`" :disabled="disabled || index === modelValue.length - 1" @click="move(index, 1)">下移</v-btn>
      </div>
      </div>
      <slot :account="account" :index="index" />
    </article>
    <p v-if="modelValue.some((account) => account.enabled && !account.saved_account_label.trim())" class="soft-note">待绑定账号不会被视为已能自动切换。多账号执行前，Manager 会检查每个启用账号的标签；登录确认或标签无法唯一匹配时，需要人工处理。</p>
  </section>
</template>

<style scoped>
.account-editor { display: grid; gap: 12px; padding-top: 14px; border-top: 1px solid rgba(142,190,230,.12); }
.account-heading,.account-order { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.account-card { display: grid; gap: 16px; padding: 16px; border: 1px solid rgba(142,190,230,.2); border-radius: 10px; background: rgba(4,14,25,.25); }
.account-row { display: grid; grid-template-columns: 110px minmax(120px,1fr) minmax(210px,1.5fr) 160px auto; align-items: center; gap: 12px; }
@media (max-width: 1100px) { .account-row { grid-template-columns: 1fr 1fr; } }
@media (max-width: 650px) { .account-row { grid-template-columns: 1fr; } }
</style>

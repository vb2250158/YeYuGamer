<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useDisplay } from 'vuetify'
import { navigation } from './router'
import { useManagerStore } from './stores/manager'
import { formatTime, humanize } from './utils/format'
import CommandCenter from './components/CommandCenter.vue'
import StatusBadge from './components/StatusBadge.vue'

const manager = useManagerStore()
const { snapshot, connectionState, managerAvailable, errors, pendingReceiptCount, lastRefreshAt } = storeToRefs(manager)
const { mdAndUp } = useDisplay()
const drawer = ref(true)
const commandsOpen = ref(false)
// A current snapshot makes the Manager control plane usable.  The event stream
// may reconnect independently without blanking the whole application.
const managerReady = computed(() => managerAvailable.value || connectionState.value === 'mock')
const managerGateMessage = computed(() => {
  if (connectionState.value === 'connecting') return '正在连接本机 Manager。连接成功前，不会加载任何游戏、队列或配置页面。'
  if (connectionState.value === 'reconnecting') return 'Manager 连接正在恢复。恢复健康快照前，所有业务页面保持关闭。'
  return 'Manager 未启动或不可达。为避免展示过期状态，所有游戏、队列和配置页面均已关闭。'
})

const groupedNavigation = computed(() => ({
  运行: navigation.filter((item) => item.section === '运行'),
  治理: navigation.filter((item) => item.section === '治理'),
}))

onMounted(() => void manager.bootstrap())
</script>

<template>
  <v-app>
    <v-navigation-drawer v-if="managerReady" v-model="drawer" :permanent="mdAndUp" width="252" class="main-nav">
      <div class="brand-block">
        <div class="brand-mark">雨</div>
        <div>
          <strong>YeYu Gamer</strong>
          <span>Manager Control Plane</span>
        </div>
      </div>

      <nav class="nav-groups" aria-label="主导航">
        <section v-for="(items, section) in groupedNavigation" :key="section">
          <p class="nav-section">{{ section }}</p>
          <RouterLink v-for="item in items" :key="item.path" :to="item.path" class="nav-item">
            <span class="nav-symbol">{{ item.short }}</span>
            <span>{{ item.title }}</span>
          </RouterLink>
        </section>
      </nav>

      <div class="nav-runtime">
        <StatusBadge :state="connectionState" />
        <div><span>State version</span><strong>v{{ snapshot.stateVersion }}</strong></div>
        <div><span>最近同步</span><strong>{{ formatTime(lastRefreshAt) }}</strong></div>
      </div>
    </v-navigation-drawer>

    <v-app-bar v-if="managerReady" flat height="66" class="top-bar">
      <v-btn v-if="!mdAndUp" icon variant="text" aria-label="打开导航" @click="drawer = !drawer">☰</v-btn>
      <div class="top-context">
        <span class="live-line" :class="`is-${connectionState}`" />
        <div>
          <strong>{{ humanize(connectionState) }}</strong>
          <small>{{ snapshot.manager?.version ?? snapshot.manager?.apiVersion ?? '等待 Manager 元数据' }}</small>
        </div>
      </div>
      <v-spacer />
      <v-btn :loading="manager.loading" variant="text" class="top-action" @click="manager.refresh()">
        刷新快照
      </v-btn>
      <v-btn variant="tonal" color="primary" class="top-action" @click="commandsOpen = true">
        命令与错误
        <span v-if="errors.length + pendingReceiptCount" class="button-count">{{ errors.length + pendingReceiptCount }}</span>
      </v-btn>
    </v-app-bar>

    <v-main v-if="!managerReady" class="manager-gate">
      <section class="manager-gate-card">
        <p class="eyebrow">MANAGER REQUIRED</p>
        <h1>等待 Manager</h1>
        <p>{{ managerGateMessage }}</p>
        <v-btn color="primary" :loading="manager.loading" @click="manager.refresh().catch(() => undefined)">重试连接</v-btn>
      </section>
    </v-main>

    <v-main v-else>
      <main class="page-shell">
        <RouterView />
      </main>
    </v-main>

    <CommandCenter v-if="managerReady" v-model="commandsOpen" />
  </v-app>
</template>

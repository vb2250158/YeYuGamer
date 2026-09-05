import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export interface NavigationItem {
  title: string
  short: string
  path: string
  section: '运行' | '治理'
}

export const navigation: NavigationItem[] = [
  { title: '今日总览', short: '今', path: '/', section: '运行' },
  { title: '队列', short: '列', path: '/queue', section: '运行' },
  { title: '游戏详情', short: '游', path: '/games', section: '运行' },
  { title: 'Incident', short: '险', path: '/incidents', section: '运行' },
  { title: '证据', short: '证', path: '/evidence', section: '运行' },
  { title: '通知', short: '信', path: '/notifications', section: '运行' },
  { title: '周常', short: '周', path: '/weekly', section: '运行' },
  { title: 'Agent 复核', short: '核', path: '/agent-workbench', section: '治理' },
  { title: 'Adapter', short: '适', path: '/adapters', section: '治理' },
  { title: '日志诊断', short: '诊', path: '/diagnostics', section: '治理' },
  { title: '设置策略', short: '策', path: '/settings', section: '治理' },
  { title: '系统', short: '系', path: '/system', section: '治理' },
]

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'today', component: () => import('./pages/TodayPage.vue') },
  { path: '/queue', name: 'queue', component: () => import('./pages/QueuePage.vue') },
  { path: '/games/:gameId?', name: 'games', component: () => import('./pages/GameDetailPage.vue') },
  { path: '/incidents', name: 'incidents', component: () => import('./pages/IncidentsPage.vue') },
  { path: '/evidence', name: 'evidence', component: () => import('./pages/EvidencePage.vue') },
  { path: '/notifications', name: 'notifications', component: () => import('./pages/NotificationsPage.vue') },
  { path: '/weekly', name: 'weekly', component: () => import('./pages/WeeklyPage.vue') },
  { path: '/agent-workbench', name: 'agent-workbench', component: () => import('./pages/AgentWorkbenchPage.vue') },
  { path: '/adapters', name: 'adapters', component: () => import('./pages/AdaptersPage.vue') },
  { path: '/diagnostics', name: 'diagnostics', component: () => import('./pages/DiagnosticsPage.vue') },
  { path: '/settings', name: 'settings', component: () => import('./pages/SettingsPage.vue') },
  { path: '/system', name: 'system', component: () => import('./pages/SystemPage.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

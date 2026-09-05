import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'
import './styles/main.css'
import { bootstrapWebGuiSession } from './api/bootstrap'

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: 'nightRain',
    themes: {
      nightRain: {
        dark: true,
        colors: {
          background: '#07111f',
          surface: '#0d1b2d',
          'surface-bright': '#142944',
          primary: '#73c9ff',
          secondary: '#b8a4ff',
          success: '#67ddb2',
          warning: '#ffc66d',
          error: '#ff7b8f',
          info: '#79b8ff',
        },
      },
    },
  },
})

async function start(): Promise<void> {
  try {
    await bootstrapWebGuiSession()
  } catch (error) {
    const root = document.querySelector<HTMLElement>('#app')
    if (root) {
      root.textContent = `WebGUI 安全会话建立失败，请从 YeYu Gamer 托盘重新打开。${error instanceof Error ? ` ${error.message}` : ''}`
    }
    return
  }

  // App imports navigation metadata from router.ts, so both modules must be
  // evaluated only after the one-time fragment has been consumed.
  // createWebHistory snapshots the URL at module creation and can otherwise
  // restore that secret fragment while the application is installed.
  const [{ default: App }, { router }] = await Promise.all([
    import('./App.vue'),
    import('./router'),
  ])

  createApp(App)
    .use(createPinia())
    .use(router)
    .use(vuetify)
    .mount('#app')
}

void start()

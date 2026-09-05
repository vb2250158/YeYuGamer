import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

interface TestLocation {
  hash: string
  pathname: string
  search: string
}

interface TestHistory {
  state: unknown
  replaceState(data: unknown, unused: string, url?: string | URL | null): void
  pushState(data: unknown, unused: string, url?: string | URL | null): void
  back(): void
  forward(): void
}

interface TestBrowser {
  location: TestLocation
  history: TestHistory
  entries: string[]
}

const harness = vi.hoisted(() => ({
  events: [] as string[],
  exchange: vi.fn<(nonce: string) => Promise<void>>(),
  mount: vi.fn(),
  errorRoot: { textContent: '' },
}))

vi.mock('./api/client', () => ({
  managerApi: {
    exchangeWebGuiSession: async (nonce: string): Promise<void> => {
      harness.events.push(`exchange:${nonce}`)
      await harness.exchange(nonce)
    },
  },
}))

vi.mock('vue-router', () => ({
  createWebHistory: () => {
    const { pathname, search, hash } = window.location
    harness.events.push(`router:evaluate:${hash}`)
    return { initialUrl: `${pathname}${search}${hash}` }
  },
  createRouter: ({ history }: { history: { initialUrl: string } }) => ({
    install: () => {
      harness.events.push(`router:install:${window.location.hash}`)
      window.history.replaceState({ router: 'installed' }, '', history.initialUrl)
      window.history.pushState({ route: 'queue' }, '', '/queue')
    },
  }),
}))

vi.mock('vue', () => ({
  createApp: () => {
    const app = {
      use(plugin: { install?: () => void }) {
        plugin.install?.()
        return app
      },
      mount: harness.mount,
    }
    return app
  },
}))

vi.mock('pinia', () => ({ createPinia: () => ({}) }))
vi.mock('vuetify', () => ({ createVuetify: () => ({}) }))
vi.mock('vuetify/components', () => ({}))
vi.mock('vuetify/directives', () => ({}))
vi.mock('vuetify/styles', () => ({}))
vi.mock('./App.vue', () => ({ default: {} }))

function createBrowser(initialUrl: string): TestBrowser {
  const base = 'http://127.0.0.1:8877'
  const entries = ['/previous', initialUrl]
  let index = 1
  let currentState: unknown = null
  const location: TestLocation = { hash: '', pathname: '/', search: '' }

  function applyUrl(url: string | URL): void {
    const parsed = new URL(String(url), base)
    location.pathname = parsed.pathname
    location.search = parsed.search
    location.hash = parsed.hash
  }

  applyUrl(initialUrl)
  const history: TestHistory = {
    get state() {
      return currentState
    },
    set state(value: unknown) {
      currentState = value
    },
    replaceState(data, _unused, url) {
      harness.events.push(`history:replace:${location.hash}`)
      currentState = data
      if (url != null) {
        const parsed = new URL(String(url), base)
        entries[index] = `${parsed.pathname}${parsed.search}${parsed.hash}`
        applyUrl(parsed)
      }
    },
    pushState(data, _unused, url) {
      currentState = data
      if (url == null) return
      const parsed = new URL(String(url), base)
      entries.splice(index + 1)
      entries.push(`${parsed.pathname}${parsed.search}${parsed.hash}`)
      index = entries.length - 1
      applyUrl(parsed)
    },
    back() {
      if (index > 0) index -= 1
      applyUrl(entries[index])
    },
    forward() {
      if (index < entries.length - 1) index += 1
      applyUrl(entries[index])
    },
  }
  return { location, history, entries }
}

async function importMain(): Promise<void> {
  await import('./main')
}

beforeEach(() => {
  vi.resetModules()
  harness.events.length = 0
  harness.exchange.mockReset()
  harness.mount.mockReset()
  harness.errorRoot.textContent = ''
  vi.stubGlobal('document', {
    querySelector: () => harness.errorRoot,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WebGUI bootstrap startup order', () => {
  it('clears the nonce before router evaluation and never restores it through install or traversal', async () => {
    const nonce = 'n'.repeat(43)
    const browser = createBrowser(`/today?view=compact#bootstrap=${nonce}`)
    vi.stubGlobal('window', browser)
    harness.exchange.mockResolvedValue()

    await importMain()
    await vi.waitFor(() => expect(harness.mount).toHaveBeenCalledWith('#app'))

    expect(harness.events).toEqual([
      `history:replace:#bootstrap=${nonce}`,
      `exchange:${nonce}`,
      'router:evaluate:',
      'router:install:',
      'history:replace:',
    ])
    expect(browser.location.hash).toBe('')
    expect(browser.entries.every((entry) => !entry.includes('bootstrap='))).toBe(true)

    browser.history.back()
    expect(browser.location.hash).toBe('')
    browser.history.back()
    expect(browser.location.hash).toBe('')
    browser.history.forward()
    expect(browser.location.hash).toBe('')
    browser.history.forward()
    expect(browser.location.hash).toBe('')
  })

  it('keeps the fragment cleared and never evaluates or installs the router when exchange fails', async () => {
    const nonce = 'f'.repeat(43)
    const browser = createBrowser(`/#bootstrap=${nonce}`)
    vi.stubGlobal('window', browser)
    harness.exchange.mockRejectedValue(new Error('exchange rejected'))

    await importMain()
    await vi.waitFor(() => expect(harness.errorRoot.textContent).toContain('exchange rejected'))

    expect(browser.location.hash).toBe('')
    expect(harness.events).toEqual([
      `history:replace:#bootstrap=${nonce}`,
      `exchange:${nonce}`,
    ])
    expect(harness.mount).not.toHaveBeenCalled()
    expect(browser.entries.every((entry) => !entry.includes('bootstrap='))).toBe(true)
  })
})

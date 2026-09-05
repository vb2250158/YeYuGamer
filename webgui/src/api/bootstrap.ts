import { managerApi, type ManagerApiClient } from './client'

interface BootstrapLocation {
  hash: string
  pathname: string
  search: string
}

interface BootstrapHistory {
  state: unknown
  replaceState(data: unknown, unused: string, url?: string | URL | null): void
}

export interface BootstrapBrowser {
  location: BootstrapLocation
  history: BootstrapHistory
}

export function consumeBootstrapNonce(browser: BootstrapBrowser): string | undefined {
  const rawFragment = browser.location.hash.startsWith('#')
    ? browser.location.hash.slice(1)
    : browser.location.hash
  if (!rawFragment) return undefined

  const nonce = new URLSearchParams(rawFragment).get('bootstrap') ?? undefined
  browser.history.replaceState(
    browser.history.state,
    '',
    `${browser.location.pathname}${browser.location.search}`,
  )
  if (!nonce || !/^[A-Za-z0-9_-]{40,128}$/.test(nonce)) {
    throw new Error('WebGUI bootstrap fragment 格式无效')
  }
  return nonce
}

export async function bootstrapWebGuiSession(
  client: ManagerApiClient = managerApi,
  browser: BootstrapBrowser = window,
): Promise<boolean> {
  const nonce = consumeBootstrapNonce(browser)
  if (nonce) {
    await client.exchangeWebGuiSession(nonce)
    return true
  }
  await client.establishLocalWebGuiSession()
  return true
}

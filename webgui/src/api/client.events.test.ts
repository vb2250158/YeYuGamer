import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { managerApi, rootApi } from './client'
import { useManagerStore } from '../stores/manager'

const sources: TestEventSource[] = []
class TestEventSource extends EventTarget {
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()

  constructor() {
    super()
    sources.push(this)
  }

  emit(type: string, stateVersion: number, eventId = `event-${stateVersion}`): void {
    this.dispatchEvent(new MessageEvent(type, {
      data: JSON.stringify({ eventId, type, stateVersion, cursor: String(stateVersion), payload: {} }),
      lastEventId: String(stateVersion),
    }))
  }
}

let manager: ReturnType<typeof useManagerStore> | undefined
beforeEach(() => {
  vi.useFakeTimers()
  sources.length = 0
  vi.stubGlobal('EventSource', TestEventSource)
  vi.stubGlobal('window', {
    location: { origin: 'http://localhost' },
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })
  setActivePinia(createPinia())
  vi.spyOn(rootApi, 'get').mockResolvedValue({ paths: {} })
})

afterEach(() => {
  manager?.$dispose()
  manager = undefined
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('named cleanup SSE events refresh Manager snapshots', () => {
  it.each(['game-launch.queue-cleanup', 'game-run.queue-cleanup'])(
    '%s reaches the real store and refreshes its snapshot without reconnecting', async (type) => {
      const snapshots = vi.spyOn(managerApi, 'get').mockResolvedValue({ stateVersion: 10, games: [] })
      manager = useManagerStore()
      await manager.bootstrap()
      sources[0]!.onopen?.()
      snapshots.mockResolvedValue({ stateVersion: 12, games: [] })

      sources[0]!.emit(type, 12)
      expect(manager.events[0]?.type).toBe(type)
      expect(manager.snapshot.stateVersion).toBe(10)
      await vi.advanceTimersByTimeAsync(500)

      expect(snapshots).toHaveBeenCalledTimes(2)
      expect(manager.snapshot.stateVersion).toBe(12)
      expect(manager.connectionState).toBe('connected')
      expect(sources).toHaveLength(1)
    },
  )

  it('does not regress or refetch the snapshot for an older cleanup event', async () => {
    const snapshots = vi.spyOn(managerApi, 'get').mockResolvedValue({ stateVersion: 12, games: [] })
    manager = useManagerStore()
    await manager.bootstrap()
    sources[0]!.onopen?.()
    sources[0]!.emit('game-launch.queue-cleanup', 11)
    await vi.advanceTimersByTimeAsync(500)
    expect(manager.snapshot.stateVersion).toBe(12)
    expect(snapshots).toHaveBeenCalledTimes(1)
  })
})

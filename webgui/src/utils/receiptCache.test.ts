import { describe, expect, it } from 'vitest'
import {
  readReceiptCache,
  receiptCacheKey,
  writeReceiptCache,
} from './receiptCache'
import type { ReceiptStorage } from './receiptCache'

class MemoryStorage implements ReceiptStorage {
  readonly values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  serializeAll(): string {
    return JSON.stringify([...this.values.entries()])
  }
}

const sensitiveReceipt = {
  commandId: 'cmd-agent-1',
  idempotencyKey: 'idem-never-persist',
  requestId: 'request-display-1',
  statusUrl: '/commands/cmd-agent-1',
  acceptedStateVersion: 23,
  state: 'accepted',
  message: 'Manager 已受理 Agent 命令',
  submittedAt: '2026-08-28T12:34:56.000Z',
  result: {
    claim: { claimId: 'claim-private-1', fencingToken: 'mock-super-secret-fencing-value' },
    secret: 'mock-super-secret-result-value',
  },
  fencingToken: 'mock-super-secret-root-value',
  originalResponse: { result: 'must-not-survive' },
}

describe('receipt cache', () => {
  it('persists only the display summary and never the raw command response', () => {
    const storage = new MemoryStorage()

    writeReceiptCache([sensitiveReceipt], storage)

    expect(JSON.parse(storage.getItem(receiptCacheKey) ?? 'null')).toEqual([{
      commandId: 'cmd-agent-1',
      state: 'accepted',
      requestId: 'request-display-1',
      statusUrl: '/commands/cmd-agent-1',
      acceptedStateVersion: 23,
      message: 'Manager 已受理 Agent 命令',
      submittedAt: '2026-08-28T12:34:56.000Z',
    }])
    expect(storage.serializeAll()).not.toContain('fencingToken')
    expect(storage.serializeAll()).not.toContain('mock-super-secret')
    expect(storage.serializeAll()).not.toContain('"result"')
    expect(storage.serializeAll()).not.toContain('claim-private-1')
  })

  it('scrubs an old raw cache in place and keeps its summary usable after reload', () => {
    const storage = new MemoryStorage()
    storage.setItem(receiptCacheKey, JSON.stringify([sensitiveReceipt]))

    const firstLoad = readReceiptCache(storage)
    const secondLoad = readReceiptCache(storage)

    expect(secondLoad).toEqual(firstLoad)
    expect(secondLoad[0]).toMatchObject({
      commandId: 'cmd-agent-1',
      state: 'accepted',
      requestId: 'request-display-1',
      acceptedStateVersion: 23,
      message: 'Manager 已受理 Agent 命令',
    })
    expect(storage.serializeAll()).not.toContain('fencingToken')
    expect(storage.serializeAll()).not.toContain('mock-super-secret')
    expect(storage.serializeAll()).not.toContain('"result"')
  })

  it('drops credential-bearing messages and status URLs while retaining safe request identity', () => {
    const storage = new MemoryStorage()

    writeReceiptCache([{
      ...sensitiveReceipt,
      message: 'Bearer mock-super-secret-token-value must not persist',
      statusUrl: '/commands/cmd-agent-1?token=mock-super-secret-token-value',
    }], storage)

    expect(JSON.parse(storage.getItem(receiptCacheKey) ?? 'null')).toEqual([{
      commandId: 'cmd-agent-1',
      state: 'accepted',
      requestId: 'request-display-1',
      acceptedStateVersion: 23,
      message: 'Manager 已受理命令；完成状态以后续事件为准。',
      submittedAt: '2026-08-28T12:34:56.000Z',
    }])
    expect(storage.serializeAll()).not.toContain('idem-never-persist')
    expect(storage.serializeAll()).not.toContain('mock-super-secret')
  })

  it('keeps a safe Manager failure reason and its terminal timestamp', () => {
    const storage = new MemoryStorage()

    writeReceiptCache([{
      commandId: 'batch-failed-1',
      idempotencyKey: 'idem-private',
      requestId: 'req-failed-1',
      state: 'failed',
      message: 'batch sealed without starting Host because no Todo matched a verified promoted binding',
      submittedAt: '2026-09-05T02:05:26.000+08:00',
      completedAt: '2026-09-05T02:05:27.200+08:00',
    }], storage)

    expect(readReceiptCache(storage)[0]).toMatchObject({
      commandId: 'batch-failed-1',
      requestId: 'req-failed-1',
      state: 'failed',
      message: 'batch sealed without starting Host because no Todo matched a verified promoted binding',
      completedAt: '2026-09-05T02:05:27.200+08:00',
    })
  })
})

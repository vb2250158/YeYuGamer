import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { receiptCacheKey } from '../utils/receiptCache'
import type { ReceiptStorage } from '../utils/receiptCache'

const harness = vi.hoisted(() => ({
  command: vi.fn(),
}))

vi.mock('../api/client', () => ({
  ApiError: class TestApiError extends Error {
    status: number
    requestId = 'test-request'
    problem: { code?: string }

    constructor(status = 500, problem: { code?: string } = { code: 'unknown' }, message = '') {
      super(message)
      this.status = status
      this.problem = problem
    }
  },
  createRequestId: vi.fn(() => 'req-generated-once'),
  getInitialSnapshot: vi.fn(),
  isMockMode: true,
  managerApi: {
    command: harness.command,
    getReceipt: vi.fn(),
  },
  ManagerEventStream: class TestManagerEventStream {},
  rootApi: { get: vi.fn() },
}))

import { ApiError } from '../api/client'
import { useManagerStore } from './manager'

class MemoryStorage implements ReceiptStorage {
  readonly values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

const browserStub = {
  setTimeout: vi.fn(() => 1),
  clearTimeout: vi.fn(),
  setInterval: vi.fn(() => 2),
  clearInterval: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
}

beforeEach(() => {
  harness.command.mockReset()
  browserStub.setTimeout.mockReset().mockReturnValue(1)
  vi.stubGlobal('window', browserStub)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('manager command receipt boundary', () => {
  it('returns the full receipt to the live claim flow but reloads only its safe summary', async () => {
    const storage = new MemoryStorage()
    vi.stubGlobal('localStorage', storage)
    const rawReceipt = {
      commandId: 'cmd-claim-1',
      idempotencyKey: 'idem-private',
      requestId: 'request-private',
      statusUrl: '/commands/cmd-claim-1',
      state: 'succeeded',
      message: '能力调用已完成',
      submittedAt: '2026-08-28T13:00:00.000Z',
      result: {
        claimId: 'claim-private-1',
        fencingToken: 'mock-super-secret-fencing-value',
        capabilityResult: { ok: true },
      },
    }
    harness.command.mockResolvedValue(rawReceipt)
    setActivePinia(createPinia())
    const manager = useManagerStore()

    const returned = await manager.submitCommand('调用能力', 'POST', '/capability-invocations', {})

    expect(returned).toBe(rawReceipt)
    expect(returned?.result).toEqual(rawReceipt.result)
    expect(manager.receipts).toEqual([{
      commandId: 'cmd-claim-1',
      state: 'succeeded',
      requestId: 'request-private',
      statusUrl: '/commands/cmd-claim-1',
      message: '能力调用已完成',
      submittedAt: '2026-08-28T13:00:00.000Z',
    }])
    const persisted = storage.getItem(receiptCacheKey) ?? ''
    expect(persisted).not.toContain('fencingToken')
    expect(persisted).not.toContain('mock-super-secret')
    expect(persisted).not.toContain('"result"')

    setActivePinia(createPinia())
    const reloadedManager = useManagerStore()
    expect(reloadedManager.receipts).toEqual(manager.receipts)
  })

  it('replays a timed-out command once with the original idempotency boundary', async () => {
    const storage = new MemoryStorage()
    vi.stubGlobal('localStorage', storage)
    const timeout = new ApiError(0, { code: 'manager_response_timeout' }, 'Manager response timed out')
    harness.command
      .mockRejectedValueOnce(timeout)
      .mockResolvedValueOnce({
        commandId: 'batch-recovered',
        idempotencyKey: 'idem-recovered',
        requestId: 'req-recovered',
        statusUrl: '/commands/batch-recovered',
        state: 'accepted',
        submittedAt: '2026-08-30T00:00:00.000Z',
      })
    browserStub.setTimeout.mockImplementationOnce(((callback: TimerHandler) => {
      if (typeof callback === 'function') callback()
      return 1
    }) as never)
    setActivePinia(createPinia())
    const manager = useManagerStore()

    const returned = await manager.submitCommand('执行今日队列', 'POST', '/batches', { cadence: 'daily' })

    expect(returned?.commandId).toBe('batch-recovered')
    expect(harness.command).toHaveBeenCalledTimes(2)
    expect(harness.command.mock.calls[1][3]).toEqual(harness.command.mock.calls[0][3])
    expect(harness.command.mock.calls[0][3]).toMatchObject({
      requestId: 'req-generated-once',
      idempotencyKey: expect.stringMatching(/^webgui-v0-/),
    })
    expect(manager.errors).toEqual([])
  })

  it('reports an unknown outcome after both attempts time out and keeps the retry identity', async () => {
    const timeout = new ApiError(0, { code: 'manager_response_timeout' }, 'Manager response timed out')
    harness.command.mockRejectedValue(timeout)
    browserStub.setTimeout.mockImplementationOnce(((callback: TimerHandler) => {
      if (typeof callback === 'function') callback()
      return 1
    }) as never)
    setActivePinia(createPinia())
    const manager = useManagerStore()

    const returned = await manager.submitCommand('执行今日队列', 'POST', '/batches', { cadence: 'daily' })

    expect(returned).toBeUndefined()
    expect(harness.command).toHaveBeenCalledTimes(2)
    expect(harness.command.mock.calls[1][3]).toEqual(harness.command.mock.calls[0][3])
    expect(manager.errors[0]).toMatchObject({
      title: '执行今日队列结果未知',
      requestId: 'req-generated-once',
      idempotencyKey: expect.stringMatching(/^webgui-v0-/),
      detail: expect.stringContaining('当前结果未知'),
      nextAction: expect.stringContaining('不要因为 activeBatch 为空就直接重试'),
    })
  })

  it('never renders an absent command rejection as undefined', async () => {
    harness.command.mockRejectedValueOnce(undefined)
    setActivePinia(createPinia())
    const manager = useManagerStore()

    const returned = await manager.submitCommand('停止遗留运行', 'POST', '/batches/batch-1/cancel-requests', {})

    expect(returned).toBeUndefined()
    expect(manager.errors[0]).toMatchObject({
      title: '停止遗留运行失败',
      detail: expect.stringContaining('没有收到这次请求的错误详情'),
      nextAction: expect.stringContaining('刷新快照'),
    })
    expect(manager.errors[0]?.detail).not.toBe('undefined')
  })
})

import { describe, expect, it, vi } from 'vitest'
import { ManagerApiClient, managerEventTypes } from './client'
import { extractItems } from './contracts'

describe('ManagerApiClient', () => {
  it('sends command identity and concurrency headers', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers)
      expect(headers.get('Idempotency-Key')).toBe('idem-fixed')
      expect(headers.get('X-Request-Id')).toBe('req-fixed')
      expect(headers.get('If-Match')).toBe('"42"')
      expect(headers.get('X-Expected-State-Version')).toBe('42')
      return new Response(JSON.stringify({ commandId: 'cmd-1', state: 'accepted' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      })
    })
    const client = new ManagerApiClient('/api/v1', fetcher)
    const receipt = await client.command('POST', '/batches', { trigger: 'manual' }, {
      idempotencyKey: 'idem-fixed',
      requestId: 'req-fixed',
      expectedStateVersion: 42,
    })
    expect(receipt.commandId).toBe('cmd-1')
    expect(receipt.state).toBe('accepted')
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('exchanges a bootstrap nonce without inventing a command receipt', async () => {
    const nonce = 'n'.repeat(43)
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe('POST')
      expect(init?.credentials).toBe('same-origin')
      expect(JSON.parse(String(init?.body))).toEqual({ nonce })
      return new Response(JSON.stringify({ authenticated: true, actor: 'webgui' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    const client = new ManagerApiClient('/api/v1', fetcher)
    await client.exchangeWebGuiSession(nonce)
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('establishes a local WebGUI session without a tray nonce', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('/api/v1/webgui/local-sessions')
      expect(init?.method).toBe('POST')
      expect(init?.credentials).toBe('same-origin')
      return new Response(JSON.stringify({ authenticated: true, actor: 'webgui' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    const client = new ManagerApiClient('/api/v1', fetcher)
    await client.establishLocalWebGuiSession()
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('submits the multi-Todo claim decision contract as todoDiagnoses', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe('POST')
      const body = JSON.parse(String(init?.body))
      expect(body.todoDiagnoses).toEqual([
        expect.objectContaining({ todoInstanceId: 'todo-1', automatable: true, issue: 'navigation drift' }),
        expect.objectContaining({ todoInstanceId: 'todo-2', automatable: false, issue: 'login gate' }),
      ])
      expect(body).not.toHaveProperty('todoDiagnosis')
      return new Response(JSON.stringify({ commandId: 'cmd-diagnoses', state: 'accepted' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      })
    })
    const client = new ManagerApiClient('/api/v1', fetcher)
    await client.submitClaimDecision({
      claimId: 'claim-1',
      fencingToken: 'f'.repeat(32),
      decision: 'review_required',
      reason: 'two Todo diagnoses recorded',
      evidenceIds: [],
      requestedBy: 'webgui',
      todoDiagnoses: [
        {
          todoInstanceId: 'todo-1', difficulty: 'moderate', automatable: true, confidence: 0.8,
          basis: ['latest attempt blocked'], failureStage: 'navigation', issue: 'navigation drift', recommendation: 'capture frame', evidenceIds: [],
        },
        {
          todoInstanceId: 'todo-2', difficulty: 'unsupported', automatable: false, confidence: 1,
          basis: ['active blocker'], failureStage: 'login', issue: 'login gate', recommendation: 'wait for release', evidenceIds: [],
        },
      ],
    }, { idempotencyKey: 'idem-multi', expectedStateVersion: 42 })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('normalizes list envelopes without inventing records', () => {
    expect(extractItems([{ id: 1 }])).toEqual([{ id: 1 }])
    expect(extractItems({ items: [{ id: 2 }] })).toEqual([{ id: 2 }])
    expect(extractItems(null)).toEqual([])
  })

  it('surfaces Manager error envelopes with the request id', async () => {
    const client = new ManagerApiClient('/api/v1', async () => new Response(JSON.stringify({
      error: { code: 'state_version_conflict', message: 'snapshot is stale', details: { expected: 44 } },
    }), {
      status: 409,
      headers: { 'content-type': 'application/json', 'x-request-id': 'req-conflict' },
    }))
    await expect(client.get('/snapshot')).rejects.toMatchObject({
      status: 409,
      message: 'snapshot is stale',
      requestId: 'req-conflict',
    })
  })

  it('explains a timed-out request as an unconfirmed Manager outcome', async () => {
    vi.useFakeTimers()
    try {
      const fetcher = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
      }))
      const client = new ManagerApiClient('/api/v1', fetcher)
      const pending = client.get('/snapshot')
      const rejected = pending.then(
        () => undefined,
        (error: unknown) => error,
      )
      await vi.advanceTimersByTimeAsync(5_000)
      expect(await rejected).toMatchObject({
        status: 0,
        requestId: undefined,
        problem: expect.objectContaining({
          code: 'manager_response_timeout',
          detail: 'Manager 在 5 秒内没有返回受理结果',
        }),
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('subscribes to durable resource events used by governance pages', () => {
    expect(managerEventTypes).toEqual(expect.arrayContaining([
      'repair-session.updated',
      'adapter-diagnostic-canary.created',
      'diagnostic-bundle.created',
      'artifact.created',
      'todo.catalog-synced',
      'todo.reconciled',
      'todo.reset-reconciled',
      'todo.status-transitioned',
      'batch.sealed',
      'notification.policy.updated',
      'notification.created',
      'notification.sending',
      'notification.sent',
      'notification.failed',
      'notification.gate.changed',
      'notification.dispatch.requested',
      'command.receipt',
    ]))
  })
})

import type {
  ApiProblem,
  ClaimDecisionCreateRequest,
  CommandOptions,
  CommandReceipt,
  ManagerEvent,
  ManagerSnapshot,
} from './contracts'

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

const normalizeBase = (value: string) => value.replace(/\/$/, '')
const defaultBase = '/api/v1'
// A local Manager normally accepts a command in well under a second.  Keep an
// individual attempt short so a stopped/restarting Manager never leaves the
// primary action button spinning for a quarter of a minute.  Mutations that
// time out are retried once by the store with their original request identity.
export const managerRequestTimeoutMs = 5_000
export const managerSnapshotTimeoutMs = 20_000

export const isMockMode = import.meta.env.DEV && import.meta.env.VITE_ENABLE_MOCK === 'true'

async function devMockResponse<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (!import.meta.env.DEV) throw new Error('Mock transport is unavailable in production')
  const fixture = await import('../mocks/fixtures')
  return fixture.createMockResponse<T>(method, path, body)
}

export class ApiError extends Error {
  readonly status: number
  readonly problem: ApiProblem
  readonly requestId?: string

  constructor(status: number, problem: ApiProblem, fallback: string) {
    super(problem.detail || problem.title || fallback)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
    this.requestId = problem.requestId
  }
}

function newId(prefix: string): string {
  const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${prefix}_${id}`
}

export function createRequestId(): string {
  return newId('req')
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('json')) return response.json()
  const text = await response.text()
  return text ? { detail: text } : null
}

function normalizeProblem(payload: unknown): ApiProblem {
  if (!payload || typeof payload !== 'object') return {}
  const outer = payload as Record<string, unknown>
  const nested = outer.error
  if (nested && typeof nested === 'object') {
    const error = nested as Record<string, unknown>
    return {
      ...outer,
      code: typeof error.code === 'string' ? error.code : undefined,
      title: typeof error.code === 'string' ? error.code : undefined,
      detail: typeof error.message === 'string' ? error.message : undefined,
      errors: error.details,
    }
  }
  return outer as ApiProblem
}

function csrfToken(): string | undefined {
  if (typeof document === 'undefined') return undefined
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content
  if (meta) return meta
  const cookie = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith('yeyu_csrf='))
  return cookie ? decodeURIComponent(cookie.slice('yeyu_csrf='.length)) : undefined
}

export class ManagerApiClient {
  readonly baseUrl: string
  private readonly fetchImpl: FetchLike

  constructor(baseUrl = defaultBase, fetchImpl: FetchLike = globalThis.fetch.bind(globalThis)) {
    this.baseUrl = normalizeBase(baseUrl)
    this.fetchImpl = fetchImpl
  }

  async get<T>(path: string, signal?: AbortSignal, timeoutMs = managerRequestTimeoutMs): Promise<T> {
    return this.request<T>('GET', path, undefined, { signal, timeoutMs })
  }

  async exchangeWebGuiSession(nonce: string): Promise<void> {
    if (!/^[A-Za-z0-9_-]{40,128}$/.test(nonce)) {
      throw new Error('WebGUI bootstrap nonce 格式无效')
    }
    await this.request<{ authenticated: boolean; actor: string }>(
      'POST',
      '/webgui/session-exchanges',
      { nonce },
    )
  }

  async establishLocalWebGuiSession(): Promise<void> {
    await this.request<{ authenticated: boolean; actor: string }>(
      'POST',
      '/webgui/local-sessions',
    )
  }

  async command<TBody extends object | undefined = Record<string, unknown>>(
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    path: string,
    body?: TBody,
    options: CommandOptions = {},
  ): Promise<CommandReceipt> {
    const requestId = options.requestId ?? createRequestId()
    const idempotencyKey = options.idempotencyKey ?? newId('idem')
    const result = await this.request<Partial<CommandReceipt>>(method, path, body, {
      requestId,
      idempotencyKey,
      expectedStateVersion: options.expectedStateVersion,
      headers: options.headers,
    })
    return {
      commandId: result.commandId ?? newId('command'),
      idempotencyKey: result.idempotencyKey ?? idempotencyKey,
      requestId: result.requestId ?? requestId,
      statusUrl: result.statusUrl,
      acceptedStateVersion: result.acceptedStateVersion,
      state: result.state ?? 'accepted',
      message: result.message,
      result: result.result,
      submittedAt: result.submittedAt ?? new Date().toISOString(),
      completedAt: result.completedAt,
    }
  }

  async submitClaimDecision(
    request: ClaimDecisionCreateRequest,
    options: CommandOptions = {},
  ): Promise<CommandReceipt> {
    return this.command('POST', '/claims/decisions', request, options)
  }

  async getReceipt(statusUrl: string, signal?: AbortSignal): Promise<CommandReceipt> {
    const path = statusUrl.startsWith(this.baseUrl) ? statusUrl.slice(this.baseUrl.length) : statusUrl
    return this.get<CommandReceipt>(path, signal)
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: {
      signal?: AbortSignal
      requestId?: string
      idempotencyKey?: string
      expectedStateVersion?: number
      headers?: Record<string, string>
      timeoutMs?: number
    } = {},
  ): Promise<T> {
    const url = path.startsWith('http://') || path.startsWith('https://')
      ? path
      : `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`
    if (isMockMode) return devMockResponse<T>(method, path, body)

    const headers = new Headers(options.headers)
    headers.set('Accept', 'application/json')
    if (body !== undefined) headers.set('Content-Type', 'application/json')
    if (options.requestId) headers.set('X-Request-Id', options.requestId)
    if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey)
    if (options.expectedStateVersion !== undefined) {
      headers.set('If-Match', `"${options.expectedStateVersion}"`)
      headers.set('X-Expected-State-Version', String(options.expectedStateVersion))
    }
    const csrf = csrfToken()
    if (csrf && method !== 'GET') headers.set('X-CSRF-Token', csrf)

    const controller = new AbortController()
    let timedOut = false
    const abortFromCaller = () => controller.abort()
    options.signal?.addEventListener('abort', abortFromCaller, { once: true })
    const timeoutMs = options.timeoutMs ?? managerRequestTimeoutMs
    const timeout = globalThis.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeoutMs)

    let response: Response
    try {
      response = await this.fetchImpl(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      })
    } catch (error) {
      if (timedOut) {
        throw new ApiError(
          0,
          {
            code: 'manager_response_timeout',
            title: 'Manager 响应超时',
            detail: `Manager 在 ${timeoutMs / 1000} 秒内没有返回受理结果`,
            requestId: options.requestId,
          },
          'Manager 响应超时',
        )
      }
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      throw new ApiError(0, { title: 'Manager 不可达', detail: String(error) }, 'Manager 不可达')
    } finally {
      globalThis.clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abortFromCaller)
    }

    const payload = await parseBody(response)
    if (!response.ok) {
      const problem = normalizeProblem(payload)
      problem.requestId ??= response.headers.get('x-request-id') ?? undefined
      throw new ApiError(response.status, problem, `HTTP ${response.status}`)
    }
    return payload as T
  }
}

export interface EventStreamCallbacks {
  onEvent: (event: ManagerEvent) => void
  onState: (state: 'connected' | 'reconnecting' | 'offline') => void
  onCursorExpired: () => void
}

export const managerEventTypes = [
  'manager.started',
  'manager.ledger-maintained',
  'manager.stop-requested',
  'manager.restart-requested',
  'legacy.imported',
  'config.updated',
  'todo.catalog-synced',
  'todo.reconciled',
  'todo.reset-reconciled',
  'todo.status-transitioned',
  'batch.created',
  'batch.updated',
  'batch.sealed',
  'game-run.created',
  'game-run.updated',
  'game-launch.phase',
  'game-launch.zombie-detected',
  'manager.zombie-reap',
  'todo-step-capture.failed',
  'agent-work-item.created',
  'agent-work-item.updated',
  'work-item.claimed',
  'work-item.claim-renewed',
  'work-item.claim-resolved',
  'claim-decision.created',
  'capability-invocation.created',
  'capability-request.created',
  'artifact.created',
  'artifact.updated',
  'evidence-review.created',
  'repair-session.created',
  'repair-session.updated',
  'repair-verification.created',
  'adapter-diagnostic-canary.created',
  'adapter-governance-request.created',
  'diagnostic-bundle.created',
  'run-control-request.created',
  'notification.policy.updated',
  'notification.created',
  'notification.sending',
  'notification.sent',
  'notification.failed',
  'notification.gate.changed',
  'notification.dispatch.requested',
  'config-validation.created',
  'state.changed',
  'command.receipt',
  'log',
  'snapshot.invalidated',
  'cursor.expired',
] as const

export class ManagerEventStream {
  private source?: EventSource
  private reconnectTimer?: number
  private attempts = 0
  private stopped = true
  private cursor?: string

  constructor(
    private readonly baseUrl: string,
    private readonly callbacks: EventStreamCallbacks,
  ) {}

  start(cursor?: string): void {
    this.stop()
    this.stopped = false
    this.cursor = cursor
    if (isMockMode) {
      this.callbacks.onState('connected')
      return
    }
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.source?.close()
    this.source = undefined
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = undefined
  }

  private connect(): void {
    if (this.stopped) return
    this.callbacks.onState(this.attempts ? 'reconnecting' : 'offline')
    const url = new URL(`${normalizeBase(this.baseUrl)}/events/stream`, window.location.origin)
    if (this.cursor) url.searchParams.set('after', this.cursor)
    this.source = new EventSource(url, { withCredentials: true })

    this.source.onopen = () => {
      this.attempts = 0
      this.callbacks.onState('connected')
    }
    const consume = (raw: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(raw.data) as Partial<ManagerEvent>
        const event = {
          eventId: parsed.eventId ?? raw.lastEventId ?? newId('event'),
          type: parsed.type ?? raw.type ?? 'message',
          occurredAt: parsed.occurredAt,
          stateVersion: parsed.stateVersion,
          cursor: parsed.cursor ?? raw.lastEventId,
          payload: (parsed.payload ?? {}) as Record<string, unknown>,
        }
        if (event.cursor) this.cursor = event.cursor
        this.callbacks.onEvent(event)
      } catch {
        this.callbacks.onEvent({
          eventId: raw.lastEventId || newId('event'),
          type: raw.type || 'message',
          payload: { message: raw.data },
        })
      }
    }
    this.source.onmessage = consume
    managerEventTypes.forEach((name) => {
      this.source?.addEventListener(name, (event) => {
        if (name === 'cursor.expired') this.callbacks.onCursorExpired()
        else consume(event as MessageEvent<string>)
      })
    })
    this.source.onerror = () => {
      this.source?.close()
      this.callbacks.onState('reconnecting')
      this.attempts += 1
      const delay = Math.min(30_000, 1_000 * 2 ** Math.min(this.attempts, 5))
      this.reconnectTimer = window.setTimeout(() => this.connect(), delay)
    }
  }
}

export const managerApi = new ManagerApiClient()
export const rootApi = new ManagerApiClient('')

export async function getInitialSnapshot(): Promise<ManagerSnapshot> {
  if (isMockMode) {
    const fixture = await import('../mocks/fixtures')
    return fixture.mockSnapshot()
  }
  return managerApi.get<ManagerSnapshot>('/snapshot', undefined, managerSnapshotTimeoutMs)
}

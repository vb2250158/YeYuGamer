import { computed, onScopeDispose, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import {
  ApiError,
  createRequestId,
  getInitialSnapshot,
  isMockMode,
  managerApi,
  ManagerEventStream,
  rootApi,
} from '../api/client'
import type {
  CommandOptions,
  CommandReceipt,
  ConnectionState,
  ManagerEvent,
  ManagerSnapshot,
} from '../api/contracts'
import { stateBoundIdempotencyKey } from '../utils/managerResources'
import {
  decodeReceiptCache,
  readReceiptCache,
  receiptCacheKey,
  summarizeCommandReceipt,
  writeReceiptCache,
} from '../utils/receiptCache'
import type { CommandReceiptSummary } from '../utils/receiptCache'

const emptySnapshot = (): ManagerSnapshot => ({
  stateVersion: 0,
  generatedAt: undefined,
  eventCursor: undefined,
  activeBatch: null,
  recentBatches: [],
  games: [],
  counters: {},
})

export interface VisibleError {
  id: string
  title: string
  detail: string
  nextAction?: string
  status?: number
  requestId?: string
  idempotencyKey?: string
  occurredAt: string
}

const missingCommandErrorDetail = '页面没有收到这次请求的错误详情。通常是浏览器与本机 Manager 在返回前断开，或请求被本地运行时中止；请先刷新快照，确认当前批次状态后再操作。'

function readableErrorDetail(error: unknown): string | undefined {
  if (typeof error === 'string' && error.trim()) return error.trim()
  if (error instanceof Error && error.message.trim() && error.message !== 'undefined') return error.message.trim()
  return undefined
}

export const useManagerStore = defineStore('manager', () => {
  const snapshot = shallowRef<ManagerSnapshot>(emptySnapshot())
  const connectionState = ref<ConnectionState>('connecting')
  // Snapshot availability and the SSE transport are separate facts.  A
  // reconnecting event stream must not tear down a UI that has already read a
  // current Manager snapshot successfully.
  const managerAvailable = ref(false)
  const loading = ref(false)
  const initialized = ref(false)
  const events = ref<ManagerEvent[]>([])
  const receipts = ref<CommandReceiptSummary[]>(readReceiptCache())
  const errors = ref<VisibleError[]>([])
  const apiOperations = ref<Record<string, string[]>>({})
  const lastRefreshAt = ref<string>()
  let stream: ManagerEventStream | undefined
  let refreshTimer: number | undefined
  let fallbackPollTimer: number | undefined

  const stateVersion = computed(() => snapshot.value.stateVersion)
  const activeBatch = computed(() => snapshot.value.activeBatch ?? null)
  const pendingReceiptCount = computed(() => receipts.value.filter((receipt) => receipt.state === 'running').length)
  const activeControllerLeaseCount = computed(() => Number(snapshot.value.executionControl?.activeControllerLeaseCount ?? 0))
  const activeTodoBlockerCount = computed(() => Number(snapshot.value.executionControl?.activeTodoBlockerCount ?? 0))
  const runningGameCount = computed(() => snapshot.value.games.filter((game) => (
    ['starting', 'running', 'cancelling'].includes(game.runtimeState)
  )).length)
  // A Batch is not the only durable representation of a live desktop claim.
  // Keep every execution entry point behind the same Manager-derived fact so a
  // stale/reconciling Batch cannot launch a competing game run.
  const executionInProgress = computed(() => (
    Boolean(activeBatch.value)
    || activeControllerLeaseCount.value > 0
    || runningGameCount.value > 0
  ))
  const executionBusyReason = computed(() => {
    if (activeBatch.value) return 'Manager 正在执行一个批次；请先等待完成或使用“请求安全取消”。'
    if (activeControllerLeaseCount.value > 0) {
      return `Manager 仍持有 ${activeControllerLeaseCount.value} 个桌面控制租约；请先停止或完成遗留运行。`
    }
    if (runningGameCount.value > 0) return `Manager 正在记录 ${runningGameCount.value} 个游戏运行。`
    return undefined
  })

  function explainError(error: unknown): Pick<VisibleError, 'detail' | 'nextAction'> {
    if (!(error instanceof ApiError)) {
      return {
        detail: readableErrorDetail(error) ?? missingCommandErrorDetail,
        nextAction: readableErrorDetail(error) ? undefined : '刷新快照；若批次仍在运行，再提交一次停止请求。',
      }
    }
    if (error.problem.code === 'manager_storage_unavailable') {
      return {
        detail: 'Manager 的本地状态库暂时不可用（SQLite 访问错误），因此无法读取或提交这次请求。页面没有收到受理回执，不能确认批次是否已创建。',
        nextAction: '等待 Manager 恢复后刷新快照；确认没有活动批次再重新执行。',
      }
    }
    if (error.problem.code === 'manager_response_timeout') {
      return {
        detail: 'Manager 连续两次都没有在 5 秒内返回可解析的受理回执。请求可能已经提交，当前结果未知；两次尝试使用了同一请求号和幂等键。',
        nextAction: '不要因为 activeBatch 为空就直接重试。请刷新快照并在“命令与错误”按原请求号和幂等键查询；只有确认原命令未创建后，才提交新的请求。',
      }
    }
    return {
      detail: readableErrorDetail(error) ?? missingCommandErrorDetail,
      nextAction: error.status === 412
        ? '配置或队列状态已变化。页面会刷新快照；确认选择后再提交。'
        : undefined,
    }
  }

  function rememberError(
    title: string,
    error: unknown,
    identity: { requestId?: string; idempotencyKey?: string } = {},
  ): void {
    const apiError = error instanceof ApiError ? error : undefined
    const explanation = explainError(error)
    errors.value.unshift({
      id: globalThis.crypto?.randomUUID?.() ?? String(Date.now()),
      title,
      detail: explanation.detail,
      nextAction: explanation.nextAction,
      status: apiError?.status,
      requestId: identity.requestId ?? apiError?.requestId,
      idempotencyKey: identity.idempotencyKey,
      occurredAt: new Date().toISOString(),
    })
    errors.value = errors.value.slice(0, 20)
  }

  async function refresh(options: { quiet?: boolean } = {}): Promise<void> {
    if (!options.quiet) loading.value = true
    try {
      const next = await getInitialSnapshot()
      if (next.stateVersion >= snapshot.value.stateVersion || snapshot.value.stateVersion === 0) {
        snapshot.value = { ...emptySnapshot(), ...next, games: next.games ?? [] }
      }
      // A successful snapshot is the minimum Manager availability boundary for
      // mounting any control-plane page. SSE may connect a moment later, but it
      // must not leave a healthy freshly-opened WebGUI behind a stale
      // `connecting` state.
      connectionState.value = isMockMode ? 'mock' : 'connected'
      managerAvailable.value = true
      lastRefreshAt.value = new Date().toISOString()
    } catch (error) {
      // Execution events can briefly hold the Manager state lock while a
      // screenshot is registered.  A background refresh timeout must not tear
      // down a page that already owns a current snapshot or flood the error
      // drawer once per event.  SSE/health still drive the real offline state;
      // an explicit refresh keeps surfacing the failure to the user.
      if (!options.quiet || snapshot.value.stateVersion === 0) {
        connectionState.value = 'offline'
        managerAvailable.value = false
        rememberError('读取 Manager snapshot 失败', error)
      }
      throw error
    } finally {
      loading.value = false
    }
  }

  async function loadApiContract(): Promise<void> {
    if (isMockMode) {
      apiOperations.value = { '*': ['get', 'post', 'put', 'patch', 'delete'] }
      return
    }
    try {
      const schema = await rootApi.get<{ paths?: Record<string, Record<string, unknown>> }>('/api/v1/openapi.json')
      apiOperations.value = Object.fromEntries(Object.entries(schema.paths ?? {}).map(([path, operations]) => [
        path,
        Object.keys(operations).map((method) => method.toLowerCase()),
      ]))
    } catch (error) {
      rememberError('读取 OpenAPI 合同失败', error)
    }
  }

  function supports(path: string, method: string): boolean {
    if (apiOperations.value['*']) return true
    const target = (path.startsWith('/api/v1') ? path : `/api/v1${path.startsWith('/') ? path : `/${path}`}`)
      .replace(/\{[^}]+\}/g, '{}')
    return Object.entries(apiOperations.value).some(([registeredPath, methods]) =>
      registeredPath.replace(/\{[^}]+\}/g, '{}') === target
      && methods.includes(method.toLowerCase()),
    )
  }

  function scheduleRefresh(): void {
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => {
      refreshTimer = undefined
      void refresh({ quiet: true }).catch(() => undefined)
    }, 500)
  }

  function updateConnection(state: 'connected' | 'reconnecting' | 'offline'): void {
    connectionState.value = state
    if (state === 'connected') {
      if (fallbackPollTimer !== undefined) window.clearInterval(fallbackPollTimer)
      fallbackPollTimer = undefined
      return
    }
    if (fallbackPollTimer === undefined) {
      fallbackPollTimer = window.setInterval(() => void refresh({ quiet: true }).catch(() => undefined), 12_000)
    }
  }

  function mergeReceipt(next: unknown): void {
    const summary = summarizeCommandReceipt(next)
    if (!summary) return
    const index = receipts.value.findIndex((item) => item.commandId === summary.commandId)
    if (index >= 0) receipts.value[index] = { ...receipts.value[index], ...summary }
    else receipts.value.unshift(summary)
    receipts.value = receipts.value.slice(0, 30)
    writeReceiptCache(receipts.value)
  }

  async function refreshCachedReceipts(): Promise<void> {
    const pending = receipts.value.filter((receipt) => receipt.state === 'accepted' || receipt.state === 'running')
    const refreshed = await Promise.allSettled(pending.map((receipt) =>
      managerApi.getReceipt(`/commands/${encodeURIComponent(receipt.commandId)}`),
    ))
    for (const result of refreshed) {
      if (result.status === 'fulfilled') mergeReceipt(result.value)
    }
  }

  function syncReceiptCache(event: StorageEvent): void {
    if (event.key !== receiptCacheKey) return
    receipts.value = decodeReceiptCache(event.newValue ?? '[]')
    writeReceiptCache(receipts.value)
  }

  function consumeEvent(event: ManagerEvent): void {
    if (events.value.some((item) => item.eventId === event.eventId)) return
    events.value.unshift(event)
    events.value = events.value.slice(0, 250)
    if (event.cursor) snapshot.value = { ...snapshot.value, eventCursor: event.cursor }
    const payload = event.payload as Partial<CommandReceipt> & { snapshot?: ManagerSnapshot }
    if (event.type === 'command.receipt' && payload.commandId) mergeReceipt(payload as CommandReceipt)
    if (payload.snapshot && payload.snapshot.stateVersion >= snapshot.value.stateVersion) {
      snapshot.value = { ...emptySnapshot(), ...payload.snapshot, games: payload.snapshot.games ?? [] }
      return
    }
    if (event.stateVersion !== undefined && event.stateVersion > snapshot.value.stateVersion) scheduleRefresh()
    if (event.type === 'snapshot.invalidated' || event.type === 'state.changed') scheduleRefresh()
  }

  async function bootstrap(): Promise<void> {
    if (initialized.value) return
    initialized.value = true
    try {
      await Promise.all([refresh(), loadApiContract()])
      await refreshCachedReceipts()
    } catch {
      // 错误已经进入全局错误中心；SSE 仍会尝试恢复。
    }
    if (isMockMode) {
      connectionState.value = 'mock'
      return
    }
    window.addEventListener('storage', syncReceiptCache)
    stream = new ManagerEventStream('/api/v1', {
      onEvent: consumeEvent,
      onState: updateConnection,
      onCursorExpired: () => void refresh({ quiet: true }),
    })
    stream.start(snapshot.value.eventCursor)
  }

  async function submitCommand(
    label: string,
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    path: string,
    body?: Record<string, unknown>,
    options: CommandOptions = {},
  ): Promise<CommandReceipt | undefined> {
    const expectedStateVersion = options.expectedStateVersion ?? snapshot.value.stateVersion
    const idempotencyKey = options.idempotencyKey
      ?? stateBoundIdempotencyKey(expectedStateVersion, method, path, body)
    const requestId = options.requestId ?? createRequestId()
    const commandOptions: CommandOptions = {
      expectedStateVersion,
      idempotencyKey,
      requestId,
      headers: options.headers,
    }
    try {
      let receipt: CommandReceipt
      try {
        receipt = await managerApi.command(method, path, body, commandOptions)
      } catch (error) {
        if (!(error instanceof ApiError) || error.problem.code !== 'manager_response_timeout') {
          throw error
        }
        // The first request can have reached Manager just as its listener was
        // restarting. Replaying the exact same request identity retrieves the
        // original receipt when it did; it cannot create another batch/run.
        await new Promise<void>((resolve) => window.setTimeout(resolve, 300))
        receipt = await managerApi.command(method, path, body, commandOptions)
      }
      mergeReceipt(receipt)
      scheduleRefresh()
      return receipt
    } catch (error) {
      const outcomeUnknown = error instanceof ApiError && error.problem.code === 'manager_response_timeout'
      rememberError(outcomeUnknown ? `${label}结果未知` : `${label}失败`, error, { requestId, idempotencyKey })
      if (error instanceof ApiError && error.status === 412) {
        await refresh({ quiet: true }).catch(() => undefined)
      }
      return undefined
    }
  }

  function clearError(id: string): void {
    errors.value = errors.value.filter((error) => error.id !== id)
  }

  function clearSettledReceipts(): void {
    receipts.value = receipts.value.filter((receipt) => receipt.state === 'running')
    writeReceiptCache(receipts.value)
  }

  onScopeDispose(() => {
    stream?.stop()
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
    if (fallbackPollTimer !== undefined) window.clearInterval(fallbackPollTimer)
    window.removeEventListener('storage', syncReceiptCache)
  })

  return {
    snapshot,
    connectionState,
    managerAvailable,
    loading,
    initialized,
    events,
    receipts,
    errors,
    apiOperations,
    lastRefreshAt,
    stateVersion,
    activeBatch,
    pendingReceiptCount,
    activeControllerLeaseCount,
    activeTodoBlockerCount,
    runningGameCount,
    executionInProgress,
    executionBusyReason,
    bootstrap,
    refresh,
    loadApiContract,
    supports,
    submitCommand,
    rememberError,
    clearError,
    clearSettledReceipts,
  }
})

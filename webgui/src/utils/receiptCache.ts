import type { CommandState } from '../api/contracts'

export const receiptCacheKey = 'yeyu-gamer:webgui-receipts:v1'

const maximumReceiptCount = 30
const commandStates = new Set<CommandState>([
  'accepted',
  'running',
  'succeeded',
  'rejected',
  'failed',
  'unknown',
])
const safeStateMessages: Record<CommandState, string> = {
  accepted: 'Manager 已受理命令；完成状态以后续事件为准。',
  running: 'Manager 正在处理命令。',
  succeeded: 'Manager 已处理命令；业务验收仍以状态与证据为准。',
  rejected: 'Manager 已拒绝命令。',
  failed: 'Manager 报告命令处理失败。',
  unknown: '命令状态待 Manager 核验。',
}

export interface CommandReceiptSummary {
  commandId: string
  state: CommandState
  requestId?: string
  statusUrl?: string
  acceptedStateVersion?: number
  message?: string
  submittedAt?: string
  completedAt?: string
}

export interface ReceiptStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

function currentStorage(): ReceiptStorage | undefined {
  return typeof localStorage === 'undefined' ? undefined : localStorage
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

function safeCommandId(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.trim()
  return /^[A-Za-z0-9._:-]{1,160}$/.test(normalized) ? normalized : undefined
}

function safeRequestId(value: unknown): string | undefined {
  return safeCommandId(value)
}

function safeReceiptMessage(value: unknown, state: CommandState): string {
  if (typeof value !== 'string') return safeStateMessages[state]
  const normalized = value
    .trim()
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '')
    .replace(/\s+/g, ' ')
  if (!normalized || normalized.length > 1000) return safeStateMessages[state]
  // Receipts are persisted in localStorage across reloads. Keep Manager's
  // diagnostic reason, but never retain text that looks like credential data.
  if (
    /\bbearer\s+\S+/i.test(normalized)
    || /\b(?:authorization|cookie|password|client_secret|access_token|refresh_token)\b\s*[:=]/i.test(normalized)
  ) {
    return safeStateMessages[state]
  }
  return normalized
}

function safeState(value: unknown): CommandState {
  return typeof value === 'string' && commandStates.has(value as CommandState)
    ? value as CommandState
    : 'unknown'
}

function safeTimestamp(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > 64 || !Number.isFinite(Date.parse(value))) return undefined
  return value
}

function safeStateVersion(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

function safeStatusUrl(value: unknown, commandId: string): string | undefined {
  if (typeof value !== 'string' || value.length > 240 || !value.startsWith('/') || value.startsWith('//')) return undefined
  const match = value.match(/^\/(?:api\/v1\/)?commands\/([A-Za-z0-9._:-]{1,160})$/)
  return match?.[1] === commandId ? value : undefined
}

/**
 * Converts a Manager response into the only receipt shape allowed to cross a
 * browser reload. The raw response stays in the caller's current promise chain.
 */
export function summarizeCommandReceipt(value: unknown): CommandReceiptSummary | undefined {
  try {
    const receipt = asRecord(value)
    const commandId = safeCommandId(receipt?.commandId)
    if (!receipt || !commandId) return undefined

    const state = safeState(receipt.state)
    const summary: CommandReceiptSummary = {
      commandId,
      state,
      message: safeReceiptMessage(receipt.message, state),
    }
    const requestId = safeRequestId(receipt.requestId)
    const statusUrl = safeStatusUrl(receipt.statusUrl, commandId)
    const acceptedStateVersion = safeStateVersion(receipt.acceptedStateVersion)
    const submittedAt = safeTimestamp(receipt.submittedAt)
    const completedAt = safeTimestamp(receipt.completedAt)
    if (requestId) summary.requestId = requestId
    if (statusUrl) summary.statusUrl = statusUrl
    if (acceptedStateVersion !== undefined) summary.acceptedStateVersion = acceptedStateVersion
    if (submittedAt) summary.submittedAt = submittedAt
    if (completedAt) summary.completedAt = completedAt
    return summary
  } catch {
    return undefined
  }
}

export function decodeReceiptCache(serialized: string): CommandReceiptSummary[] {
  try {
    const value = JSON.parse(serialized) as unknown
    if (!Array.isArray(value)) return []
    const summaries: CommandReceiptSummary[] = []
    const seen = new Set<string>()
    for (const item of value) {
      const summary = summarizeCommandReceipt(item)
      if (!summary || seen.has(summary.commandId)) continue
      seen.add(summary.commandId)
      summaries.push(summary)
      if (summaries.length >= maximumReceiptCount) break
    }
    return summaries
  } catch {
    return []
  }
}

export function writeReceiptCache(
  receipts: readonly unknown[],
  storage: ReceiptStorage | undefined = currentStorage(),
): void {
  if (!storage) return
  try {
    const summaries = decodeReceiptCache(JSON.stringify(receipts))
    storage.setItem(receiptCacheKey, JSON.stringify(summaries))
  } catch {
    // Receipt history is only a UI recovery aid; Manager remains authoritative.
  }
}

export function readReceiptCache(
  storage: ReceiptStorage | undefined = currentStorage(),
): CommandReceiptSummary[] {
  if (!storage) return []
  try {
    const raw = storage.getItem(receiptCacheKey) ?? '[]'
    const summaries = decodeReceiptCache(raw)
    const sanitized = JSON.stringify(summaries)
    if (sanitized !== raw) storage.setItem(receiptCacheKey, sanitized)
    return summaries
  } catch {
    try {
      storage.setItem(receiptCacheKey, '[]')
    } catch {
      // An unavailable cache must not prevent the WebGUI from starting.
    }
    return []
  }
}

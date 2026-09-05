import type { LogEntry } from '../api/contracts'

export interface LogPresentation {
  source: string
  phase: string
  observedState: string
  decision: string
  reason: string
  rawMessage: string
  structured: boolean
}

function meaningful(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

/**
 * Present structured runtime facts without upgrading a legacy free-text label
 * (for example “blank/stuck” or “crashed”) into an observed UI fact.
 */
export function presentLogEntry(entry: LogEntry): LogPresentation {
  const phase = meaningful(entry.phase) ?? meaningful(entry.stage)
  const observedState = meaningful(entry.observedState)
  const decision = meaningful(entry.decision)
  const reasonCode = meaningful(entry.reasonCode)
  const detail = meaningful(entry.reason) ?? meaningful(entry.detail)
  const reason = reasonCode && detail ? `${reasonCode} · ${detail}` : reasonCode ?? detail
  return {
    source: meaningful(entry.source) ?? meaningful(entry.entityType) ?? '未提供来源',
    phase: phase ?? '未提供阶段',
    observedState: observedState ?? '未提供观察结果',
    decision: decision ?? '未提供判定',
    reason: reason ?? '未提供结构化原因',
    rawMessage: entry.message,
    structured: Boolean(phase && observedState && decision && reason),
  }
}

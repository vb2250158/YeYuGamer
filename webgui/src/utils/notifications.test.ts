import { describe, expect, it } from 'vitest'
import type { NotificationAttempt, NotificationDelivery } from '../api/contracts'
import {
  notificationCanRetry,
  notificationCanSend,
  notificationReason,
  notificationRetryIsAmbiguous,
  notificationRetryRequest,
  notificationSendRequest,
} from './notifications'

function delivery(overrides: Partial<NotificationDelivery> = {}): NotificationDelivery {
  return {
    notificationId: 'notification-1',
    batchId: 'batch-1',
    sealVersion: 1,
    channel: 'email',
    recipientBindingId: 'self-primary',
    messageId: 'message-1',
    state: 'draft',
    dispatchGate: 'manual_review',
    outcome: 'completed',
    subject: '测试通知',
    attachmentRefs: [],
    attemptCount: 0,
    lastErrorClass: '',
    createdAt: '2026-08-28T00:00:00Z',
    updatedAt: '2026-08-28T00:00:00Z',
    ...overrides,
  }
}

function attempt(overrides: Partial<NotificationAttempt> = {}): NotificationAttempt {
  return {
    attemptId: 'attempt-1',
    notificationId: 'notification-1',
    attemptNumber: 1,
    state: 'failed',
    outcome: 'transient_failure',
    errorClass: 'network_timeout',
    transportReceiptHash: '',
    startedAt: '2026-08-28T00:00:00Z',
    createdAt: '2026-08-28T00:00:00Z',
    ...overrides,
  }
}

describe('notification control helpers', () => {
  it('allows only draft sends and failed retries inside the retry budget', () => {
    expect(notificationCanSend(delivery())).toBe(true)
    expect(notificationCanSend(delivery({ state: 'failed' }))).toBe(false)
    expect(notificationCanRetry(delivery({ state: 'failed', attemptCount: 2 }))).toBe(true)
    expect(notificationCanRetry(delivery({ state: 'failed', attemptCount: 3 }))).toBe(false)
    expect(notificationCanRetry(delivery({ state: 'sent' }))).toBe(false)
  })

  it('requires explicit confirmation for every ambiguous backend class', () => {
    for (const errorClass of [
      'transport_ambiguous',
      'worker_internal',
      'lease_expired_ambiguous',
    ]) {
      expect(notificationRetryIsAmbiguous(delivery({ state: 'failed', lastErrorClass: errorClass }))).toBe(true)
    }
    expect(notificationRetryIsAmbiguous(
      delivery({ state: 'failed', lastErrorClass: '' }),
      [attempt({ outcome: 'ambiguous' })],
    )).toBe(true)
    expect(notificationRetryIsAmbiguous(
      delivery({ state: 'failed', lastErrorClass: 'network_timeout' }),
      [attempt()],
    )).toBe(false)
  })

  it('builds only the typed send and retry request fields', () => {
    expect(notificationSendRequest('  operator reviewed preview  ')).toEqual({
      reason: 'operator reviewed preview',
      requestedBy: 'webgui',
    })
    expect(notificationRetryRequest('retry after review', true)).toEqual({
      reason: 'retry after review',
      requestedBy: 'webgui',
      confirmAmbiguous: true,
    })
    expect(notificationReason(` ${'x'.repeat(700)} `)).toHaveLength(500)
  })
})

import type {
  NotificationAttempt,
  NotificationDelivery,
  NotificationRetryRequest,
  NotificationSendRequest,
} from '../api/contracts'

const ambiguousErrorClasses = new Set([
  'transport_ambiguous',
  'worker_internal',
  'lease_expired_ambiguous',
])

export function notificationReason(value: string): string {
  return value.trim().slice(0, 500)
}

export function notificationCanSend(delivery?: NotificationDelivery): boolean {
  return delivery?.state === 'draft' && delivery.attemptCount < 3
}

export function notificationCanRetry(delivery?: NotificationDelivery): boolean {
  return delivery?.state === 'failed' && delivery.attemptCount < 3
}

export function notificationRetryIsAmbiguous(
  delivery?: NotificationDelivery,
  attempts: NotificationAttempt[] = [],
): boolean {
  if (!delivery) return false
  if (ambiguousErrorClasses.has(delivery.lastErrorClass)) return true
  const latest = [...attempts].sort((left, right) => right.attemptNumber - left.attemptNumber)[0]
  return latest?.outcome === 'ambiguous'
}

export function notificationSendRequest(reason: string): NotificationSendRequest {
  return {
    reason: notificationReason(reason),
    requestedBy: 'webgui',
  }
}

export function notificationRetryRequest(
  reason: string,
  confirmAmbiguous: boolean,
): NotificationRetryRequest {
  return {
    ...notificationSendRequest(reason),
    confirmAmbiguous,
  }
}

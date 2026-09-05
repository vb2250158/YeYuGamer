import type { CommandReceipt, WorkItemClaim } from '../api/contracts'

export interface ClaimSecretContext {
  workItemId: string
  claimId: string
  principal: string
}

export interface ClaimSecretExpectation {
  workItemId: string
  principal: string
}

export interface PageClaimSecretSession {
  captureDirectReceipt(receipt: CommandReceipt | undefined, expected: ClaimSecretExpectation): boolean
  has(context: ClaimSecretContext): boolean
  tokenFor(context: ClaimSecretContext): string | undefined
  clear(): void
}

interface ClaimSecretBinding extends ClaimSecretContext {
  fencingToken: string
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function sameBinding(binding: ClaimSecretBinding | undefined, context: ClaimSecretContext): boolean {
  return binding?.workItemId === context.workItemId
    && binding.claimId === context.claimId
    && binding.principal === context.principal
}

export function canRecoverPageClaimSecret(
  claim: WorkItemClaim | undefined,
  currentPrincipal: string,
  hasPageSecret: boolean,
): boolean {
  return claim?.state === 'active'
    && claim.claimant === currentPrincipal
    && !hasPageSecret
}

export function newClaimIdempotencyKey(): string {
  const nonce = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}`
  return `webgui-claim-${nonce}`
}

/**
 * Holds one claim secret for one mounted Agent workbench page.
 *
 * The value stays inside this closure. The caller must create the session from
 * component setup and must never place it in Pinia or browser storage. A fresh
 * page/component therefore starts without a recoverable fencing token.
 */
export function createPageClaimSecretSession(): PageClaimSecretSession {
  let binding: ClaimSecretBinding | undefined

  return {
    captureDirectReceipt(receipt, expected) {
      binding = undefined
      const result = asRecord(receipt?.result)
      const claim = asRecord(result?.claim)
      const claimId = nonEmptyString(claim?.claimId)
      const workItemId = nonEmptyString(claim?.workItemId)
      const principal = nonEmptyString(claim?.claimant)
      const fencingToken = nonEmptyString(claim?.fencingToken)
      if (!receipt || !claimId || receipt.commandId !== claimId || !workItemId || !principal || !fencingToken) return false
      if (workItemId !== expected.workItemId || principal !== expected.principal) return false
      binding = Object.freeze({ workItemId, claimId, principal, fencingToken })
      return true
    },

    has(context) {
      return sameBinding(binding, context)
    },

    tokenFor(context) {
      return sameBinding(binding, context) ? binding?.fencingToken : undefined
    },

    clear() {
      binding = undefined
    },
  }
}

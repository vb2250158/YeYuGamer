import { readFileSync, readdirSync } from 'node:fs'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { CommandReceipt, WorkItemClaim } from '../api/contracts'
import {
  canRecoverPageClaimSecret,
  createPageClaimSecretSession,
  newClaimIdempotencyKey,
} from './claimSecret'

const utilsRoot = dirname(fileURLToPath(import.meta.url))
const sourceRoot = resolve(utilsRoot, '..')

function directClaimReceipt(overrides: Record<string, unknown> = {}): CommandReceipt {
  return {
    commandId: 'claim-1',
    idempotencyKey: 'claim-direct-receipt',
    state: 'succeeded',
    result: {
      claim: {
        claimId: 'claim-1',
        workItemId: 'work-1',
        claimant: 'webgui',
        fencingToken: 'one-page-only-secret',
        ...overrides,
      },
    },
  }
}

function productionSources(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name)
    if (entry.isDirectory()) return productionSources(path)
    if (!['.ts', '.vue'].includes(extname(entry.name)) || entry.name.endsWith('.test.ts')) return []
    return [path]
  })
}

describe('page-local claim secret boundary', () => {
  it('captures the fencing token only from the direct claim receipt', () => {
    const session = createPageClaimSecretSession()
    const context = { workItemId: 'work-1', claimId: 'claim-1', principal: 'webgui' }

    expect(session.captureDirectReceipt(directClaimReceipt(), {
      workItemId: 'work-1',
      principal: 'webgui',
    })).toBe(true)
    expect(session.has(context)).toBe(true)
    expect(session.tokenFor(context)).toBe('one-page-only-secret')
  })

  it('binds the secret to the work item, claim id, and current principal', () => {
    const session = createPageClaimSecretSession()
    session.captureDirectReceipt(directClaimReceipt(), { workItemId: 'work-1', principal: 'webgui' })

    expect(session.tokenFor({ workItemId: 'work-2', claimId: 'claim-1', principal: 'webgui' })).toBeUndefined()
    expect(session.tokenFor({ workItemId: 'work-1', claimId: 'claim-2', principal: 'webgui' })).toBeUndefined()
    expect(session.tokenFor({ workItemId: 'work-1', claimId: 'claim-1', principal: 'agent.yeyu' })).toBeUndefined()

    expect(session.captureDirectReceipt(directClaimReceipt({ claimant: 'agent.yeyu' }), {
      workItemId: 'work-1',
      principal: 'webgui',
    })).toBe(false)
    expect(session.tokenFor({ workItemId: 'work-1', claimId: 'claim-1', principal: 'webgui' })).toBeUndefined()
  })

  it('allows the same principal to POST a fresh recovery grant after page refresh', () => {
    const firstPage = createPageClaimSecretSession()
    const context = { workItemId: 'work-1', claimId: 'claim-1', principal: 'webgui' }
    firstPage.captureDirectReceipt(directClaimReceipt(), { workItemId: 'work-1', principal: 'webgui' })
    expect(firstPage.has(context)).toBe(true)

    const refreshedPage = createPageClaimSecretSession()
    const publicActiveClaim: WorkItemClaim = {
      claimId: 'claim-1',
      workItemId: 'work-1',
      claimant: 'webgui',
      state: 'active',
      expiresAt: '2099-08-28T12:00:00.000Z',
    }

    expect(publicActiveClaim.state).toBe('active')
    expect(refreshedPage.has(context)).toBe(false)
    expect(refreshedPage.tokenFor(context)).toBeUndefined()
    expect(canRecoverPageClaimSecret(publicActiveClaim, 'webgui', refreshedPage.has(context))).toBe(true)
    expect(refreshedPage.captureDirectReceipt(
      directClaimReceipt({ fencingToken: 'recovered-page-secret' }),
      { workItemId: 'work-1', principal: 'webgui' },
    )).toBe(true)
    expect(refreshedPage.tokenFor(context)).toBe('recovered-page-secret')
    expect(canRecoverPageClaimSecret(publicActiveClaim, 'webgui', refreshedPage.has(context))).toBe(false)
    expect(newClaimIdempotencyKey()).not.toBe(newClaimIdempotencyKey())
  })

  it('does not allow a different principal to recover an active claim', () => {
    const otherPrincipalClaim: WorkItemClaim = {
      claimId: 'claim-1',
      workItemId: 'work-1',
      claimant: 'agent.yeyu',
      state: 'active',
      expiresAt: '2099-08-28T12:00:00.000Z',
    }

    expect(canRecoverPageClaimSecret(otherPrincipalClaim, 'webgui', false)).toBe(false)
    expect(canRecoverPageClaimSecret({ ...otherPrincipalClaim, claimant: 'webgui' }, 'webgui', true)).toBe(false)
  })

  it('keeps secret-bearing production code out of browser storage and Pinia persistence', () => {
    const secretBearingSources = productionSources(sourceRoot)
      .map((path) => ({ path, source: readFileSync(path, 'utf8') }))
      .filter(({ source }) => source.includes('fencingToken'))

    expect(secretBearingSources.length).toBeGreaterThan(0)
    for (const { path, source } of secretBearingSources) {
      expect(source, path).not.toMatch(/\blocalStorage\b|\bsessionStorage\b|\bdefineStore\s*\(/)
    }

    const pageSource = readFileSync(resolve(sourceRoot, 'pages', 'AgentWorkbenchPage.vue'), 'utf8')
    expect(pageSource).toContain('claimSecret.captureDirectReceipt(receipt')
    expect(pageSource).toContain('canRecoverPageClaimSecret(')
    expect(pageSource).toContain('currentClaim.value?.claimant === currentPrincipal')
    expect(pageSource).toContain('(!currentClaim.value || needsClaimRecovery.value)')
    expect(pageSource).toContain('{ idempotencyKey: newClaimIdempotencyKey() }')
    expect(pageSource).toContain('可重新领取恢复当前页凭据')
    expect(pageSource).toContain('{{ claimActionText }}')
    expect(pageSource).toContain('if (!hasUsableClaim.value) return claimExplanation.value')
    expect(pageSource).toContain(':disabled="!!requestMoreEvidenceUnavailableReason" @click="requestMoreEvidence"')
    expect(pageSource).toContain(':disabled="!!decisionUnavailableReason" @click="decide"')
    expect(pageSource).toContain('const completionReviewBuild = computed(() => buildCompletionReviewSubmission(')
    expect(pageSource).toContain('currentSnapshotRunId.value')
    expect(pageSource).toContain('if (artifacts.error.value) return `无法读取工作项 artifact 台账：${artifacts.error.value}`')
    expect(pageSource).toContain('accepted 不是“有一张截图就算完成”')
    expect(pageSource).toContain('v-for="todo in completionScope.value.requiredTodos"')
    expect(pageSource).toContain('v-model="formForTodo(todo).artifactRefs"')
    expect(pageSource.match(/const fencingToken = activeClaimToken\(\)/g)).toHaveLength(3)
    expect(pageSource.match(/!fencingToken/g)).toHaveLength(3)

    const contractsSource = readFileSync(resolve(sourceRoot, 'api', 'contracts.ts'), 'utf8')
    const publicClaimDto = contractsSource.match(/export interface WorkItemClaim \{([\s\S]*?)\n\}/)?.[1] ?? ''
    expect(publicClaimDto).not.toContain('fencingToken')
  })
})

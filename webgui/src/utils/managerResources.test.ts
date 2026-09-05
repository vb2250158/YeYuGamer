import { describe, expect, it } from 'vitest'
import type { AdapterInfo, CapabilityDefinition, ManagerSnapshot } from '../api/contracts'
import {
  artifactContentPath,
  buildCapabilityArguments,
  capabilityClosure,
  capabilityInputFields,
  editableConfigPatch,
  enabledCapability,
  evidenceCanBeReviewed,
  executionIsEnabled,
  parseOpaqueEvidenceIds,
  runtimeBindingReadinessForGames,
  stateBoundIdempotencyKey,
} from './managerResources'

describe('Manager resource helpers', () => {
  it('opens execution only when Manager explicitly enables it', () => {
    const snapshot = { stateVersion: 1, games: [] } as ManagerSnapshot
    expect(executionIsEnabled(snapshot)).toBe(false)
    snapshot.manager = { legacyExecutionEnabled: false }
    expect(executionIsEnabled(snapshot)).toBe(false)
    snapshot.manager.legacyExecutionEnabled = true
    expect(executionIsEnabled(snapshot)).toBe(true)
  })

  it('uses Manager per-game runtimeBinding readiness without dropping unavailable selections', () => {
    const adapters = [
      {
        adapterId: 'adapter-a',
        gameId: 'A',
        executionReady: true,
        executionPackageStatus: 'promoted',
        health: 'healthy',
      },
      {
        adapterId: 'adapter-b',
        gameId: 'B',
        executionReady: false,
        executionPackageStatus: 'installed-unpromoted',
        health: 'host-healthy-execution-disabled',
      },
    ] as AdapterInfo[]

    const partial = runtimeBindingReadinessForGames(['A', 'B'], adapters)

    expect(partial.readyGameIds).toEqual(['A'])
    expect(partial.unavailableGames.map((game) => game.gameId)).toEqual(['B'])
    expect(partial.unavailableGames[0]?.reason).toContain('installed-unpromoted')
    expect(partial.anyReady).toBe(true)
    expect(partial.allReady).toBe(false)
  })

  it('fails closed when every selected game lacks a ready Manager projection', () => {
    const summary = runtimeBindingReadinessForGames(['A', 'B'], [])
    expect(summary.readyGameIds).toEqual([])
    expect(summary.unavailableGames.map((game) => game.gameId)).toEqual(['A', 'B'])
    expect(summary.anyReady).toBe(false)
  })

  it('requires the exact capability to be enabled', () => {
    const capabilities = [
      { capabilityId: 'game.weekly.plan', version: '1.0', risk: 'controlled_write', enabled: true },
      { capabilityId: 'game.weekly.run', version: '1.0', risk: 'routine_action', enabled: false },
    ] as CapabilityDefinition[]
    expect(enabledCapability(capabilities, 'game.weekly.plan')?.version).toBe('1.0')
    expect(enabledCapability(capabilities, 'game.weekly.run')).toBeUndefined()
  })

  it('builds an opaque Manager artifact URL and rejects path-shaped evidence ids', () => {
    expect(artifactContentPath('artifact / 01')).toBe('/api/v1/artifacts/artifact%20%2F%2001/content')
    expect(parseOpaqueEvidenceIds('one, two，one ../escape C:\\file three')).toEqual(['one', 'two', 'three'])
  })

  it('binds idempotency to canonical intent and the observed state version', () => {
    const left = stateBoundIdempotencyKey(42, 'POST', '/batches', { mode: 'plan', gameIds: ['A'] })
    const reordered = stateBoundIdempotencyKey(42, 'POST', '/batches', { gameIds: ['A'], mode: 'plan' })
    expect(left).toBe(reordered)
    expect(stateBoundIdempotencyKey(43, 'POST', '/batches', { mode: 'plan', gameIds: ['A'] })).not.toBe(left)
  })

  it('projects imported snake case config into the strict editable API schema', () => {
    expect(editableConfigPatch({
      daily_schedule_enabled: false,
      weekly_day: 'Saturday',
      execution_strategy: 'continue',
      step_timeout_seconds: 1200,
    })).toEqual({
      dailyScheduleEnabled: false,
      weeklyDay: 'Saturday',
      executionStrategy: 'continue',
    })
  })

  it('keeps the typed Todo reset policy inside the editable config boundary', () => {
    const todoResetPolicy = {
      timezone: 'Asia/Shanghai',
      time: '04:00',
      week_start_day: 'Monday',
      per_game: { FGO: { time: '00:00' } },
      per_definition: {},
    }
    expect(editableConfigPatch({ todo_reset_policy: todoResetPolicy })).toEqual({
      todoResetPolicy,
    })
  })

  it('keeps diagnostic artifacts outside completion evidence review', () => {
    expect(evidenceCanBeReviewed({ artifactId: 'diagnostic', kind: 'diagnostic-bundle', verdict: 'not-applicable' })).toBe(false)
    expect(evidenceCanBeReviewed({ artifactId: 'frame', kind: 'raw-frame', verdict: 'pending' })).toBe(true)
  })

  it('builds only typed capability arguments and reports missing domain context', () => {
    const definition: CapabilityDefinition = {
      capabilityId: 'game.weekly.plan',
      version: '1.0',
      risk: 'controlled_write',
      enabled: true,
      inputSchema: {
        type: 'object',
        properties: {
          gameId: { type: 'string' },
          weeklyId: { type: 'string' },
        },
        additionalProperties: false,
      },
    }
    expect(capabilityInputFields(definition).map((field) => [field.name, field.required])).toEqual([
      ['gameId', true],
      ['weeklyId', true],
    ])
    expect(buildCapabilityArguments(definition, { gameId: 'ZZZ', ignored: 'not-in-schema' })).toEqual({
      arguments: { gameId: 'ZZZ' },
      errors: ['请填写 weeklyId'],
    })
  })

  it('parses array capability fields without forwarding arbitrary keys', () => {
    const definition: CapabilityDefinition = {
      capabilityId: 'checkpoint.record',
      version: '1.0',
      risk: 'controlled_write',
      enabled: true,
      inputSchema: {
        type: 'object',
        properties: {
          runId: { type: 'string' },
          artifactIds: { type: 'array', items: { type: 'string' } },
        },
      },
    }
    expect(buildCapabilityArguments(definition, {
      runId: 'run-1',
      artifactIds: 'a-1，a-2\na-3',
      shell: 'forbidden',
    })).toEqual({
      arguments: { runId: 'run-1', artifactIds: ['a-1', 'a-2', 'a-3'] },
      errors: [],
    })
  })

  it('distinguishes domain results from generic planned request records', () => {
    expect(capabilityClosure('system.snapshot.read')).toBe('read')
    expect(capabilityClosure('adapter.canary.request')).toBe('domain')
    expect(capabilityClosure('game.daily.run')).toBe('execution-request')
    expect(capabilityClosure('incident.repair.create')).toBe('request-record')
  })
})

import { describe, expect, it } from 'vitest'
import type { EvidenceArtifact, TodoInstance } from '../api/contracts'
import { acceptedCompletionEvidence, currentStepEvidence } from './todayEvidence'

function artifact(overrides: Partial<EvidenceArtifact>): EvidenceArtifact {
  return {
    artifactId: crypto.randomUUID(),
    gameId: 'StarRail',
    runId: 'run-accepted',
    runAttemptId: 'attempt-accepted',
    todoAttemptId: 'todo-attempt-accepted',
    gameDayKey: 'daily:2026-09-01',
    contentType: 'image/png',
    capturedAt: '2026-09-01T14:00:00Z',
    ...overrides,
  }
}

const acceptedScope = {
  acceptanceState: 'accepted_done' as const,
  gameId: 'StarRail',
  runId: 'run-accepted',
  gameDayKeys: ['daily:2026-09-01'],
}

describe('acceptedCompletionEvidence', () => {
  it('rejects a human-gate diagnostic PNG from the current run', () => {
    const result = acceptedCompletionEvidence([
      artifact({ kind: 'game-ui-human-required-raw', raw: true }),
    ], acceptedScope)

    expect(result).toEqual([])
  })

  it('shows nothing until Manager accepts the run', () => {
    const pair = [
      artifact({ kind: 'game-ui-daily-reward-raw', raw: true }),
      artifact({ kind: 'game-ui-daily-reward-watermarked', raw: false }),
    ]

    expect(acceptedCompletionEvidence(pair, {
      ...acceptedScope,
      acceptanceState: 'review_required',
    })).toEqual([])
  })

  it('requires a same-attempt raw and watermarked reward pair', () => {
    const result = acceptedCompletionEvidence([
      artifact({ kind: 'game-ui-daily-reward-raw', raw: true }),
      artifact({
        kind: 'game-ui-daily-reward-watermarked',
        raw: false,
        todoAttemptId: 'different-todo-attempt',
      }),
    ], acceptedScope)

    expect(result).toEqual([])
  })

  it('returns only the accepted run same-attempt reward pair', () => {
    const raw = artifact({ kind: 'game-ui-daily-reward-raw', raw: true })
    const watermarked = artifact({
      kind: 'game-ui-daily-reward-watermarked',
      raw: false,
      capturedAt: '2026-09-01T14:00:01Z',
    })
    const result = acceptedCompletionEvidence([
      artifact({ kind: 'game-ui-human-required-raw', raw: true }),
      artifact({ kind: 'game-ui-daily-reward-raw', raw: true, runId: 'older-run' }),
      raw,
      watermarked,
    ], acceptedScope)

    expect(result.map((item) => item.artifactId)).toEqual([raw.artifactId, watermarked.artifactId])
  })

  it('shows only screenshot IDs sealed into the completion decision', () => {
    const raw = artifact({ kind: 'game-ui-daily-reward-raw', raw: true })
    const watermarked = artifact({ kind: 'game-ui-daily-reward-watermarked', raw: false })
    const unsealed = artifact({
      kind: 'game-ui-daily-reward-raw',
      raw: true,
      capturedAt: '2026-09-01T14:00:05Z',
    })

    const result = acceptedCompletionEvidence([raw, watermarked, unsealed], {
      ...acceptedScope,
      acceptedArtifactIds: [raw.artifactId, watermarked.artifactId],
    })

    expect(result.map((item) => item.artifactId)).toEqual([raw.artifactId, watermarked.artifactId])
  })
})

describe('currentStepEvidence', () => {
  const todo = {
    todoInstanceId: 'todo-current',
    gameId: 'StarRail',
    runId: 'run-current',
    periodKey: 'daily:2026-09-01',
  } as TodoInstance

  it('shows nothing after reset clears the current run watermark', () => {
    expect(currentStepEvidence([
      artifact({
        runId: 'run-before-reset',
        todoInstanceId: todo.todoInstanceId,
        kind: 'game-ui-step-after-watermarked',
      }),
    ], { ...todo, runId: null })).toEqual([])
  })

  it('rejects same-period screenshots from an older run', () => {
    const current = artifact({
      runId: todo.runId ?? undefined,
      todoInstanceId: todo.todoInstanceId,
      todoAttemptId: 'attempt-current',
      kind: 'game-ui-step-after-watermarked',
    })
    const result = currentStepEvidence([
      artifact({
        runId: 'run-before-reset',
        todoInstanceId: todo.todoInstanceId,
        todoAttemptId: 'attempt-old',
        kind: 'game-ui-step-after-watermarked',
      }),
      current,
    ], todo)

    expect(result.map((item) => item.artifactId)).toEqual([current.artifactId])
  })
})

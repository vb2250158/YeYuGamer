import type { AcceptanceState, EvidenceArtifact, TodoInstance } from '../api/contracts'

const completionEvidenceKinds = new Set([
  'game-ui-daily-reward-raw',
  'game-ui-daily-reward-watermarked',
])

function lineageKey(artifact: EvidenceArtifact): string | undefined {
  return artifact.todoAttemptId ?? artifact.runAttemptId ?? artifact.runId
}

export interface CompletionEvidenceScope {
  acceptanceState: AcceptanceState
  gameId: string
  runId?: string
  gameDayKeys: Iterable<string>
  /** Exact screenshot IDs sealed into the Manager completion decision. */
  acceptedArtifactIds?: Iterable<string>
}

/**
 * Return the latest same-attempt reward screenshot pair for an accepted run.
 * Diagnostic, human-gate, step and older-run images must never appear under
 * “今日完成截图”, even when they are current-day PNG artifacts.
 */
export function acceptedCompletionEvidence(
  artifacts: EvidenceArtifact[],
  scope: CompletionEvidenceScope,
): EvidenceArtifact[] {
  if (scope.acceptanceState !== 'accepted_done' || !scope.runId) return []

  const gameDayKeys = new Set(scope.gameDayKeys)
  const acceptedArtifactIds = scope.acceptedArtifactIds
    ? new Set(scope.acceptedArtifactIds)
    : undefined
  if (acceptedArtifactIds && acceptedArtifactIds.size === 0) return []
  const candidates = artifacts
    .filter((artifact) => (
      (!acceptedArtifactIds || acceptedArtifactIds.has(artifact.artifactId))
      &&
      artifact.gameId === scope.gameId
      && artifact.runId === scope.runId
      && typeof artifact.gameDayKey === 'string'
      && gameDayKeys.has(artifact.gameDayKey)
      && typeof artifact.contentType === 'string'
      && ['image/png', 'image/jpeg'].includes(artifact.contentType.toLowerCase())
      && completionEvidenceKinds.has(artifact.kind ?? '')
      && (
        artifact.kind === 'game-ui-daily-reward-watermarked'
          ? artifact.raw === false
          : artifact.raw === true
      )
    ))
    .sort((left, right) => Date.parse(right.capturedAt ?? '') - Date.parse(left.capturedAt ?? ''))

  const latestWatermarked = candidates.find((artifact) => (
    artifact.kind === 'game-ui-daily-reward-watermarked'
  ))
  const latestLineage = latestWatermarked ? lineageKey(latestWatermarked) : undefined
  if (!latestLineage) return []

  const sameAttempt = candidates.filter((artifact) => lineageKey(artifact) === latestLineage)
  const raw = sameAttempt.find((artifact) => artifact.kind === 'game-ui-daily-reward-raw')
  const watermarked = sameAttempt.find((artifact) => artifact.kind === 'game-ui-daily-reward-watermarked')
  return raw && watermarked ? [raw, watermarked] : []
}

/** Return only the latest screenshot pair bound to the Todo's current run. */
export function currentStepEvidence(
  artifacts: EvidenceArtifact[],
  todo: TodoInstance,
): EvidenceArtifact[] {
  // Explicit reset reuses the deterministic current-period Todo id, while the
  // cleared runId is the authoritative watermark that no new attempt exists.
  if (!todo.runId) return []
  const candidates = artifacts
    .filter((artifact) => (
      artifact.gameId === todo.gameId
      && artifact.runId === todo.runId
      && artifact.todoInstanceId === todo.todoInstanceId
      && artifact.gameDayKey === todo.periodKey
      && ['game-ui-step-before-raw', 'game-ui-step-after-watermarked'].includes(artifact.kind ?? '')
      && typeof artifact.contentType === 'string'
      && ['image/png', 'image/jpeg'].includes(artifact.contentType.toLowerCase())
    ))
    .sort((left, right) => Date.parse(right.capturedAt ?? '') - Date.parse(left.capturedAt ?? ''))
  const latestAttemptId = candidates.find((artifact) => artifact.kind === 'game-ui-step-after-watermarked')?.todoAttemptId
    ?? candidates[0]?.todoAttemptId
  if (!latestAttemptId) return []
  return candidates
    .filter((artifact) => artifact.todoAttemptId === latestAttemptId)
    .sort((left, right) => (
      left.kind === 'game-ui-step-before-raw' ? -1
        : right.kind === 'game-ui-step-before-raw' ? 1
          : Date.parse(left.capturedAt ?? '') - Date.parse(right.capturedAt ?? '')
    ))
}

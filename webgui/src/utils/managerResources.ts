import type { AdapterInfo, CapabilityDefinition, EvidenceArtifact, JsonObject, ManagerSnapshot } from '../api/contracts'

export function executionIsEnabled(snapshot: ManagerSnapshot): boolean {
  return snapshot.manager?.legacyExecutionEnabled === true
}

export interface GameRuntimeBindingReadiness {
  gameId: string
  executionReady: boolean
  reason: string
}

export interface RuntimeBindingReadinessSummary {
  games: GameRuntimeBindingReadiness[]
  readyGameIds: string[]
  unavailableGames: GameRuntimeBindingReadiness[]
  anyReady: boolean
  allReady: boolean
}

function projectedRuntimeBindingReason(adapter?: AdapterInfo): string {
  if (!adapter) return 'Manager 尚未返回这个游戏的 runtimeBinding readiness。'
  if (adapter.executionReady === true) return 'Manager runtimeBinding executionReady=true。'
  const facts = [
    adapter.executionPackageStatus ? `执行包 ${adapter.executionPackageStatus}` : undefined,
    adapter.health ? `Adapter ${adapter.health}` : undefined,
    adapter.hostStatus ? `Host ${adapter.hostStatus}` : undefined,
  ].filter((item): item is string => Boolean(item))
  return `Manager runtimeBinding executionReady=false${facts.length ? `（${facts.join('；')}）` : '（未返回更多原因）'}。`
}

/**
 * Summarizes Manager-owned per-game Adapter projections for action affordances.
 * This is an early UI guard only; the batch command still submits the complete
 * selection and Manager remains the final authority for operation bindings.
 */
export function runtimeBindingReadinessForGames(
  gameIds: readonly string[],
  adapters: readonly AdapterInfo[],
): RuntimeBindingReadinessSummary {
  const uniqueGameIds = [...new Set(gameIds)]
  const games = uniqueGameIds.map((gameId) => {
    const projected = adapters.filter((adapter) => adapter.gameId === gameId)
    const adapter = projected.find((candidate) => candidate.executionReady === true) ?? projected[0]
    return {
      gameId,
      executionReady: adapter?.executionReady === true,
      reason: projectedRuntimeBindingReason(adapter),
    }
  })
  const readyGameIds = games.filter((game) => game.executionReady).map((game) => game.gameId)
  const unavailableGames = games.filter((game) => !game.executionReady)
  return {
    games,
    readyGameIds,
    unavailableGames,
    anyReady: readyGameIds.length > 0,
    allReady: games.length > 0 && unavailableGames.length === 0,
  }
}

export function enabledCapability(
  capabilities: CapabilityDefinition[],
  capabilityId: string,
): CapabilityDefinition | undefined {
  return capabilities.find((capability) =>
    capability.capabilityId === capabilityId && capability.enabled === true,
  )
}

export function artifactContentPath(artifactId: string): string {
  return `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`
}

export function parseOpaqueEvidenceIds(value: string): string[] {
  return [...new Set(value
    .split(/[\s,，]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0 && item.length <= 160 && !/[\\/]/.test(item)))]
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as JsonObject)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, stableValue(child)]))
  }
  return value
}

export function stateBoundIdempotencyKey(
  stateVersion: number,
  method: string,
  path: string,
  body?: JsonObject,
): string {
  const text = JSON.stringify(stableValue({ stateVersion, method, path, body: body ?? null }))
  let left = 0x811c9dc5
  let right = 0x9e3779b9
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index)
    left = Math.imul(left ^ code, 0x01000193) >>> 0
    right = Math.imul(right ^ (code + index), 0x85ebca6b) >>> 0
  }
  return `webgui-v${stateVersion}-${left.toString(16).padStart(8, '0')}${right.toString(16).padStart(8, '0')}`
}

const configAliases: Array<[string, string]> = [
  ['enabled', 'enabled'],
  ['order', 'order'],
  ['dailyScheduleEnabled', 'daily_schedule_enabled'],
  ['dailyScheduleTime', 'daily_schedule_time'],
  ['weeklyEnabled', 'weekly_enabled'],
  ['weeklyDay', 'weekly_day'],
  ['skipBlockedOnRunAll', 'skip_blocked_on_run_all'],
  ['executionStrategy', 'execution_strategy'],
  ['todoResetPolicy', 'todo_reset_policy'],
]

export function editableConfigPatch(values: JsonObject): JsonObject {
  const patch: JsonObject = {}
  for (const [apiName, storedName] of configAliases) {
    const value = values[apiName] ?? values[storedName]
    if (value !== undefined) patch[apiName] = value
  }
  return patch
}

export function evidenceCanBeReviewed(artifact: EvidenceArtifact): boolean {
  return artifact.kind !== 'diagnostic-bundle' && artifact.verdict !== 'not-applicable'
}

export interface CapabilityInputField {
  name: string
  type: string
  required: boolean
  description?: string
  values?: unknown[]
}

const domainRequiredFields: Record<string, string[]> = {
  'game.daily.plan': ['gameId'],
  'game.daily.run': ['gameId'],
  'game.weekly.plan': ['gameId', 'weeklyId'],
  'game.weekly.run': ['gameId', 'weeklyId'],
  'run.resume.request': ['runId'],
  'run.takeover.request': ['runId'],
  'run.takeover.release': ['runId'],
  'observation.capture.request': ['gameId', 'runId'],
  'checkpoint.record': ['runId', 'artifactIds'],
  'evidence.review.submit': ['artifactId', 'verdict'],
  'incident.repair.create': ['incidentId', 'reason'],
  'repair.verification.request': ['repairSessionId'],
  'adapter.replay.request': ['adapterId'],
  'adapter.shadow.request': ['adapterId'],
  'adapter.canary.request': ['adapterId'],
  'adapter.promotion.request': ['adapterId', 'versionId'],
  'adapter.rollback.request': ['adapterId', 'versionId'],
  'config.validate': ['config'],
  'config.rollback': ['version'],
  'notification.draft': ['milestoneId'],
  'notification.send': ['notificationId'],
}

export function capabilityInputFields(definition?: CapabilityDefinition): CapabilityInputField[] {
  if (!definition) return []
  const schema = definition.inputSchema ?? {}
  const properties = schema.properties && typeof schema.properties === 'object'
    ? schema.properties as JsonObject
    : {}
  const required = new Set([
    ...(Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === 'string') : []),
    ...(domainRequiredFields[definition.capabilityId] ?? []),
  ])
  return Object.entries(properties).map(([name, raw]) => {
    const property = raw && typeof raw === 'object' ? raw as JsonObject : {}
    return {
      name,
      type: typeof property.type === 'string' ? property.type : 'string',
      required: required.has(name),
      description: typeof property.description === 'string' ? property.description : undefined,
      values: Array.isArray(property.enum) ? property.enum : undefined,
    }
  })
}

function missingInput(value: unknown): boolean {
  return value === undefined || value === null || (typeof value === 'string' && value.trim() === '')
}

export function buildCapabilityArguments(
  definition: CapabilityDefinition | undefined,
  values: Record<string, unknown>,
): { arguments: JsonObject; errors: string[] } {
  const result: JsonObject = {}
  const errors: string[] = []
  for (const field of capabilityInputFields(definition)) {
    const raw = values[field.name]
    if (missingInput(raw)) {
      if (field.required) errors.push(`请填写 ${field.name}`)
      continue
    }
    try {
      let value: unknown = raw
      if (field.type === 'array') {
        value = Array.isArray(raw)
          ? raw
          : String(raw).split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean)
      } else if (field.type === 'object') {
        value = typeof raw === 'string' ? JSON.parse(raw) as unknown : raw
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('必须是 JSON 对象')
      } else if (field.type === 'boolean') {
        if (typeof raw !== 'boolean' && raw !== 'true' && raw !== 'false') throw new Error('必须是 true 或 false')
        value = typeof raw === 'boolean' ? raw : raw === 'true'
      } else if (field.type === 'number' || field.type === 'integer') {
        value = Number(raw)
        if (!Number.isFinite(value) || (field.type === 'integer' && !Number.isInteger(value))) {
          throw new Error(field.type === 'integer' ? '必须是整数' : '必须是数字')
        }
      } else {
        value = String(raw).trim()
      }
      if (field.values && !field.values.includes(value)) throw new Error('不在允许值中')
      result[field.name] = value
    } catch (error) {
      errors.push(`${field.name}：${error instanceof Error ? error.message : String(error)}`)
    }
  }
  return { arguments: result, errors }
}

export type CapabilityClosure = 'read' | 'domain' | 'execution-request' | 'request-record'

export function capabilityClosure(capabilityId?: string): CapabilityClosure {
  if (capabilityId === 'system.snapshot.read') return 'read'
  if (capabilityId && ['game.daily.run', 'game.weekly.run', 'batch.daily.run'].includes(capabilityId)) return 'execution-request'
  if (capabilityId && [
    'game.daily.plan',
    'game.weekly.plan',
    'batch.daily.plan',
    'adapter.canary.request',
  ].includes(capabilityId)) return 'domain'
  return 'request-record'
}

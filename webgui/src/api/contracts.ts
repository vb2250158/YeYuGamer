export type JsonObject = Record<string, unknown>
export type CommandState = 'accepted' | 'running' | 'succeeded' | 'rejected' | 'failed' | 'unknown'
export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'mock'
export type TodoCadence = 'daily' | 'weekly'
export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'skipped' | 'blocked' | 'review_required' | 'human_required'
export type TodoRisk = 'observe_only' | 'routine_action' | 'approval_required' | 'forbidden'
export type TodoDifficulty = 'low' | 'medium' | 'high' | 'unknown'
export type TodoDispatchDisposition =
  | 'eligible'
  | 'completed_skip'
  | 'terminal_skip'
  | 'deferred_review'
  | 'deferred_human'
  | 'deferred_forbidden'
  | 'reconcile_required'
  | 'unsupported'

export interface TodoActionAvailability {
  execute: boolean
  resume: boolean
  review: boolean
  releaseHuman: boolean
  submitManualEvidence: boolean
  nextAction: string
}

export interface TodoResetRule extends JsonObject {
  timezone?: string
  time?: string
  weekday?: string
  weekStartDay?: string
  cadence?: TodoCadence
}

export interface TodoResetOverride {
  timezone?: string
  time?: string
  weekStartDay?: string
}

export interface TodoResetPolicy {
  timezone: string
  time: string
  weekStartDay: string
  perGame: Record<string, TodoResetOverride>
  perDefinition: Record<string, TodoResetOverride>
}

export interface LDPlayerGameBindingConfig {
  provider: 'ldplayer'
  consolePath: string
  adbPath: string
  instanceIndex: number
  instanceName?: string | null
  adbSerial: string
}

export interface GamePathConfig {
  gamePath?: string | null
  toolPath?: string | null
  emulator?: LDPlayerGameBindingConfig | null
}

export type GameIntegrationMappingStatus = 'registered' | 'incomplete' | 'not_registered'

export interface GameIntegrationOperation {
  operation: string
  todoDefinitionId: string
  title: string
  sourceRefs: string[]
}

export type GameIntegrationParameterDispatchStatus = 'active' | 'requires_adapter'

export interface GameIntegrationParameter {
  key: string
  label: string
  valueType: 'enum' | 'integer' | 'boolean' | 'notice'
  dispatchStatus: GameIntegrationParameterDispatchStatus
  options: string[]
  minimum?: number | null
  maximum?: number | null
  note: string
}

export interface GameIntegration {
  gameId: string
  integrationId?: string | null
  toolName?: string | null
  source?: string | null
  entryOperation?: string | null
  mappingStatus: GameIntegrationMappingStatus
  mappingNote: string
  operations: GameIntegrationOperation[]
  parameters: GameIntegrationParameter[]
}

export interface TodoProgress {
  completed: number
  total: number
  percent: number
}

export interface TodoDifficultOperation {
  todoInstanceId: string
  operation: string
  title: string
  status: TodoStatus
  automationDifficulty: TodoDifficulty
  automationState: string
  reason?: string
}

export interface TodoSummary {
  cadence: TodoCadence
  scopeKey: string
  scopeFingerprint: string
  periodKeys: string[]
  periodStartsAt: string[]
  periodEndsAt: string[]
  resetRules: TodoResetRule[]
  definitionCount: number
  requiredTotal: number
  requiredCompleted: number
  requiredRemaining: number
  allRequiredCompleted: boolean
  progress: TodoProgress
  nextResetAt?: string | null
  counts: Record<TodoStatus, number>
  outstandingTodoInstanceIds: string[]
  unresolvedRequiredTodoIds: string[]
  difficultOperations: TodoDifficultOperation[]
}

export interface TodoOverview {
  defaultResetRule: TodoResetRule
  scopeKey: string
  scopeFingerprint: string
  scopeGameIds: string[]
  games: Record<string, TodoSummary>
  requiredTotal: number
  requiredCompleted: number
  requiredRemaining: number
  blocked: number
  reviewRequired: number
}

export interface TodoSummaries {
  daily: TodoSummary
  weekly: TodoSummary
}

export interface TodoDefinition {
  todoDefinitionId: string
  definitionVersion: number
  catalogVersion: string
  sourceHash: string
  gameId: string
  cadence: TodoCadence
  operation: string
  title: string
  category: string
  orderIndex: number
  required: boolean
  risk: TodoRisk
  automationDifficulty: TodoDifficulty
  adapterCapabilityRef?: string | null
  automationState: string
  initialStatus: TodoStatus
  initialReason?: string
  resetRule: TodoResetRule
  sourceRefs: string[]
  active: boolean
  updatedAt: string
}

export interface TodoInstance {
  todoInstanceId: string
  todoDefinitionId: string
  definitionVersion: number
  catalogVersion: string
  sourceHash: string
  gameId: string
  cadence: TodoCadence
  periodKey: string
  periodStartsAt: string
  periodEndsAt: string
  operation: string
  title: string
  category: string
  orderIndex: number
  required: boolean
  risk: TodoRisk
  automationDifficulty: TodoDifficulty
  adapterCapabilityRef?: string | null
  automationState: string
  resetRule: TodoResetRule
  sourceRefs: string[]
  status: TodoStatus
  dispatchDisposition: TodoDispatchDisposition
  dispatchReason: string
  actionAvailability: TodoActionAvailability
  attempts: number
  reason?: string
  evidenceRefs: string[]
  runId?: string | null
  startedAt?: string | null
  completedAt?: string | null
  lastAttemptAt?: string | null
  createdAt: string
  updatedAt: string
}

export type RunAttemptState = 'starting' | 'running' | 'cancelling' | 'completed' | 'partial' | 'blocked' | 'review_required' | 'human_required' | 'cancelled' | 'failed'
export type TodoAttemptState = 'running' | 'completed' | 'skipped' | 'blocked' | 'review_required' | 'human_required'

export interface RunAttempt extends JsonObject {
  runAttemptId: string
  runId: string
  gameId: string
  cadence: TodoCadence
  state: RunAttemptState
  executableTodoInstanceIds: string[]
  processId?: number | null
  exitCode?: number | null
  result: JsonObject
  startedAt: string
  completedAt?: string | null
  createdAt: string
  updatedAt: string
}

export interface TodoAttempt extends JsonObject {
  todoAttemptId: string
  runAttemptId: string
  todoInstanceId: string
  attemptNumber: number
  operation: string
  state: TodoAttemptState
  reasonCode: string
  reason: string
  retryable: boolean
  evidenceRefs: string[]
  startedAt: string
  completedAt?: string | null
  createdAt: string
  updatedAt: string
}

export interface AttemptProblemSignal extends JsonObject {
  todoInstanceId: string
  todoDefinitionId: string
  gameId: string
  cadence: TodoCadence
  operation: string
  title: string
  currentStatus: TodoStatus
  automationDifficulty: TodoDifficulty
  attemptCount: number
  blockedCount: number
  reviewRequiredCount: number
  humanRequiredCount?: number
  retryableFailureCount: number
  latestState: TodoAttemptState
  latestReasonCode: string
  latestReason: string
  latestAttemptAt: string
  evidenceRefs: string[]
}

export interface AttemptAnalysis extends JsonObject {
  summary: {
    runAttemptCount: number
    todoAttemptCount: number
    blockedTodoAttemptCount: number
    reviewRequiredTodoAttemptCount: number
    retryableFailureCount: number
    problemSignalCount: number
  }
  runAttempts: RunAttempt[]
  todoAttempts: TodoAttempt[]
  problemSignals: AttemptProblemSignal[]
  interpretation: string
}

export interface TodoResetPreviewItem {
  todoDefinitionId: string
  todoInstanceId: string
  gameId: string
  cadence: TodoCadence
  periodKey: string
  periodStartsAt: string
  periodEndsAt: string
  exists: boolean
  currentStatus?: TodoStatus | null
  effectiveResetRule: TodoResetRule
  nextPeriodResetRule: TodoResetRule
  policyChangeDeferred: boolean
}

export interface TodoResetPreview {
  stateVersion: number
  generatedAt: string
  action: 'reconcile' | 'reset'
  gameIds: string[]
  cadence?: TodoCadence | null
  definitionCount: number
  existingCount: number
  wouldCreateCount: number
  items: TodoResetPreviewItem[]
}

export type RuntimeState =
  | 'planned'
  | 'preflight'
  | 'launching'
  | 'attaching'
  | 'observing'
  | 'executing'
  | 'verifying'
  | 'reconciling'
  | 'terminal'
  | 'retry_wait'
  | 'blocked_technical'
  | 'human_takeover'
  | 'cancelled'
  | 'crashed'
  | string

export type AcceptanceState =
  | 'unknown'
  | 'not_started'
  | 'in_progress'
  | 'evidence_pending'
  | 'accepted_done'
  | 'rejected_done'
  | 'not_applicable'
  | string

export type ReviewState =
  | 'none'
  | 'agent_queued'
  | 'agent_in_progress'
  | 'approval_required'
  | 'human_required'
  | 'resolved'
  | string

export interface ManagerIdentity {
  name?: string
  version?: string
  apiVersion?: string
  startedAt?: string
  mode?: string
  webGuiAvailable?: boolean
  legacyExecutionEnabled?: boolean
  [key: string]: unknown
}

export interface HealthStatus {
  status: string
  manager?: string
  storage?: string
  eventStream?: string
  checkedAt?: string
  components?: Record<string, string | JsonObject>
  [key: string]: unknown
}

export interface GameState {
  gameId: string
  displayName: string
  enabled?: boolean
  runtimeState: RuntimeState
  acceptanceState: AcceptanceState
  reviewState: ReviewState
  stage?: string
  runId?: string
  batchId?: string
  nextAction?: string
  updatedAt?: string
  evidenceCount?: number
  incidentCount?: number
  todoSummary?: TodoSummary | TodoSummaries
  policy?: JsonObject
  [key: string]: unknown
}

export type StrictMetricValue = boolean | number | string

export interface CompletionPredicateReview {
  predicateId: string
  metrics: Record<string, StrictMetricValue>
  artifactRefs: string[]
}

export type TodoReviewVerdict = 'confirmed' | 'rejected' | 'review_required'

export interface AgentTodoReview {
  todoInstanceId: string
  verdict: TodoReviewVerdict
  reasonCode: string
  artifactRefs: string[]
}

export interface CompletionReviewRequiredTodo {
  todoInstanceId: string
  operation: string
  status: TodoStatus
  artifactRefs: string[]
}

export interface CompletionReviewScope {
  schemaVersion: 3
  batchId: string
  gameId: string
  runId: string
  runAttemptId: string
  attemptLineageIds: string[]
  gameDayKey: string
  periodStartsAt: string
  periodEndsAt: string
  requiredTodoInstanceIds: string[]
  requiredTodos: CompletionReviewRequiredTodo[]
}

export type CompletionReviewConstraintOperator = 'equals' | 'at_least'
export type CompletionReviewExpectedValue = number | string | boolean

export interface CompletionReviewMetricConstraint {
  metric: string
  operator: CompletionReviewConstraintOperator
  expected: CompletionReviewExpectedValue
}

export interface CompletionReviewPredicateContract {
  predicateId: string
  label?: string
  description?: string
  constraints: CompletionReviewMetricConstraint[]
  allowedArtifactKinds: string[]
}

export interface CompletionTodoReviewContract {
  requiredForAccepted: true
  acceptedVerdict: 'confirmed'
  reasonCodeRequired: true
  artifactRefsRequired: true
  allowedEvidenceContentTypes: Array<'image/jpeg' | 'image/png'>
}

export interface CompletionReviewContract {
  schemaVersion: 'completion-review-contract/v2'
  policyId: string
  policyVersion: string
  gameId: string
  cadence: TodoCadence
  timezone: string
  resetTime: string
  periodStartsAt: string
  periodEndsAt: string
  policyStatus: 'supported' | 'unsupported'
  unsupportedReason: string
  todoReviewContract: CompletionTodoReviewContract
  predicates: CompletionReviewPredicateContract[]
}

export interface CompletionReviewSubmission {
  gameId: string
  runId: string
  runAttemptId: string
  gameDayKey: string
  predicates: CompletionPredicateReview[]
  todoReviews: AgentTodoReview[]
}

export type AgentTodoDifficulty = 'easy' | 'moderate' | 'hard' | 'unsupported' | 'unknown'

export interface TodoDiagnosisSubmission {
  todoInstanceId: string
  difficulty: AgentTodoDifficulty
  automatable: boolean
  confidence: number
  basis: string[]
  failureStage: string
  issue: string
  recommendation: string
  evidenceIds: string[]
}

export interface LegacyTodoDiagnosisSubmission {
  todoInstanceId: string
  difficulty: AgentTodoDifficulty
  confidence: number
  basis: string[]
  failureStage?: string
  recommendation?: string
}

interface ClaimDecisionCreateRequestBase {
  claimId: string
  fencingToken: string
  decision: 'accepted' | 'rejected' | 'review_required'
  reason: string
  evidenceIds: string[]
  requestedBy: string
  completionReview?: CompletionReviewSubmission
}

export type ClaimDecisionCreateRequest = ClaimDecisionCreateRequestBase & (
  | { todoDiagnoses?: TodoDiagnosisSubmission[]; todoDiagnosis?: never }
  /** @deprecated Transitional input for Manager versions that only accept one Todo diagnosis. */
  | { todoDiagnoses?: never; todoDiagnosis: LegacyTodoDiagnosisSubmission }
)

export interface CompletionReview {
  schemaVersion?: 2
  completionReviewId: string
  workItemId: string
  claimId: string
  decisionId: string
  reviewerPrincipalId: string
  decision: 'accepted' | 'rejected' | 'review_required'
  gameId: string
  runId: string
  runAttemptId: string
  gameDayKey: string
  completionPolicyId?: string
  completionPolicyVersion?: string
  attemptLineageIds?: string[]
  predicates: CompletionPredicateReview[]
  todoReviews?: AgentTodoReview[]
  artifactRefs: string[]
  reviewedAt: string
  createdAt: string
}

export interface CompletionPredicateEvaluation {
  code: string
  state: 'satisfied' | 'missing' | 'blocked'
  message: string
  artifactRefs: string[]
  todoInstanceIds: string[]
}

export interface CompletionExcludedEvidence {
  artifactId: string
  reasons: string[]
}

export interface CompletionContractDecision {
  outcome: 'accepted_done' | 'review_required' | 'blocked'
  acceptedDone: boolean
  gameId: string
  runId: string
  runAttemptId?: string | null
  gameDayKey: string
  attemptLineageIds?: string[]
  currentAttemptEvidenceRefs?: string[]
  carriedEvidenceRefs?: string[]
  evaluations: CompletionPredicateEvaluation[]
  missingPredicates: string[]
  blockingPredicates: string[]
  blockerIds: string[]
  artifactRefs: string[]
  screenshotArtifactRefs: string[]
  acceptedEvidenceRefs: string[]
  supportingArtifactRefs: string[]
  excludedEvidence: CompletionExcludedEvidence[]
  reviewId?: string | null
  message: string
}

export interface CompletionAdjudication {
  completionAdjudicationId: string
  batchId: string
  gameId: string
  runId: string
  runAttemptId?: string | null
  gameDayKey: string
  contract: CompletionContractDecision
  createdAt: string
}

export interface BatchTodoSnapshot {
  summary?: TodoSummary
  instances?: TodoInstance[]
}

export interface BatchLineage {
  predecessorBatchId: string | null
  rootBatchId: string
  continuationOrdinal: number
  resumeIntentId: string | null
  runIds: string[]
}

export interface BatchActionAvailability {
  resume: boolean
  cancel: boolean
  review: boolean
  humanTakeover?: boolean
  humanTakeoverTargets?: Array<{ runId: string; gameId: string }>
  /** Terminal member runs resume independently; this is not batch.resume. */
  runResume?: boolean
  runResumeTargets?: Array<{
    runId: string
    gameId: string
    expectedRunRevision?: number
    expectedCurrentAttemptId?: string
    expectedCurrentAttemptRevision?: number
  }>
  /** A typed resume request is available only to record missing facts; it will not start Host. */
  runReconcile?: boolean
  runReconcileTargets?: Array<{
    runId: string
    gameId: string
    expectedRunRevision?: number
    expectedCurrentAttemptId?: string
    expectedCurrentAttemptRevision?: number
    executionWillStart: false
    reasonCode: string
    requirements: string[]
    unavailableRequirements: string[]
    deferredTodoInstanceIds: string[]
  }>
  nextAction: string
  reasonCode: string
  reason: string
}

export interface BatchResult extends JsonObject {
  /** Manager preflight projection. It is advisory in the UI; execution rechecks it. */
  candidateGameIds?: string[]
  executableGameIds?: string[]
  deferredGameIds?: string[]
  skippedCompletedGameIds?: string[]
  gameDay?: string
  awaitingCompletionReviewRunIds?: string[]
  completionReviewWorkItemIds?: Record<string, string>
  completionReviewPhase?: JsonObject
  finalGameRunIds?: string[]
  todoSnapshot?: Record<string, BatchTodoSnapshot>
  todoScope?: {
    schemaVersion?: number
    scopeKey?: string
    scopeFingerprint?: string
    games?: Array<{
      gameId: string
      periodKeys?: string[]
      completionTodoInstanceIds?: string[]
    }>
  }
  todoPlans?: Record<string, {
    selectedTodoDefinitionIds?: string[]
    periodKeys?: string[]
    completionTodoInstanceIds?: string[]
  }>
  completionContracts?: Array<CompletionContractDecision & { completionAdjudicationId?: string }>
  unresolvedRequiredTodoIds?: string[]
  sealEvidenceArtifactIds?: string[]
  currentAttemptEvidenceArtifactIds?: string[]
  carriedEvidenceArtifactIds?: string[]
  currentGameId?: string
  batchLineage?: BatchLineage
  batchActionAvailability?: BatchActionAvailability
  acceptedDone?: boolean
  acceptanceReason?: string
  sealVersion?: number
}

export interface ExecutionControlSummary extends JsonObject {
  activeControllerLeaseCount?: number
  activeTodoBlockerCount?: number
  factCounts?: Record<string, number>
  providers?: Record<string, string>
}

export interface BatchRunMembership {
  batchId: string
  runId: string
  state: 'queued' | 'resume_pending' | 'active' | 'terminal' | 'cancelled' | 'reconciliation_required'
  latestRunAttemptId?: string | null
}

export interface BatchRun {
  batchId: string
  cadence?: TodoCadence
  mode?: 'plan' | 'execute'
  trigger?: string
  state: string
  requestedBy?: string
  createdAt?: string
  updatedAt?: string
  startedAt?: string
  finishedAt?: string
  deadline?: string
  gameIds?: string[]
  runMemberships?: BatchRunMembership[]
  currentGameId?: string
  progress?: number
  result?: BatchResult
  [key: string]: unknown
}

export interface ManagerSnapshot {
  stateVersion: number
  generatedAt?: string
  eventCursor?: string
  manager?: ManagerIdentity
  health?: HealthStatus
  gameDay?: string
  activeBatch?: BatchRun | null
  recentBatches?: BatchRun[]
  games: GameState[]
  counters?: Record<string, number>
  todo?: TodoOverview
  executionControl?: ExecutionControlSummary
  notifications?: NotificationDelivery[]
  [key: string]: unknown
}

export interface ManagerEvent<T = JsonObject> {
  eventId: string
  type: string
  occurredAt?: string
  stateVersion?: number
  cursor?: string
  payload: T
}

export interface CommandReceipt {
  commandId: string
  idempotencyKey: string
  requestId?: string
  statusUrl?: string
  acceptedStateVersion?: number
  state: CommandState
  message?: string
  result?: unknown
  submittedAt?: string
  completedAt?: string
  [key: string]: unknown
}

export interface PageResult<T> {
  items: T[]
  nextCursor?: string
  total?: number
}

export interface GameDetail extends GameState {
  bootstrapStage?: string
  activeRun?: JsonObject | null
  attempts?: JsonObject[]
  runAttempts?: RunAttempt[]
  todoAttempts?: TodoAttempt[]
  attemptAnalysis?: AttemptAnalysis
  controllerLease?: JsonObject | null
  windowBinding?: JsonObject | null
  checkpoints?: JsonObject[]
  artifacts?: EvidenceArtifact[]
  findings?: JsonObject[]
  todoDefinitions?: TodoDefinition[]
  todoInstances?: TodoInstance[]
  todoSummary?: TodoSummaries
  progress?: TodoProgress
  nextResetAt?: string | null
  unresolvedRequiredTodoIds?: string[]
}

export interface AgentWorkItem {
  workItemId: string
  kind: string
  state?: string
  batchId?: string
  runId?: string
  attemptId?: string
  gameId?: string
  cadence?: TodoCadence
  runtimeStage?: string
  artifactRefs?: string[]
  knownFindings?: string[]
  allowedCapabilityRefs?: string[]
  forbiddenClasses?: string[]
  claimExpiresAt?: string
  expectedStateVersion?: number
  requestedBy?: string
  note?: string
  createdAt?: string
  updatedAt?: string
  result?: JsonObject
  [key: string]: unknown
}

export interface AutomationAssessment extends JsonObject {
  assessmentId: string
  todoInstanceId: string
  workItemId: string
  claimId: string
  decisionId: string
  gameId: string
  runId?: string | null
  difficulty: AgentTodoDifficulty
  automatable?: boolean | null
  confidence: number
  basis: string[]
  failureStage?: string
  issue?: string
  recommendation?: string
  evidenceIds: string[]
  requestedBy: string
  createdAt: string
}

export interface WorkItemClaim {
  claimId: string
  workItemId: string
  claimant: string
  state: string
  claimedAt?: string
  expiresAt?: string
  [key: string]: unknown
}

export interface Incident {
  incidentId: string
  fingerprint?: string
  title?: string
  gameId?: string
  state: string
  severity?: string
  occurrenceCount?: number
  retryEligibility?: string
  lastSeenAt?: string
  findings?: JsonObject[]
  [key: string]: unknown
}

export interface EvidenceArtifact {
  artifactId: string
  kind?: string
  hash?: string
  sizeBytes?: number
  fileName?: string
  capturedAt?: string
  source?: string
  raw?: boolean
  contentType?: string
  thumbnailUrl?: string
  runId?: string
  runAttemptId?: string
  todoInstanceId?: string
  todoAttemptId?: string
  gameDayKey?: string
  gameId?: string
  verdict?: string
  [key: string]: unknown
}

export interface WeeklyTask {
  weeklyId: string
  gameId?: string
  displayName: string
  enabled?: boolean
  state?: string
  acceptanceState?: AcceptanceState
  lastRunAt?: string
  nextResetAt?: string
  capabilityRef?: string
  [key: string]: unknown
}

export interface AdapterInfo {
  adapterId: string
  displayName?: string
  gameId?: string
  activeVersion?: string
  activeVersionId?: string
  candidateVersion?: string
  candidateVersionId?: string
  rollbackVersionId?: string
  previousVerifiedVersionId?: string
  stage?: string
  health?: string
  capabilityCount?: number
  implementationHash?: string
  hostId?: string
  hostVersion?: string
  hostHealthy?: boolean
  executionReady?: boolean
  executionPackageStatus?: string
  [key: string]: unknown
}

export interface LogEntry {
  sequence?: number
  logId?: string
  timestamp: string
  level: string
  source?: string
  message: string
  /** Structured producer fields. Free-text message remains diagnostic only. */
  phase?: string
  stage?: string
  observedState?: string
  decision?: string
  reasonCode?: string
  reason?: string
  detail?: string
  eventType?: string
  entityType?: string
  entityId?: string
  gameId?: string
  todoInstanceId?: string
  todoAttemptId?: string
  runAttemptId?: string
  batchId?: string
  runId?: string
  attemptId?: string
  [key: string]: unknown
}

export interface CapabilityDefinition {
  capabilityId: string
  version: string
  displayName?: string
  risk: string
  description?: string
  enabled?: boolean
  requiresIdempotencyKey?: boolean
  inputSchema?: JsonObject
  implementationHash?: string
  outputSchema?: JsonObject
  policy?: JsonObject
  preEvidence?: string[]
  postEvidence?: string[]
  [key: string]: unknown
}

export interface ManagerResource<TDocument extends JsonObject = JsonObject> {
  resourceId: string
  resourceType: string
  state: string
  document: TDocument
  createdAt?: string
  updatedAt?: string
  [key: string]: unknown
}

export interface DiagnosticBundleDocument extends JsonObject {
  artifactId: string
  hash?: string
  batchId?: string
  runId?: string
  incidentId?: string
  requestedBy?: string
}

export type DiagnosticBundle = ManagerResource<DiagnosticBundleDocument>

export interface AdapterDiagnosticCanaryDocument extends JsonObject {
  adapterId: string
  gameId: string
  canaryStatus: string
  managerOwned: boolean
  diagnosticOnly: boolean
  hostId?: string
  hostVersion?: string
  hostHealthy?: boolean
  hostImplementationHash?: string
  protocolVersion?: string
  supportedOperations?: string[]
  executionPackage?: {
    packageId?: string
    packageVersion?: string
    entryPoint?: string
    status?: string
    sha256?: string | null
  }
  executionGateEnabled: boolean
  activeExecution: boolean
  executionReady: boolean
  adapterProcessStarted: boolean
  gameProcessStarted: boolean
  requestedBy?: string
  note?: string
}

export type AdapterDiagnosticCanary = ManagerResource<AdapterDiagnosticCanaryDocument>

export interface RepairSessionDocument extends JsonObject {
  incidentId: string
  gameId?: string
  reason?: string
  requestedBy?: string
  productionMutationAllowed?: boolean
  lastVerificationId?: string
  lastVerdict?: RepairVerificationVerdict
}

export type RepairSession = ManagerResource<RepairSessionDocument>
export type RepairVerificationVerdict = 'passed' | 'failed' | 'needs_more_evidence'

export type NotificationDeliveryState = 'draft' | 'sending' | 'sent' | 'failed'
export type NotificationDispatchGate = 'automatic' | 'disabled' | 'secret_missing' | 'manual_review'
export type NotificationOutcome = 'completed' | 'blocked'
export type NotificationAttemptState = 'sending' | 'sent' | 'failed'
export type NotificationAttemptOutcome = 'sent' | 'transient_failure' | 'permanent_failure' | 'ambiguous'
export type NotificationSecretState = 'configured' | 'missing' | 'invalid'

export interface NotificationDelivery {
  notificationId: string
  batchId: string
  sealVersion: number
  channel: 'email'
  recipientBindingId: string
  messageId: string
  state: NotificationDeliveryState
  dispatchGate: NotificationDispatchGate
  outcome: NotificationOutcome
  subject: string
  attachmentRefs: string[]
  attemptCount: number
  nextAttemptAt?: string | null
  lastErrorClass: string
  sentAt?: string | null
  createdAt: string
  updatedAt: string
}

export interface NotificationAttempt {
  attemptId: string
  notificationId: string
  attemptNumber: number
  state: NotificationAttemptState
  outcome?: NotificationAttemptOutcome | null
  errorClass: string
  retryScheduledAt?: string | null
  transportReceiptHash: string
  startedAt: string
  completedAt?: string | null
  createdAt: string
}

export interface NotificationAttachmentDecision {
  artifactId: string
  accepted: boolean
  reason: string
}

export interface NotificationPreview {
  notificationId: string
  subject: string
  textBody: string
  htmlBody: string
  attachmentDecisions: NotificationAttachmentDecision[]
}

export interface NotificationPolicy {
  enabled: boolean
  automaticDispatch: boolean
  channel: 'email'
  recipientBindingId: string
  secretState: NotificationSecretState
  updatedBy: string
  createdAt: string
  updatedAt: string
}

export interface NotificationSendRequest {
  reason: string
  requestedBy: 'webgui'
}

export interface NotificationRetryRequest extends NotificationSendRequest {
  confirmAmbiguous: boolean
}

export interface ConfigDocument {
  version?: string
  stateVersion?: number
  config: JsonObject
  schema?: JsonObject
  allowedGameIds?: string[]
  legacySources?: JsonObject
  updatedAt?: string
  [key: string]: unknown
}

export interface PolicyDocument {
  version?: string
  policy: JsonObject
  forbiddenClasses?: string[]
  updatedAt?: string
  [key: string]: unknown
}

export interface ApiProblem {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
  requestId?: string
  code?: string
  errors?: unknown
  [key: string]: unknown
}

export interface CommandOptions {
  expectedStateVersion?: number
  idempotencyKey?: string
  requestId?: string
  headers?: Record<string, string>
}

export function extractItems<T>(value: T[] | PageResult<T> | { data?: T[] } | null | undefined): T[] {
  if (Array.isArray(value)) return value
  if (!value || typeof value !== 'object') return []
  if ('items' in value && Array.isArray(value.items)) return value.items
  if ('data' in value && Array.isArray(value.data)) return value.data
  const record = value as Record<string, unknown>
  for (const key of ['games', 'batches', 'gameRuns', 'workItems', 'capabilities', 'events', 'logs', 'incidents', 'artifacts', 'adapters', 'weeklyTasks', 'notifications', 'completionReviews', 'completionAdjudications']) {
    if (Array.isArray(record[key])) return record[key] as T[]
  }
  return []
}

export function recordOf(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonObject)
    : {}
}

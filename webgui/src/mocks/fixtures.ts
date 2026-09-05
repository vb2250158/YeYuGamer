import type {
  AdapterInfo,
  AgentWorkItem,
  AutomationAssessment,
  BatchRun,
  CapabilityDefinition,
  CommandReceipt,
  CompletionAdjudication,
  CompletionReview,
  ConfigDocument,
  DiagnosticBundle,
  EvidenceArtifact,
  GameState,
  Incident,
  LogEntry,
  ManagerSnapshot,
  NotificationAttempt,
  NotificationDelivery,
  NotificationPolicy,
  NotificationPreview,
  PolicyDocument,
  RepairSession,
  TodoDefinition,
  TodoInstance,
  TodoSummary,
  WeeklyTask,
} from '../api/contracts'
import { todoSummaryFromItems } from '../utils/todos'

const now = () => new Date().toISOString()
const clone = <T>(value: T): T => structuredClone(value)

const resetRule = { timezone: 'Asia/Shanghai', time: '04:00', cadence: 'daily' as const }
const sourceHash = 'a'.repeat(64)
const todoDefinitions: TodoDefinition[] = [
  {
    todoDefinitionId: 'todo.v1.starrail.daily.spend_stamina', definitionVersion: 1, catalogVersion: 'mock-1', sourceHash,
    gameId: 'StarRail', cadence: 'daily', operation: 'spend_stamina', title: '消耗开拓力', category: 'stamina', orderIndex: 10,
    required: true, risk: 'routine_action', automationDifficulty: 'low', adapterCapabilityRef: 'game.daily.run@1.0', automationState: 'implemented',
    initialStatus: 'pending', resetRule, sourceRefs: ['March7thAssistant/config'], active: true, updatedAt: now(),
  },
  {
    todoDefinitionId: 'todo.v1.starrail.daily.claim_daily', definitionVersion: 1, catalogVersion: 'mock-1', sourceHash,
    gameId: 'StarRail', cadence: 'daily', operation: 'claim_daily', title: '领取每日奖励', category: 'reward', orderIndex: 20,
    required: true, risk: 'routine_action', automationDifficulty: 'medium', adapterCapabilityRef: 'game.daily.run@1.0', automationState: 'implemented',
    initialStatus: 'pending', resetRule, sourceRefs: ['March7thAssistant/config'], active: true, updatedAt: now(),
  },
  {
    todoDefinitionId: 'todo.v1.zzz.daily.login', definitionVersion: 1, catalogVersion: 'mock-1', sourceHash,
    gameId: 'ZZZ', cadence: 'daily', operation: 'login', title: '登录并确认主界面', category: 'login', orderIndex: 10,
    required: true, risk: 'approval_required', automationDifficulty: 'high', automationState: 'manual_review',
    initialStatus: 'review_required', initialReason: '登录状态需要人工确认', resetRule, sourceRefs: ['one_dragon/_group'], active: true, updatedAt: now(),
  },
]

const todoInstances: TodoInstance[] = todoDefinitions.map((definition, index) => ({
  todoInstanceId: `mock-${definition.todoDefinitionId}`,
  todoDefinitionId: definition.todoDefinitionId,
  definitionVersion: definition.definitionVersion,
  catalogVersion: definition.catalogVersion,
  sourceHash: definition.sourceHash,
  gameId: definition.gameId,
  cadence: definition.cadence,
  periodKey: 'daily:2026-08-27',
  periodStartsAt: '2026-08-27T04:00:00+08:00',
  periodEndsAt: '2026-08-28T04:00:00+08:00',
  operation: definition.operation,
  title: definition.title,
  category: definition.category,
  orderIndex: definition.orderIndex,
  required: definition.required,
  risk: definition.risk,
  automationDifficulty: definition.automationDifficulty,
  adapterCapabilityRef: definition.adapterCapabilityRef,
  automationState: definition.automationState,
  resetRule: definition.resetRule,
  sourceRefs: definition.sourceRefs,
  status: index === 0 ? 'completed' : index === 2 ? 'human_required' : definition.initialStatus,
  dispatchDisposition: index === 0 ? 'completed_skip' : index === 2 ? 'deferred_human' : 'eligible',
  dispatchReason: index === 0
    ? 'Manager 已确认当前周期 Todo 完成，后续调度跳过。'
    : index === 2
      ? 'Manager 检测到人工登录门，等待显式释放。'
      : 'Manager 已确认 Todo 可由当前 Adapter 能力执行。',
  actionAvailability: index === 0
    ? { execute: false, resume: false, review: false, releaseHuman: false, submitManualEvidence: false, nextAction: '等待下一周期重置' }
    : index === 2
      ? { execute: false, resume: false, review: true, releaseHuman: true, submitManualEvidence: true, nextAction: '人工完成登录后释放接管' }
      : { execute: true, resume: false, review: false, releaseHuman: false, submitManualEvidence: false, nextAction: '等待 Manager 调度执行' },
  attempts: index + 1,
  reason: index === 0 ? '同 run 证据已登记' : index === 2 ? '等待操作者完成登录并显式释放' : definition.initialReason,
  evidenceRefs: index === 0 ? ['artifact_star_before'] : [],
  createdAt: now(),
  updatedAt: now(),
}))

const automationAssessments: AutomationAssessment[] = [
  {
    assessmentId: 'assessment_mock_star_claim',
    todoInstanceId: 'mock-todo.v1.starrail.daily.claim_daily',
    workItemId: 'wi_star_reward',
    claimId: 'claim_star_reward',
    decisionId: 'decision_star_reward',
    gameId: 'StarRail',
    runId: 'run_star_0827',
    difficulty: 'moderate',
    automatable: true,
    confidence: 0.86,
    basis: ['每日实训奖励需要结构化画面复核', '当前 Adapter 已能进入每日实训页面'],
    failureStage: 'completion_review',
    issue: '当前奖励画面仍缺少 Manager 可验收的完整结构化证据。',
    recommendation: '继续采集同一 GameRun 当前 attempt 的原始奖励画面。',
    evidenceIds: ['artifact_star_reward'],
    requestedBy: 'mock-agent',
    createdAt: now(),
  },
  {
    assessmentId: 'assessment_mock_zzz_login',
    todoInstanceId: 'mock-todo.v1.zzz.daily.login',
    workItemId: 'wi_zzz_login',
    claimId: 'claim_zzz_login',
    decisionId: 'decision_zzz_login',
    gameId: 'ZZZ',
    runId: 'run_zzz_0827',
    difficulty: 'unsupported',
    automatable: false,
    confidence: 0.99,
    basis: ['登录门需要操作者确认，不能由 Adapter 越权处理'],
    failureStage: 'login_gate',
    issue: '人工登录门禁止 Adapter 自主越过。',
    recommendation: '人工登录后通过 Manager 释放接管。',
    evidenceIds: [],
    requestedBy: 'mock-agent',
    createdAt: now(),
  },
  {
    assessmentId: 'assessment_mock_star_stamina',
    todoInstanceId: 'mock-todo.v1.starrail.daily.spend_stamina',
    workItemId: 'wi_star_reward',
    claimId: 'claim_star_reward',
    decisionId: 'decision_star_stamina',
    gameId: 'StarRail',
    runId: 'run_star_0827',
    difficulty: 'easy',
    automatable: true,
    confidence: 0.95,
    basis: ['Manager 已登记当前 TodoAttempt completed 与证据引用'],
    failureStage: '',
    issue: '',
    recommendation: '后续调度继续遵从 completed_skip。',
    evidenceIds: ['artifact_star_before'],
    requestedBy: 'mock-agent',
    createdAt: now(),
  },
]

function mockTodoSummary(gameId: string): TodoSummary {
  return todoSummaryFromItems(todoInstances.filter((item) => item.gameId === gameId), 'daily')
}

const games: GameState[] = [
  {
    gameId: 'StarRail',
    displayName: '崩坏：星穹铁道',
    enabled: true,
    runtimeState: 'verifying',
    acceptanceState: 'evidence_pending',
    reviewState: 'agent_queued',
    stage: 'daily_panel_ready',
    runId: 'run_star_0827',
    batchId: 'batch_0827',
    nextAction: '等待 Agent 复核奖励证据',
    updatedAt: now(),
    evidenceCount: 3,
    incidentCount: 0,
    todoSummary: mockTodoSummary('StarRail'),
    bootstrapStage: 'daily_panel_ready',
    controllerLease: { owner: 'manager', fencingToken: 17, expiresAt: '2026-08-27T16:12:00+08:00' },
    windowBinding: { pid: 24116, bindingVersion: 3, captureProvider: 'windows-graphics-capture' },
    checkpoints: [{ checkpointId: 'cp_star_stamina', kind: 'stamina_route', version: 2 }],
  },
  {
    gameId: 'Endfield',
    displayName: '明日方舟：终末地',
    enabled: true,
    runtimeState: 'retry_wait',
    acceptanceState: 'in_progress',
    reviewState: 'none',
    stage: 'main_ready',
    runId: 'run_end_0827',
    batchId: 'batch_0827',
    nextAction: '冷却 34 秒后重新观察主界面',
    updatedAt: now(),
    evidenceCount: 1,
    incidentCount: 1,
    todoSummary: mockTodoSummary('Endfield'),
  },
  {
    gameId: 'ZZZ',
    displayName: '绝区零',
    enabled: true,
    runtimeState: 'human_takeover',
    acceptanceState: 'unknown',
    reviewState: 'human_required',
    stage: 'login_gate',
    nextAction: '等待人工完成登录，不自动重试',
    updatedAt: now(),
    evidenceCount: 2,
    incidentCount: 1,
    todoSummary: mockTodoSummary('ZZZ'),
  },
  {
    gameId: 'WutheringWaves',
    displayName: '鸣潮',
    enabled: false,
    runtimeState: 'planned',
    acceptanceState: 'not_started',
    reviewState: 'none',
    nextAction: '未加入今日范围',
    updatedAt: now(),
    todoSummary: mockTodoSummary('WutheringWaves'),
  },
]

const workItems: AgentWorkItem[] = [
  {
    workItemId: 'wi_star_reward',
    kind: 'evidence_review',
    state: 'open',
    batchId: 'batch_0827',
    gameId: 'StarRail',
    cadence: 'daily',
    runId: 'run_star_0827',
    runtimeStage: 'verifying',
    artifactRefs: ['artifact_star_reward', 'artifact_star_before'],
    knownFindings: ['finding_daily_panel_reached'],
    allowedCapabilityRefs: ['starrail.observe_daily_reward@1'],
    forbiddenClasses: ['purchase', 'draw', 'account_settings'],
    expectedStateVersion: 42,
    result: {
      todoSnapshot: {
        items: todoInstances.filter((item) => item.gameId === 'StarRail').map((item) => {
          const isStamina = item.operation === 'spend_stamina'
          return {
            ...item,
            dispatch: {
              disposition: item.dispatchDisposition,
              reason: item.dispatchReason,
              actionAvailability: item.actionAvailability,
            },
            latestTodoAttempt: {
              todoAttemptId: isStamina ? 'todo-attempt-star-stamina' : 'todo-attempt-star-claim',
              runAttemptId: 'attempt_star_1',
              todoInstanceId: item.todoInstanceId,
              attemptNumber: item.attempts,
              operation: item.operation,
              state: isStamina ? 'completed' : 'review_required',
              reasonCode: isStamina ? 'adapter_confirmed' : 'evidence_pending',
              reason: isStamina ? '同 run 体力消耗证据已登记' : '等待核验 500/500 与 5/5',
              retryable: !isStamina,
              evidenceRefs: isStamina ? ['artifact_star_before'] : ['artifact_star_reward'],
              startedAt: '2026-08-27T15:51:00+08:00',
              completedAt: '2026-08-27T15:53:00+08:00',
              createdAt: '2026-08-27T15:51:00+08:00',
              updatedAt: '2026-08-27T15:53:00+08:00',
            },
            activeBlocker: isStamina ? null : {
              blockerId: 'blocker-star-claim-review',
              kind: 'review_required',
              reasonCode: 'evidence_pending',
              reason: '等待同一 attempt 的奖励截图复核',
            },
            recentExecutionFacts: [{
              factId: isStamina ? 'fact-star-stamina' : 'fact-star-claim',
              factType: isStamina ? 'action_receipt' : 'checkpoint',
              gameId: 'StarRail',
              runId: 'run_star_0827',
              runAttemptId: 'attempt_star_1',
              todoInstanceId: item.todoInstanceId,
              gameDayKey: 'daily:2026-08-27',
              document: {},
              createdAt: '2026-08-27T15:53:00+08:00',
            }],
            latestAutomationAssessment: automationAssessments.find((assessment) => assessment.todoInstanceId === item.todoInstanceId),
          }
        }),
      },
      completionReviewScope: {
        schemaVersion: 3,
        batchId: 'batch_0827',
        gameId: 'StarRail',
        runId: 'run_star_0827',
        runAttemptId: 'attempt_star_1',
        attemptLineageIds: ['attempt_star_0', 'attempt_star_1'],
        gameDayKey: 'daily:2026-08-27',
        periodStartsAt: '2026-08-27T04:00:00+08:00',
        periodEndsAt: '2026-08-28T04:00:00+08:00',
        requiredTodoInstanceIds: [
          'mock-todo.v1.starrail.daily.spend_stamina',
          'mock-todo.v1.starrail.daily.claim_daily',
        ],
        requiredTodos: [
          {
            todoInstanceId: 'mock-todo.v1.starrail.daily.spend_stamina',
            operation: 'spend_stamina',
            status: 'completed',
            artifactRefs: ['artifact_star_before'],
          },
          {
            todoInstanceId: 'mock-todo.v1.starrail.daily.claim_daily',
            operation: 'claim_daily',
            status: 'review_required',
            artifactRefs: ['artifact_star_reward'],
          },
        ],
      },
      completionReviewContract: {
        schemaVersion: 'completion-review-contract/v2',
        policyId: 'StarRail.daily',
        policyVersion: '1+sha256.mock',
        gameId: 'StarRail',
        cadence: 'daily',
        timezone: 'Asia/Shanghai',
        resetTime: '04:00',
        periodStartsAt: '2026-08-27T04:00:00+08:00',
        periodEndsAt: '2026-08-28T04:00:00+08:00',
        policyStatus: 'supported',
        unsupportedReason: null,
        todoReviewContract: {
          requiredForAccepted: true,
          acceptedVerdict: 'confirmed',
          reasonCodeRequired: true,
          artifactRefsRequired: true,
          allowedEvidenceContentTypes: ['image/jpeg', 'image/png'],
        },
        predicates: [
          {
            predicateId: 'starrail.daily_training.points',
            label: '每日实训点数',
            description: '观察当前点数与目标点数。',
            constraints: [
              { metric: 'current', operator: 'equals', expected: 500 },
              { metric: 'target', operator: 'equals', expected: 500 },
            ],
            allowedArtifactKinds: ['raw_frame'],
          },
          {
            predicateId: 'starrail.daily_training.reward_tiers',
            label: '每日实训奖励档位',
            constraints: [
              { metric: 'claimed', operator: 'equals', expected: 5 },
              { metric: 'total', operator: 'equals', expected: 5 },
            ],
            allowedArtifactKinds: ['raw_frame'],
          },
        ],
      },
      attemptAnalysis: {
        problemSignals: [
          {
            todoInstanceId: 'mock-todo.v1.starrail.daily.claim_daily',
            gameId: 'StarRail',
            title: '领取每日奖励',
            status: 'review_required',
            attemptCount: 2,
            reviewRequiredCount: 1,
            humanRequiredCount: 0,
            latestReason: '需要确认 500/500 与 5/5',
            evidenceRefs: ['artifact_star_reward'],
          },
          {
            todoInstanceId: 'mock-todo.v1.starrail.daily.spend_stamina',
            gameId: 'StarRail',
            title: '消耗开拓力',
            status: 'completed',
            attemptCount: 1,
            blockedCount: 0,
            latestReason: '同 run 体力消耗证据已登记',
            evidenceRefs: ['artifact_star_before'],
          },
        ],
      },
    },
  },
  {
    workItemId: 'wi_end_retry',
    kind: 'incident_triage',
    state: 'claimed',
    gameId: 'Endfield',
    runId: 'run_end_0827',
    artifactRefs: ['artifact_end_static'],
    allowedCapabilityRefs: [],
    claimExpiresAt: '2026-08-27T16:20:00+08:00',
    expectedStateVersion: 42,
  },
]

const incidents: Incident[] = [
  {
    incidentId: 'inc_end_static',
    fingerprint: 'endfield:main_ready:no_delta:v2',
    title: '主界面连续观察无变化',
    gameId: 'Endfield',
    state: 'open',
    severity: 'warning',
    occurrenceCount: 3,
    retryEligibility: 'after_cooldown',
    lastSeenAt: now(),
  },
  {
    incidentId: 'inc_zzz_login',
    fingerprint: 'zzz:login_gate:human_required',
    title: '登录状态需要人工接管',
    gameId: 'ZZZ',
    state: 'awaiting_human',
    severity: 'critical',
    occurrenceCount: 1,
    retryEligibility: 'explicit_release_only',
    lastSeenAt: now(),
  },
]

const artifacts: EvidenceArtifact[] = [
  {
    artifactId: 'artifact_star_reward',
    kind: 'raw_frame',
    hash: 'sha256:d46f…19ce',
    capturedAt: now(),
    source: 'windows-graphics-capture',
    raw: true,
    contentType: 'image/png',
    gameId: 'StarRail',
    runId: 'run_star_0827',
    runAttemptId: 'attempt_star_1',
    todoInstanceId: 'mock-todo.v1.starrail.daily.claim_daily',
    verdict: 'pending_review',
  },
  {
    artifactId: 'artifact_end_static',
    kind: 'derived_diff',
    hash: 'sha256:ab07…f2d9',
    capturedAt: now(),
    source: 'frame-diff',
    raw: false,
    contentType: 'image/png',
    gameId: 'Endfield',
    runId: 'run_end_0827',
    verdict: 'no_meaningful_delta',
  },
  {
    artifactId: 'artifact_star_before',
    kind: 'raw_frame',
    hash: 'sha256:31b2…ba17',
    capturedAt: now(),
    source: 'windows-graphics-capture',
    raw: true,
    contentType: 'image/png',
    gameId: 'StarRail',
    runId: 'run_star_0827',
    runAttemptId: 'attempt_star_0',
    todoInstanceId: 'mock-todo.v1.starrail.daily.spend_stamina',
    verdict: 'context_only',
  },
]

const mockCompletionContract: CompletionAdjudication['contract'] = {
  outcome: 'review_required',
  acceptedDone: false,
  gameId: 'StarRail',
  runId: 'run_star_0827',
  runAttemptId: 'attempt_star_1',
  gameDayKey: 'daily:2026-08-27',
  attemptLineageIds: ['attempt_star_0', 'attempt_star_1'],
  currentAttemptEvidenceRefs: ['artifact_star_reward'],
  carriedEvidenceRefs: ['artifact_star_before'],
  evaluations: [
    {
      code: 'starrail_daily_training_500',
      state: 'missing',
      message: '当前结构化观察为 460/500。',
      artifactRefs: ['artifact_star_reward'],
      todoInstanceIds: ['mock-todo.v1.starrail.daily.claim_daily'],
    },
    {
      code: 'starrail_five_reward_tiers_claimed',
      state: 'missing',
      message: '当前结构化观察为 4/5。',
      artifactRefs: ['artifact_star_reward'],
      todoInstanceIds: ['mock-todo.v1.starrail.daily.claim_daily'],
    },
  ],
  missingPredicates: ['starrail_daily_training_500', 'starrail_five_reward_tiers_claimed'],
  blockingPredicates: [],
  blockerIds: [],
  artifactRefs: ['artifact_star_reward'],
  screenshotArtifactRefs: ['artifact_star_reward'],
  acceptedEvidenceRefs: [],
  supportingArtifactRefs: ['artifact_star_reward'],
  excludedEvidence: [{ artifactId: 'artifact_star_before', reasons: ['attempt_mismatch'] }],
  reviewId: 'completion-review-old',
  message: '当前观察不足以封印 accepted_done，等待同 run 新鲜证据。',
}

const completionReviews: CompletionReview[] = [{
  schemaVersion: 2,
  completionReviewId: 'completion-review-old',
  workItemId: 'wi_star_reward_old',
  claimId: 'claim_star_old',
  decisionId: 'decision_star_old',
  reviewerPrincipalId: 'mock-agent',
  decision: 'review_required',
  gameId: 'StarRail',
  runId: 'run_star_0827',
  runAttemptId: 'attempt_star_1',
  gameDayKey: 'daily:2026-08-27',
  completionPolicyId: 'StarRail.daily',
  completionPolicyVersion: '1+sha256.mock',
  attemptLineageIds: ['attempt_star_0', 'attempt_star_1'],
  predicates: [
    { predicateId: 'starrail.daily_training.points', metrics: { current: 460, target: 500 }, artifactRefs: ['artifact_star_reward'] },
    { predicateId: 'starrail.daily_training.reward_tiers', metrics: { claimed: 4, total: 5 }, artifactRefs: ['artifact_star_reward'] },
  ],
  todoReviews: [],
  artifactRefs: ['artifact_star_reward'],
  reviewedAt: now(),
  createdAt: now(),
}]

const completionAdjudications: CompletionAdjudication[] = [{
  completionAdjudicationId: 'completion-adjudication-old',
  batchId: 'batch_0827',
  gameId: 'StarRail',
  runId: 'run_star_0827',
  runAttemptId: 'attempt_star_1',
  gameDayKey: 'daily:2026-08-27',
  contract: mockCompletionContract,
  createdAt: now(),
}]

const batches: BatchRun[] = [{
  batchId: 'batch_0827',
  cadence: 'daily',
  mode: 'execute',
  state: 'running',
  requestedBy: 'webgui',
  createdAt: '2026-08-27T15:41:00+08:00',
  updatedAt: now(),
  startedAt: '2026-08-27T15:41:00+08:00',
  currentGameId: 'StarRail',
  gameIds: ['StarRail', 'Endfield', 'ZZZ'],
  progress: 44,
  result: {
    gameDay: 'batch:daily:2026-08-27:mock',
    todoScope: {
      schemaVersion: 1,
      scopeKey: 'batch:daily:2026-08-27:mock',
      games: ['StarRail', 'Endfield', 'ZZZ'].map((gameId) => ({
        gameId,
        periodKeys: ['daily:2026-08-27'],
        completionTodoInstanceIds: todoInstances.filter((item) => item.gameId === gameId).map((item) => item.todoInstanceId),
      })),
    },
    batchLineage: {
      predecessorBatchId: null,
      rootBatchId: 'batch_0827',
      continuationOrdinal: 0,
      resumeIntentId: null,
      runIds: ['run_star_0827', 'run_end_0827', 'run_zzz_0827'],
    },
    batchActionAvailability: {
      resume: false,
      cancel: true,
      review: true,
      nextAction: 'review_batch_work_items',
      reasonCode: 'batch_review_required',
      reason: 'Manager requires the current completion review.',
    },
    awaitingCompletionReviewRunIds: ['run_star_0827'],
    completionReviewWorkItemIds: { run_star_0827: 'wi_star_reward' },
    completionReviewPhase: { schemaVersion: 1, status: 'awaiting' },
    finalGameRunIds: [],
    todoSnapshot: Object.fromEntries(games.map((game) => [game.gameId, {
      summary: mockTodoSummary(game.gameId),
      instances: todoInstances.filter((item) => item.gameId === game.gameId),
    }])),
    completionContracts: [{ ...mockCompletionContract, completionAdjudicationId: 'completion-adjudication-old' }],
    currentAttemptEvidenceArtifactIds: ['artifact_star_reward'],
    carriedEvidenceArtifactIds: ['artifact_star_before'],
    unresolvedRequiredTodoIds: todoInstances.filter((item) => item.required && item.status !== 'completed').map((item) => item.todoInstanceId),
    acceptedDone: false,
    acceptanceReason: '等待 StarRail 新一轮 CompletionReview，并保留 ZZZ human_required。',
  },
}]

const weekly: WeeklyTask[] = [
  {
    weeklyId: 'zzz_ridu_weekly',
    gameId: 'ZZZ',
    displayName: '绝区零丽都周常',
    enabled: true,
    state: 'blocked_by_login',
    acceptanceState: 'unknown',
    nextResetAt: '2026-08-31T04:00:00+08:00',
    capabilityRef: 'game.weekly.plan@1.0',
  },
  {
    weeklyId: 'zzz_lost_void',
    gameId: 'ZZZ',
    displayName: '迷失之地悬赏委托',
    enabled: true,
    state: 'not_started',
    acceptanceState: 'not_started',
    nextResetAt: '2026-08-31T04:00:00+08:00',
    capabilityRef: 'game.weekly.plan@1.0',
  },
]

const adapters: AdapterInfo[] = [
  {
    adapterId: 'adapter_starrail',
    displayName: 'StarRail Adapter',
    gameId: 'StarRail',
    activeVersion: '2.3.1',
    candidateVersion: '2.4.0-rc.2',
    stage: 'shadow',
    health: 'healthy',
    capabilityCount: 7,
    implementationHash: 'sha256:08bd…cdd1',
  },
  {
    adapterId: 'adapter_endfield',
    displayName: 'Endfield Adapter',
    gameId: 'Endfield',
    activeVersion: '1.8.0',
    stage: 'production',
    health: 'degraded',
    capabilityCount: 4,
    implementationHash: 'sha256:7b9a…40c2',
  },
]

const capabilities: CapabilityDefinition[] = [
  {
    capabilityId: 'starrail.observe_daily_reward',
    version: '1',
    displayName: '观察星铁每日奖励',
    risk: 'observe_only',
    enabled: true,
  },
  {
    capabilityId: 'game.weekly.plan',
    version: '1.0',
    displayName: '规划单游戏周常',
    risk: 'controlled_write',
    enabled: true,
  },
  {
    capabilityId: 'game.weekly.run',
    version: '1.0',
    displayName: '运行单游戏周常',
    risk: 'routine_action',
    enabled: false,
  },
]

const repairSessions: RepairSession[] = [
  {
    resourceId: 'repair_mock_endfield',
    resourceType: 'repair-session',
    state: 'planned',
    document: {
      incidentId: 'inc_end_static',
      gameId: 'Endfield',
      reason: '隔离复现实验',
      productionMutationAllowed: false,
    },
    createdAt: now(),
    updatedAt: now(),
  },
]

const diagnosticBundles: DiagnosticBundle[] = [
  {
    resourceId: 'diagnostic_mock_global',
    resourceType: 'diagnostic-bundle',
    state: 'ready',
    document: {
      artifactId: 'artifact_diagnostic_mock',
      hash: 'sha256:mock-diagnostic',
      requestedBy: 'webgui',
    },
    createdAt: now(),
    updatedAt: now(),
  },
]

const notificationPolicy: NotificationPolicy = {
  enabled: true,
  automaticDispatch: true,
  channel: 'email',
  recipientBindingId: 'self-primary',
  secretState: 'configured',
  updatedBy: 'webgui',
  createdAt: now(),
  updatedAt: now(),
}

const notifications: NotificationDelivery[] = [
  {
    notificationId: 'notification_mock_ambiguous',
    batchId: 'batch_0827',
    sealVersion: 1,
    channel: 'email',
    recipientBindingId: 'self-primary',
    messageId: 'message_mock_ambiguous',
    state: 'failed',
    dispatchGate: 'manual_review',
    outcome: 'completed',
    subject: '今日游戏任务已完成',
    attachmentRefs: ['artifact_star_reward'],
    attemptCount: 1,
    lastErrorClass: 'transport_ambiguous',
    createdAt: now(),
    updatedAt: now(),
  },
  {
    notificationId: 'notification_mock_blocked',
    batchId: 'batch_0826',
    sealVersion: 2,
    channel: 'email',
    recipientBindingId: 'self-primary',
    messageId: 'message_mock_blocked',
    state: 'draft',
    dispatchGate: 'manual_review',
    outcome: 'blocked',
    subject: '今日游戏任务需要人工处理',
    attachmentRefs: ['artifact_end_static'],
    attemptCount: 0,
    lastErrorClass: 'operator_review_required',
    createdAt: now(),
    updatedAt: now(),
  },
  {
    notificationId: 'notification_mock_sent',
    batchId: 'batch_0825',
    sealVersion: 1,
    channel: 'email',
    recipientBindingId: 'self-primary',
    messageId: 'message_mock_sent',
    state: 'sent',
    dispatchGate: 'automatic',
    outcome: 'completed',
    subject: '昨日游戏任务已完成',
    attachmentRefs: [],
    attemptCount: 1,
    lastErrorClass: '',
    sentAt: now(),
    createdAt: now(),
    updatedAt: now(),
  },
]

const notificationAttempts: Record<string, NotificationAttempt[]> = {
  notification_mock_ambiguous: [
    {
      attemptId: 'notification_attempt_mock_1',
      notificationId: 'notification_mock_ambiguous',
      attemptNumber: 1,
      state: 'failed',
      outcome: 'ambiguous',
      errorClass: 'transport_ambiguous',
      transportReceiptHash: '',
      startedAt: now(),
      completedAt: now(),
      createdAt: now(),
    },
  ],
  notification_mock_blocked: [],
  notification_mock_sent: [
    {
      attemptId: 'notification_attempt_mock_sent',
      notificationId: 'notification_mock_sent',
      attemptNumber: 1,
      state: 'sent',
      outcome: 'sent',
      errorClass: '',
      transportReceiptHash: 'sha256:mock-receipt',
      startedAt: now(),
      completedAt: now(),
      createdAt: now(),
    },
  ],
}

const notificationPreviews: Record<string, NotificationPreview> = Object.fromEntries(
  notifications.map((delivery) => [delivery.notificationId, {
    notificationId: delivery.notificationId,
    subject: delivery.subject,
    textBody: `${delivery.subject}\nBatch: ${delivery.batchId}\nOutcome: ${delivery.outcome}`,
    htmlBody: `<h1>${delivery.subject}</h1><p>Batch: ${delivery.batchId}</p><img src=x onerror="mock-only">`,
    attachmentDecisions: delivery.attachmentRefs.map((artifactId) => ({
      artifactId,
      accepted: artifactId === 'artifact_star_reward',
      reason: artifactId === 'artifact_star_reward' ? 'strict-seal-artifact' : 'derived-artifact-excluded',
    })),
  }]),
)

const logs: LogEntry[] = [
  { timestamp: now(), level: 'info', source: 'manager', message: 'Mock Manager snapshot ready', phase: 'bootstrap', observedState: 'snapshot_available', decision: 'ready', reasonCode: 'mock_snapshot_loaded' },
  { timestamp: now(), level: 'warning', source: 'adapter:endfield', message: 'no meaningful frame delta; cooldown scheduled', phase: 'launch', observedState: 'frame_delta_below_threshold', decision: 'retry_wait', reasonCode: 'observation_ambiguous', runId: 'run_end_0827' },
  { timestamp: now(), level: 'info', source: 'evidence', message: 'artifact artifact_star_reward persisted', phase: 'evidence_register', observedState: 'artifact_persisted', decision: 'awaiting_semantic_review', reasonCode: 'artifact_not_yet_adjudicated', runId: 'run_star_0827' },
]

export function mockSnapshot(): ManagerSnapshot {
  const perGame = Object.fromEntries(games.map((game) => [game.gameId, mockTodoSummary(game.gameId)]))
  const scopeGameIds = games.filter((game) => game.enabled).map((game) => game.gameId)
  const scopeFingerprint = `mock-overview-v1:${scopeGameIds
    .map((gameId) => `${gameId}:${perGame[gameId].scopeFingerprint}`)
    .join('|')}`
  const todo = {
    defaultResetRule: resetRule,
    scopeKey: `daily:manager:${scopeFingerprint.slice(-16)}`,
    scopeFingerprint,
    scopeGameIds,
    games: perGame,
    requiredTotal: Object.values(perGame).reduce((sum, item) => sum + item.requiredTotal, 0),
    requiredCompleted: Object.values(perGame).reduce((sum, item) => sum + item.requiredCompleted, 0),
    requiredRemaining: Object.values(perGame).reduce((sum, item) => sum + item.requiredRemaining, 0),
    blocked: Object.values(perGame).reduce((sum, item) => sum + item.counts.blocked, 0),
    reviewRequired: Object.values(perGame).reduce((sum, item) => sum + item.counts.review_required, 0),
  }
  return clone({
    stateVersion: 42,
    generatedAt: now(),
    eventCursor: 'mock-42',
    gameDay: '2026-08-27',
    manager: { name: 'YeYu Gamer Manager', version: '0.1.0-dev', apiVersion: 'v1', mode: 'mock', legacyExecutionEnabled: false },
    health: { status: 'healthy', storage: 'healthy', eventStream: 'healthy', checkedAt: now() },
    activeBatch: batches[0],
    recentBatches: [],
    games,
    counters: { running: 1, review: 2, incidents: 2, accepted: 0, todoRequired: todo.requiredTotal, todoCompleted: todo.requiredCompleted },
    todo,
  })
}

const resourceMap: Record<string, unknown> = {
  '/games': { items: games },
  '/batches': { items: batches },
  '/agent/work-items': { items: workItems },
  '/completion-reviews': { items: completionReviews, total: completionReviews.length },
  '/completion-adjudications': { items: completionAdjudications, total: completionAdjudications.length },
  '/incidents': { items: incidents },
  '/artifacts': { items: artifacts },
  '/weekly': { items: weekly },
  '/adapters': { items: adapters },
  '/logs': { items: logs },
  '/capabilities': { items: capabilities },
  '/repair-sessions': { items: repairSessions },
  '/diagnostic-bundles': { items: diagnosticBundles },
  '/notifications': { items: notifications, total: notifications.length },
  '/notification-policy': notificationPolicy,
  '/todo-definitions': { items: todoDefinitions, total: todoDefinitions.length },
  '/todo-instances': { items: todoInstances, total: todoInstances.length },
  '/automation-assessments': { items: automationAssessments, total: automationAssessments.length },
  '/meta': { name: 'YeYu Gamer Manager', version: '0.1.0-dev', apiVersion: 'v1', mode: 'mock' },
  '/health': { status: 'healthy', storage: 'healthy', eventStream: 'healthy', checkedAt: now() },
}

const config: ConfigDocument = {
  version: 'cfg-12',
  stateVersion: 42,
  config: {
    scheduleEnabled: false,
    gameDayResetHour: 4,
    defaultRetryBudget: 2,
    enabledGames: games.filter((game) => game.enabled).map((game) => game.gameId),
    notificationPolicy: 'milestones_only',
    todo_reset_policy: {
      timezone: 'Asia/Shanghai', time: '04:00', week_start_day: 'Monday', per_game: {}, per_definition: {},
    },
  },
  updatedAt: now(),
}

const policy: PolicyDocument = {
  version: 'policy-7',
  policy: {
    requireFreshEvidence: true,
    requireSameRunMarker: true,
    preserveVisibleClients: true,
    stopOnHumanGate: true,
  },
  forbiddenClasses: ['purchase', 'draw', 'account_settings', 'pvp', 'trade', 'irreversible_choice'],
  updatedAt: now(),
}

export async function createMockResponse<T>(method: string, rawPath: string, body?: unknown): Promise<T> {
  await new Promise((resolve) => setTimeout(resolve, 90))
  const path = rawPath.split('?')[0]
  if (method === 'GET') {
    if (path === '/snapshot') return mockSnapshot() as T
    if (path === '/config') return clone(config) as T
    if (path === '/policy') return clone(policy) as T
    if (path === '/notification-policy') return clone(notificationPolicy) as T
    if (path === '/completion-reviews') {
      const runId = new URL(rawPath, 'http://127.0.0.1').searchParams.get('runId')
      const items = completionReviews.filter((item) => !runId || item.runId === runId)
      return clone({ items, total: items.length }) as T
    }
    if (path === '/completion-adjudications') {
      const params = new URL(rawPath, 'http://127.0.0.1').searchParams
      const runId = params.get('runId')
      const batchId = params.get('batchId')
      const items = completionAdjudications.filter((item) =>
        (!runId || item.runId === runId) && (!batchId || item.batchId === batchId))
      return clone({ items, total: items.length }) as T
    }
    if (path === '/todo-reset-preview') {
      const url = new URL(rawPath, 'http://127.0.0.1')
      const cadence = url.searchParams.get('cadence') === 'weekly' ? 'weekly' : 'daily'
      const selectedGame = url.searchParams.get('gameId')
      const scoped = todoInstances.filter((item) => item.cadence === cadence && (!selectedGame || item.gameId === selectedGame))
      return clone({
        stateVersion: 42, generatedAt: now(), action: url.searchParams.get('action') === 'reset' ? 'reset' : 'reconcile',
        gameIds: selectedGame ? [selectedGame] : [...new Set(scoped.map((item) => item.gameId))], cadence,
        definitionCount: scoped.length, existingCount: scoped.length, wouldCreateCount: 0,
        items: scoped.map((item) => ({
          todoDefinitionId: item.todoDefinitionId, todoInstanceId: item.todoInstanceId, gameId: item.gameId, cadence: item.cadence,
          periodKey: item.periodKey, periodStartsAt: item.periodStartsAt, periodEndsAt: item.periodEndsAt, exists: true, currentStatus: item.status,
          effectiveResetRule: item.resetRule, nextPeriodResetRule: item.resetRule, policyChangeDeferred: false,
        })),
      }) as T
    }
    if (path.startsWith('/batches/')) {
      const batchId = decodeURIComponent(path.slice('/batches/'.length))
      return clone(batches.find((batch) => batch.batchId === batchId) ?? {}) as T
    }
    if (path.startsWith('/games/')) {
      const gameId = decodeURIComponent(path.slice('/games/'.length))
      const game = games.find((item) => item.gameId === gameId)
      return clone(game ? {
        ...game,
        todoDefinitions: todoDefinitions.filter((item) => item.gameId === gameId),
        todoInstances: todoInstances.filter((item) => item.gameId === gameId),
        todoSummary: { daily: mockTodoSummary(gameId), weekly: mockTodoSummary('__none__') },
      } : {}) as T
    }
    if (path.startsWith('/artifacts/') && !path.endsWith('/content')) {
      const artifactId = decodeURIComponent(path.slice('/artifacts/'.length))
      return clone(artifacts.find((artifact) => artifact.artifactId === artifactId) ?? {}) as T
    }
    const notificationMatch = path.match(/^\/notifications\/([^/]+)(?:\/(attempts|preview))?$/)
    if (notificationMatch) {
      const notificationId = decodeURIComponent(notificationMatch[1])
      if (notificationMatch[2] === 'attempts') {
        return clone({ items: notificationAttempts[notificationId] ?? [] }) as T
      }
      if (notificationMatch[2] === 'preview') {
        return clone(notificationPreviews[notificationId] ?? {}) as T
      }
      return clone(notifications.find((item) => item.notificationId === notificationId) ?? {}) as T
    }
    return clone(resourceMap[path] ?? { items: [] }) as T
  }

  const notificationAction = path.match(/^\/notifications\/([^/]+)\/(send-requests|retry-requests)$/)
  let result: unknown = body
  if (method === 'POST' && path === '/claims/decisions') {
    const request = body && typeof body === 'object' ? body as Record<string, unknown> : {}
    if (request.todoDiagnoses !== undefined && request.todoDiagnosis !== undefined) {
      throw new Error('todoDiagnoses and legacy todoDiagnosis are mutually exclusive')
    }
    const diagnoses = request.todoDiagnoses
    if (diagnoses !== undefined && !Array.isArray(diagnoses)) {
      throw new Error('todoDiagnoses must be an array')
    }
    if (Array.isArray(diagnoses)) {
      const ids = diagnoses.map((item) => item && typeof item === 'object'
        ? (item as Record<string, unknown>).todoInstanceId
        : undefined)
      if (ids.some((id) => typeof id !== 'string') || new Set(ids).size !== ids.length) {
        throw new Error('todoDiagnoses must contain unique typed Todo diagnoses')
      }
    }
    result = {
      todoDiagnoses: clone(Array.isArray(diagnoses) ? diagnoses : []),
      legacyTodoDiagnosisUsed: request.todoDiagnoses === undefined && request.todoDiagnosis !== undefined,
    }
  }
  if (method === 'POST' && notificationAction) {
    const notificationId = decodeURIComponent(notificationAction[1])
    const delivery = notifications.find((item) => item.notificationId === notificationId)
    const request = body && typeof body === 'object' ? body as Record<string, unknown> : {}
    const ambiguous = delivery && ['transport_ambiguous', 'worker_internal', 'lease_expired_ambiguous']
      .includes(delivery.lastErrorClass)
    if (notificationAction[2] === 'retry-requests' && ambiguous && request.confirmAmbiguous !== true) {
      throw new Error('ambiguous delivery requires explicit confirmation')
    }
    result = delivery ? {
      notification: {
        ...delivery,
        dispatchGate: notificationPolicy.secretState === 'configured' ? 'automatic' : 'secret_missing',
        nextAttemptAt: now(),
        lastErrorClass: '',
        updatedAt: now(),
      },
    } : {}
  }

  const receipt: CommandReceipt = {
    commandId: `mock_cmd_${Date.now()}`,
    idempotencyKey: `mock_idem_${Date.now()}`,
    statusUrl: `/commands/mock_cmd_${Date.now()}`,
    acceptedStateVersion: 43,
    state: 'accepted',
    message: `Mock 已受理 ${method} ${path}；未宣称业务完成。`,
    result,
    submittedAt: now(),
  }
  return receipt as T
}

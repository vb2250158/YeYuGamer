import type {
  AgentTodoDifficulty,
  TodoDiagnosisSubmission,
} from '../api/contracts'
import type { TodoDiagnosticItem } from './todos'

export interface TodoDiagnosisDraft {
  included: boolean
  todoInstanceId: string
  difficulty: AgentTodoDifficulty
  automatable: boolean | null
  confidence: number
  basisText: string
  failureStage: string
  issue: string
  recommendation: string
  evidenceIds: string[]
}

export interface TodoDiagnosisBuildResult {
  value: TodoDiagnosisSubmission[]
  errors: string[]
}

export function createTodoDiagnosisDraft(
  item: TodoDiagnosticItem,
  evidenceIds: string[] = item.evidenceRefs ?? [],
): TodoDiagnosisDraft {
  return {
    included: true,
    todoInstanceId: item.todoInstanceId,
    difficulty: 'unknown',
    automatable: null,
    confidence: 0.5,
    basisText: '',
    failureStage: '',
    issue: '',
    recommendation: '',
    evidenceIds: [...new Set(evidenceIds)],
  }
}

export function buildTodoDiagnoses(drafts: TodoDiagnosisDraft[]): TodoDiagnosisBuildResult {
  const errors: string[] = []
  const value: TodoDiagnosisSubmission[] = []
  const seen = new Set<string>()
  const included = drafts.filter((item) => item.included)
  if (included.length > 100) errors.push('单次 todoDiagnoses 不能超过 100 项')

  for (const draft of included) {
    if (seen.has(draft.todoInstanceId)) {
      errors.push(`${draft.todoInstanceId} 重复出现`)
      continue
    }
    seen.add(draft.todoInstanceId)
    const basis = draft.basisText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
    if (draft.automatable === null) errors.push(`${draft.todoInstanceId} 尚未判断是否可自动化`)
    if (!basis.length) errors.push(`${draft.todoInstanceId} 缺少判断依据`)
    if (basis.length > 20 || basis.some((item) => item.length > 1000)) {
      errors.push(`${draft.todoInstanceId} 的判断依据必须为 1 到 20 条且每条不超过 1000 字符`)
    }
    if (draft.failureStage.length > 160) errors.push(`${draft.todoInstanceId} 的失败阶段超过 160 字符`)
    if (draft.issue.length > 1000) errors.push(`${draft.todoInstanceId} 的具体问题超过 1000 字符`)
    if (draft.recommendation.length > 1000) errors.push(`${draft.todoInstanceId} 的建议超过 1000 字符`)
    if (draft.evidenceIds.length > 50) errors.push(`${draft.todoInstanceId} 的证据不能超过 50 项`)
    if (draft.evidenceIds.length !== new Set(draft.evidenceIds).size) errors.push(`${draft.todoInstanceId} 的证据包含重复 ID`)
    if (!Number.isFinite(draft.confidence) || draft.confidence < 0 || draft.confidence > 1) {
      errors.push(`${draft.todoInstanceId} 的置信度必须在 0 到 1 之间`)
    }
    if (draft.automatable === null || !basis.length) continue
    value.push({
      todoInstanceId: draft.todoInstanceId,
      difficulty: draft.difficulty,
      automatable: draft.automatable,
      confidence: draft.confidence,
      basis,
      failureStage: draft.failureStage.trim(),
      issue: draft.issue.trim(),
      recommendation: draft.recommendation.trim(),
      evidenceIds: [...draft.evidenceIds],
    })
  }

  return { value, errors }
}

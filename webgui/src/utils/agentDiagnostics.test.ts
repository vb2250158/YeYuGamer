import { describe, expect, it } from 'vitest'
import { buildTodoDiagnoses, createTodoDiagnosisDraft } from './agentDiagnostics'

describe('Agent Todo diagnosis presentation', () => {
  it('serializes several explicitly edited Todo diagnoses without deriving Manager state', () => {
    const first = createTodoDiagnosisDraft({ todoInstanceId: 'todo-1' })
    const second = createTodoDiagnosisDraft({ todoInstanceId: 'todo-2' })
    Object.assign(first, {
      difficulty: 'moderate', automatable: true, confidence: 0.8,
      basisText: 'Manager dispatch=eligible\nlatest attempt blocked',
      issue: 'navigation drift', recommendation: 'capture a new frame',
      evidenceIds: ['artifact-1'],
    })
    Object.assign(second, {
      difficulty: 'unsupported', automatable: false, confidence: 1,
      basisText: 'active blocker=human_required',
      issue: 'login gate', recommendation: 'wait for explicit release',
    })

    expect(buildTodoDiagnoses([first, second])).toEqual({
      errors: [],
      value: [
        expect.objectContaining({ todoInstanceId: 'todo-1', automatable: true, issue: 'navigation drift', evidenceIds: ['artifact-1'] }),
        expect.objectContaining({ todoInstanceId: 'todo-2', automatable: false, issue: 'login gate' }),
      ],
    })
  })

  it('fails closed when an included Todo lacks an explicit automatable judgment or basis', () => {
    const draft = createTodoDiagnosisDraft({ todoInstanceId: 'todo-1' })
    const result = buildTodoDiagnoses([draft])
    expect(result.value).toEqual([])
    expect(result.errors).toEqual([
      'todo-1 尚未判断是否可自动化',
      'todo-1 缺少判断依据',
    ])
  })
})

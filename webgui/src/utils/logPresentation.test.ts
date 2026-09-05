import { describe, expect, it } from 'vitest'
import { presentLogEntry } from './logPresentation'

describe('log presentation', () => {
  it('keeps source, observation, decision and reason as separate facts', () => {
    const result = presentLogEntry({
      timestamp: '2026-09-04T12:00:00Z', level: 'warning', source: 'adapter:endfield', message: 'launcher gate',
      phase: 'launch', observedState: 'continue_download_button_visible', decision: 'human_required', reasonCode: 'launcher_download_required', reason: '启动器需要继续下载',
    })
    expect(result).toMatchObject({
      source: 'adapter:endfield', phase: 'launch', observedState: 'continue_download_button_visible',
      decision: 'human_required', reason: 'launcher_download_required · 启动器需要继续下载', structured: true,
    })
  })

  it('does not treat a generic legacy crash message as observed state', () => {
    const result = presentLogEntry({
      timestamp: '2026-09-04T12:00:00Z', level: 'error', source: 'game-run', message: 'blank or stuck; crashed',
    })
    expect(result.observedState).toBe('未提供观察结果')
    expect(result.decision).toBe('未提供判定')
    expect(result.rawMessage).toBe('blank or stuck; crashed')
    expect(result.structured).toBe(false)
  })
})

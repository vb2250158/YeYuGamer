import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const pagesRoot = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(pagesRoot, 'TodayPage.vue'), 'utf8')

// These remaining checks guard page-only mutation and takeover wiring; the
// runtime/scope/evidence projections have behavioral tests in utils/.
describe('Today page mutation safety wiring', () => {
  it('routes a Manager-owned human gate to takeover details before review or new execution', () => {
    expect(source).toContain("actions?.humanTakeover === true ? actions.humanTakeoverTargets ?? [] : []")
    expect(source).toContain('v-if="humanTakeoverTargets.length"')
    expect(source.indexOf('<template v-if="humanTakeoverTargets.length">')).toBeLessThan(source.indexOf('<v-btn v-else-if="canReviewEvidence"'))
    expect(source).toContain('router.push(`/games/${encodeURIComponent(target.gameId)}`)')
    expect(source).toContain('人工接管详情')
    expect(source).toContain("if (mode === 'execute' && executionInProgress.value) return")
    expect(source).toContain("if (['human_required', 'human_takeover'].includes(gameRuntimeState(game))) return false")
  })

  it('resolves the selected batch inside the click handler', () => {
    expect(source).toContain('async function cancelBatch(): Promise<void>')
    expect(source).toContain('const batch = displayedExecutionBatch.value')
    expect(source).not.toContain('cancelBatch(batch = displayedExecutionBatch.value)')
  })

  it('refreshes the Manager snapshot before clearing saved checkbox drafts', () => {
    expect(source).toContain(
      'await Promise.all([manager.refresh({ quiet: true }), loadConfiguration()])',
    )
  })

  it('flushes every unsaved config mutation before creating a batch', () => {
    const flushIndex = source.indexOf('if (!await flushDailyConfiguration()) return')
    const batchIndex = source.indexOf("await manager.submitCommand(mode === 'plan' ? '规划今日队列' : '执行今日队列', 'POST', '/batches'")
    expect(flushIndex).toBeGreaterThan(0)
    expect(batchIndex).toBeGreaterThan(flushIndex)
    expect(source).toContain('保存结果尚未确认，未允许开始执行。')
    expect(source).toContain('没有创建执行批次')
    expect(source).toContain('const expectedConfigurationRevision = configurationMutationRevision')
    expect(source).toContain('configurationMutationRevision !== expectedConfigurationRevision')
    expect(source).toContain('|| hasUnsavedConfiguration.value')
    expect(source).toContain("configurationFlushError.value = '保存期间配置发生变化；已停止，没有创建执行批次。'")
  })

  it('freezes every configuration mutation while save-and-start is in flight', () => {
    expect(source).toContain('const configurationLocked = computed(() => busy.value || executionInProgress.value)')
    expect(source.match(/:disabled="configurationLocked/g)?.length ?? 0).toBeGreaterThanOrEqual(20)
    expect(source.match(/if \(configurationLocked\.value\) return/g)?.length ?? 0).toBeGreaterThanOrEqual(9)
  })

  it('resumes each terminal game run through the typed run endpoint', () => {
    expect(source).toContain('actions?.runResume !== true')
    expect(source).toContain('v-for="target in runResumeTargets"')
    expect(source).toContain('await manager.refresh({ quiet: true })')
    expect(source).toContain('`/game-runs/${encodeURIComponent(target.runId)}/resume-requests`')
    expect(source).toContain("'explicit_operator_resume_from_today'")
    expect(source).toContain('expectedRunRevision')
    expect(source).not.toContain('generic run.resume')
    expect(source).not.toContain('explicit_operator_resume_reconciliation_from_today')
    expect(source).toContain("=== 'cancel_old_batch_then_start_fresh'")
    expect(source).toContain('结束旧运行后重新开始')
  })
})

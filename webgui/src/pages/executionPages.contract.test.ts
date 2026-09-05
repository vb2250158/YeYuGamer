import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const pagesRoot = dirname(fileURLToPath(import.meta.url))
const todaySource = readFileSync(resolve(pagesRoot, 'TodayPage.vue'), 'utf8').replace(/\r\n/g, '\n')
const queueSource = readFileSync(resolve(pagesRoot, 'QueuePage.vue'), 'utf8').replace(/\r\n/g, '\n')

// The page must send the complete selection, including blocked games. Helper
// readiness tests alone cannot detect a UI that silently narrows the request.
describe('execution page runtimeBinding readiness contract', () => {
  it('guards Today execution with Manager per-game readiness without narrowing the submitted scope', () => {
    expect(todaySource).toContain("const runtimeBindings = useResource<AdapterInfo>('/adapters')")
    expect(todaySource).toContain('queuedRuntimeBindingReadiness.value.anyReady')
    expect(todaySource).toContain(':disabled="!queuedDailyGames.length || !canExecuteQueuedGames')
    expect(todaySource).toContain('const gameIds = queuedDailyGames.value.map((game) => game.gameId)')
    expect(todaySource).toMatch(/\bgameIds\s*,\s*mode\s*,/)
    expect(todaySource).not.toContain('gameIds: queuedRuntimeBindingReadiness.value.readyGameIds')
  })

  it('guards Queue execution while sending every selected game for final Manager adjudication', () => {
    expect(queueSource).toContain("const runtimeBindings = useResource<AdapterInfo>('/adapters')")
    expect(queueSource).toContain('selectedRuntimeBindingReadiness.value.anyReady')
    expect(queueSource).toContain('|| !canExecuteSelection ||')
    expect(queueSource).toContain('gameIds: selected.value,')
    expect(queueSource).not.toContain('gameIds: selectedRuntimeBindingReadiness.value.readyGameIds')
    expect(queueSource).toContain('执行请求仍会包含全部所选游戏')
  })
})

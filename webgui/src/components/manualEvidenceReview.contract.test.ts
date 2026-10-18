import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const component = readFileSync(new URL('./ManualEvidenceReview.vue', import.meta.url), 'utf8')
const router = readFileSync(new URL('../router.ts', import.meta.url), 'utf8')

describe('manual screenshot review surface', () => {
  it('submits bounded uploads through Manager without completing a Todo', () => {
    expect(component).toContain('8 * 1024 * 1024')
    expect(component).toContain('manager.submitCommand')
    expect(component).toContain('/evidence-uploads')
    expect(component).toContain("artifact.source === 'operator-upload'")
    expect(component).not.toContain("status: 'completed'")
  })

  it('opens a real review page rather than silently redirecting', () => {
    expect(router).toContain("{ path: '/evidence', name: 'evidence', component: () => import('./pages/EvidencePage.vue') }")
    expect(component).toContain('/evidence?artifactId=')
  })
})

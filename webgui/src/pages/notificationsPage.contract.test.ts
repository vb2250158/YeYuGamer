import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const pagesRoot = dirname(fileURLToPath(import.meta.url))
const notificationSource = readFileSync(resolve(pagesRoot, 'NotificationsPage.vue'), 'utf8')

// Keep UI-only escaping, secret disclosure and duplicate-send confirmation
// boundaries until equivalent rendered interaction tests replace them.
describe('notification page contract boundary', () => {
  it('renders text and HTML previews only through escaped Vue interpolation', () => {
    expect(notificationSource).toContain('{{ preview.textBody }}')
    expect(notificationSource).toContain('{{ preview.htmlBody }}')
    expect(notificationSource).not.toMatch(/\bv-html\s*=/)
    expect(notificationSource).not.toMatch(/\.innerHTML\b/)
  })

  it('does not expose raw transport configuration or artifact paths', () => {
    for (const forbidden of [
      'smtpHost',
      'smtpPassword',
      'recipientAddress',
      'attachmentRefs',
      'filePath',
      'localPath',
    ]) {
      expect(notificationSource).not.toContain(forbidden)
    }
    expect(notificationSource).toContain('recipientBindingId')
    expect(notificationSource).toContain('attachmentDecisions')
  })

  it('keeps ambiguous retry behind a second confirmation', () => {
    expect(notificationSource).toContain('ambiguousDialog.value = true')
    expect(notificationSource).toContain('confirmAmbiguousRetry')
    expect(notificationSource).toContain('submitRetry(notificationId, true)')
  })
})

import { describe, expect, it, vi } from 'vitest'
import { bootstrapWebGuiSession, consumeBootstrapNonce } from './bootstrap'

describe('WebGUI bootstrap fragment', () => {
  it('consumes the nonce and clears the fragment immediately', () => {
    const nonce = 'n'.repeat(43)
    const replaceState = vi.fn()
    const received = consumeBootstrapNonce({
      location: {
        hash: `#bootstrap=${nonce}`,
        pathname: '/today',
        search: '?view=compact',
      },
      history: { state: { route: 'today' }, replaceState },
    })
    expect(received).toBe(nonce)
    expect(replaceState).toHaveBeenCalledOnce()
    expect(replaceState).toHaveBeenCalledWith(
      { route: 'today' },
      '',
      '/today?view=compact',
    )
  })

  it('clears a malformed fragment before rejecting it', () => {
    const replaceState = vi.fn()
    expect(() => consumeBootstrapNonce({
      location: { hash: '#bootstrap=short', pathname: '/', search: '' },
      history: { state: null, replaceState },
    })).toThrow('格式无效')
    expect(replaceState).toHaveBeenCalledWith(null, '', '/')
  })

  it('leaves a fragment-free refresh untouched', () => {
    const replaceState = vi.fn()
    expect(consumeBootstrapNonce({
      location: { hash: '', pathname: '/', search: '' },
      history: { state: null, replaceState },
    })).toBeUndefined()
    expect(replaceState).not.toHaveBeenCalled()
  })

  it('establishes a same-origin local session after a Manager restart', async () => {
    const establishLocalWebGuiSession = vi.fn().mockResolvedValue(undefined)
    const exchangeWebGuiSession = vi.fn()
    const result = await bootstrapWebGuiSession(
      { establishLocalWebGuiSession, exchangeWebGuiSession } as never,
      {
        location: { hash: '', pathname: '/', search: '' },
        history: { state: null, replaceState: vi.fn() },
      },
    )
    expect(result).toBe(true)
    expect(establishLocalWebGuiSession).toHaveBeenCalledOnce()
    expect(exchangeWebGuiSession).not.toHaveBeenCalled()
  })
})

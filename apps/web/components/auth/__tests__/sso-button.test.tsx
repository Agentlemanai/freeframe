import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const get = vi.fn()
vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args) } }))

import { SsoButton, ssoErrorMessage } from '../sso-button'

function setSearch(search: string) {
  window.history.replaceState(null, '', `/login${search}`)
}

describe('SsoButton', () => {
  afterEach(() => {
    get.mockReset()
    setSearch('')
  })

  it('renders nothing when SSO is not configured', async () => {
    get.mockResolvedValue({ enabled: false, button_label: null })
    const { container } = render(<SsoButton />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('/auth/oidc/config'))
    expect(container).toBeEmptyDOMElement()
  })

  it('links to the API login with the original destination', async () => {
    setSearch('?from=%2Fprojects%2Fabc')
    get.mockResolvedValue({ enabled: true, button_label: 'Log in with Garlomedia account' })
    render(<SsoButton />)
    const link = await screen.findByRole('link', { name: /Garlomedia account/ })
    const url = new URL(link.getAttribute('href')!, 'http://x')
    expect(url.pathname).toMatch(/\/auth\/oidc\/login$/)
    expect(url.searchParams.get('next')).toBe('/projects/abc')
  })

  it('never forwards an off-site destination', async () => {
    setSearch('?from=%2F%2Fevil.example.com')
    get.mockResolvedValue({ enabled: true, button_label: 'SSO' })
    render(<SsoButton />)
    const link = await screen.findByRole('link', { name: /SSO/ })
    expect(new URL(link.getAttribute('href')!, 'http://x').searchParams.get('next')).toBe('/projects')
  })

  it('shows the error the callback sent back', async () => {
    setSearch('?sso_error=no_account')
    get.mockResolvedValue({ enabled: true, button_label: 'SSO' })
    render(<SsoButton />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/no account/i)
  })
})

describe('ssoErrorMessage', () => {
  it('falls back to a generic message for unknown codes', () => {
    expect(ssoErrorMessage('something_new')).toMatch(/failed/)
    expect(ssoErrorMessage(null)).toBeNull()
  })
})

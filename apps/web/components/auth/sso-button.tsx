'use client'

import { useEffect, useState } from 'react'
import { KeyRound } from 'lucide-react'
import { api } from '@/lib/api'
import { safeInternalPath } from '@/lib/safe-redirect'
import { Button } from '@/components/ui/button'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface OidcConfig {
  enabled: boolean
  button_label: string | null
}

/** Messages for the `?sso_error=` codes the API's /auth/oidc/callback sends back. */
export const SSO_ERROR_MESSAGES: Record<string, string> = {
  no_account: 'There is no account for your email yet. Ask an admin to invite you.',
  account_deactivated: 'Your account has been deactivated.',
  account_unavailable: 'This email cannot be used to sign in. Ask an admin for help.',
  not_allowed: "Your account isn't allowed to use this app.",
  access_denied: 'Single sign-on was cancelled or not allowed.',
  email_not_verified: 'Your email address is not verified at the sign-in provider.',
  no_email: 'The sign-in provider did not share your email address.',
  invalid_state: 'The sign-in link expired. Please try again.',
}
const SSO_ERROR_FALLBACK = 'Single sign-on failed. Please try again.'

export function ssoErrorMessage(code: string | null): string | null {
  if (!code) return null
  return SSO_ERROR_MESSAGES[code] ?? SSO_ERROR_FALLBACK
}

/**
 * "Sign in with SSO" on the login screen. Renders nothing unless the server
 * has OIDC configured, so instances without SSO look exactly as before.
 */
export function SsoButton() {
  const [config, setConfig] = useState<OidcConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [next, setNext] = useState('/projects')

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    setError(ssoErrorMessage(params.get('sso_error')))
    setNext(safeInternalPath(params.get('from')))
    api
      .get<OidcConfig>('/auth/oidc/config')
      .then(setConfig)
      .catch(() => setConfig(null))
  }, [])

  if (!config?.enabled) return null

  const href = `${API_URL}/auth/oidc/login?${new URLSearchParams({ next }).toString()}`

  return (
    <div className="mb-6">
      {error && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-status-error/30 bg-status-error/10 px-3 py-2.5 text-sm text-status-error"
        >
          {error}
        </div>
      )}
      <Button asChild size="lg" className="w-full">
        <a href={href}>
          <KeyRound className="h-4 w-4" aria-hidden />
          {config.button_label || 'Sign in with SSO'}
        </a>
      </Button>
      <div className="mt-6 flex items-center gap-3 text-xs uppercase tracking-wide text-text-tertiary">
        <span className="h-px flex-1 bg-border" />
        or
        <span className="h-px flex-1 bg-border" />
      </div>
    </div>
  )
}

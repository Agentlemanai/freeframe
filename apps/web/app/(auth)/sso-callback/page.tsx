'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { setTokens } from '@/lib/auth'
import { safeInternalPath } from '@/lib/safe-redirect'
import { useAuthStore } from '@/stores/auth-store'

/**
 * Landing page after single sign-on. The API puts the session tokens in the
 * URL fragment (never sent to any server); they are stored, wiped from the
 * address bar and history, and the user continues to where they started.
 */
export default function SsoCallbackPage() {
  const router = useRouter()

  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    const access = params.get('access_token')
    const refresh = params.get('refresh_token')
    const next = safeInternalPath(params.get('next'))
    window.history.replaceState(null, '', window.location.pathname)

    if (!access || !refresh) {
      router.replace('/login?sso_error=invalid_request')
      return
    }
    setTokens(access, refresh)
    useAuthStore
      .getState()
      .fetchUser()
      .finally(() => router.replace(next))
  }, [router])

  return (
    <p className="text-center text-sm text-text-secondary" aria-live="polite">
      Signing you in…
    </p>
  )
}

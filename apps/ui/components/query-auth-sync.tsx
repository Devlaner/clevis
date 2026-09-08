"use client"

import { useRef } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useAuth } from "@/lib/auth-context"

/**
 * Drops the entire React Query cache whenever the signed-in user changes,
 * including sign-out (id -> null) and an account switch on the same tab.
 *
 * The QueryClient is created once and outlives a logout/login that happens
 * via client navigation, and several sensitive queries are keyed on `org`
 * alone (`["tokens.resolve", org]` resolves to a decrypted GitHub PAT,
 * `["analytics.my-view", org]`, `["my-orgs"]`, `["installations"]`, …). Without
 * this, user B landing on the app within `staleTime` of user A signing out
 * could be served A's cached PAT and personal data (CWE-200).
 *
 * The clear runs **during render**, not in an effect. This component is
 * rendered immediately before `<AuthGuard>` in `app/layout.tsx`, so a
 * synchronous clear here lands before any `useQuery` inside the authenticated
 * subtree reads the cache on the same commit. An effect-based clear would run
 * only after that first paint, leaving one frame in which a query keyed on
 * `org` alone serves the previous user's data. Adjusting external state during
 * render for a prop change is a supported React pattern; `queryClient.clear()`
 * is idempotent and the ref guard makes it fire once per identity change
 * (Strict Mode's double render included).
 *
 * Rendered inside AuthProvider (needs useAuth) and QueryProvider (needs
 * useQueryClient); it renders nothing.
 */
export function QueryAuthSync() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const lastUserId = useRef<number | null | undefined>(undefined)

  const currentUserId = user?.id ?? null
  if (lastUserId.current === undefined) {
    // First observation on mount — nothing cached under a prior identity yet.
    lastUserId.current = currentUserId
  } else if (lastUserId.current !== currentUserId) {
    lastUserId.current = currentUserId
    queryClient.clear()
  }

  return null
}

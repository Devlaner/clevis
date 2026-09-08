"use client"

import { useEffect, useRef } from "react"
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
 * could be served A's cached PAT and personal data (CWE-200). `app/page.tsx`
 * mitigated one query by adding `user?.id` to its key; this generalises it.
 *
 * Rendered inside AuthProvider (needs useAuth) and QueryProvider (needs
 * useQueryClient); it renders nothing.
 */
export function QueryAuthSync() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const lastUserId = useRef<number | null | undefined>(undefined)

  useEffect(() => {
    const current = user?.id ?? null
    if (lastUserId.current === undefined) {
      // First observation on mount — nothing cached under a prior identity yet.
      lastUserId.current = current
      return
    }
    if (lastUserId.current !== current) {
      lastUserId.current = current
      queryClient.clear()
    }
  }, [user?.id, queryClient])

  return null
}

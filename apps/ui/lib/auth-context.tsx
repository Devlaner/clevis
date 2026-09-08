"use client"

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import type { PendingInvitationSummary } from "@/lib/api/types"
import { clearActiveScope } from "@/lib/active-scope"

export interface AuthUser {
  id: number
  email: string
  name: string | null
  is_workspace_admin: boolean
}

interface AuthContextValue {
  user: AuthUser | null
  token: string | null
  isLoading: boolean
  logoutWarning: string | null
  /** True once the optimistic JWT-derived user could not be confirmed against
   *  the server (network error, timeout, or non-401 failure) after a retry —
   *  `user` may be stale (e.g. is_workspace_admin) until the next successful
   *  check, which happens on login/setSession or once the browser is back
   *  online. */
  authUnconfirmed: boolean
  pendingInvitations: PendingInvitationSummary[]
  login(email: string, password: string): Promise<void>
  logout(): void
  clearLogoutWarning(): void
  updateUser(u: Partial<AuthUser>): void
  setSession(jwtToken: string, authUser: AuthUser, pendingInvitations?: PendingInvitationSummary[]): void
  dismissPendingInvitations(): void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const _TOKEN_KEY = "clevis:token"
const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080"
const _LOGOUT_WARNING =
  "Logged out locally, but the server session may still be active. Avoid shared devices until you can retry."

function parseJwtPayload(token: string): AuthUser | null {
  try {
    const segment = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")
    const padded = segment.padEnd(Math.ceil(segment.length / 4) * 4, "=")
    const payload = JSON.parse(atob(padded))
    const id = Number(payload.sub)
    if (!id || !payload.email) return null
    if (payload.exp && payload.exp * 1000 < Date.now()) return null
    return {
      id,
      email: payload.email,
      name: payload.name ?? null,
      is_workspace_admin: Boolean(payload.is_workspace_admin),
    }
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [logoutWarning, setLogoutWarning] = useState<string | null>(null)
  const [authUnconfirmed, setAuthUnconfirmed] = useState(false)
  const [pendingInvitations, setPendingInvitations] = useState<PendingInvitationSummary[]>([])
  const sessionEpochRef = useRef(0)

  const dismissPendingInvitations = useCallback(() => {
    setPendingInvitations([])
  }, [])

  const bumpSessionEpoch = useCallback(() => {
    sessionEpochRef.current += 1
  }, [])

  const clearLogoutWarning = useCallback(() => {
    setLogoutWarning(null)
  }, [])

  const logout = useCallback(() => {
    bumpSessionEpoch()
    fetch(`${BASE}/auth/logout`, { method: "POST", credentials: "include" })
      .then((res) => {
        if (!res.ok) setLogoutWarning(_LOGOUT_WARNING)
      })
      .catch(() => setLogoutWarning(_LOGOUT_WARNING))
    localStorage.removeItem(_TOKEN_KEY)
    // Drop per-user browser state so a different user on this browser doesn't
    // inherit the previous user's org scope / unread-activity marker. The React
    // Query cache is cleared separately by QueryAuthSync on the user-id change.
    clearActiveScope()
    localStorage.removeItem("activity_last_seen_at")
    setToken(null)
    setUser(null)
    setAuthUnconfirmed(false)
    setPendingInvitations([])
  }, [bumpSessionEpoch])

  useEffect(() => {
    const epochAtStart = sessionEpochRef.current
    const stored = localStorage.getItem(_TOKEN_KEY)
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    // Guards against a fetch that was already in flight when this effect's
    // cleanup ran (e.g. unmount, or React StrictMode's mount/unmount/remount)
    // from scheduling a retry or touching state after the fact — clearing
    // retryTimer alone can't catch this, since it isn't assigned until the
    // in-flight fetch's catch handler runs, which may be after cleanup.
    let cancelled = false

    if (stored) {
      const optimistic = parseJwtPayload(stored)
      if (optimistic) {
        setToken(stored)
        setUser(optimistic)
        setIsLoading(false)
      }
    }

    function stale() {
      return cancelled || sessionEpochRef.current !== epochAtStart
    }

    function checkMe(attempt: number) {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 15000)
      let willRetry = false

      fetch(`${BASE}/auth/me`, {
        headers: stored ? { Authorization: `Bearer ${stored}` } : {},
        credentials: "include",
        signal: controller.signal,
      })
        .then(async (res) => {
          if (stale()) return
          if (res.status === 401) {
            if (stored) logout()
            return
          }
          if (!res.ok) throw new Error(`auth/me responded ${res.status}`)
          const data = (await res.json()) as AuthUser
          // Re-check after the await — a concurrent login()/logout() could have
          // bumped the epoch while the body was being parsed, in which case this
          // response is stale and must not clobber the newer session.
          if (stale()) return
          if (stored) setToken(stored)
          setUser(data)
          setAuthUnconfirmed(false)
        })
        .catch(() => {
          if (stale()) return
          // One retry for a transient network hiccup/timeout before treating the
          // optimistic (or absent) user as unconfirmed instead of trusting it forever.
          if (attempt === 0) {
            willRetry = true
            retryTimer = setTimeout(() => checkMe(1), 2000)
            return
          }
          if (stored) setAuthUnconfirmed(true)
        })
        .finally(() => {
          clearTimeout(timer)
          // Keep isLoading true while a retry is pending; otherwise this is the
          // terminal outcome for this mount's check (success, 401, exhausted
          // retries, or a stale epoch after a concurrent login/logout) and the
          // initial loading phase is over regardless of which branch ran.
          if (!willRetry) setIsLoading(false)
        })
    }

    checkMe(0)

    return () => {
      cancelled = true
      clearTimeout(retryTimer)
    }
  }, [logout])

  // Re-check once the browser regains connectivity, so a session left
  // "unconfirmed" by a network blip doesn't sit that way forever — the mount
  // effect above only runs once and has no periodic re-check of its own.
  useEffect(() => {
    if (!authUnconfirmed) return
    function handleOnline() {
      const epochAtStart = sessionEpochRef.current
      const stored = localStorage.getItem(_TOKEN_KEY)
      fetch(`${BASE}/auth/me`, {
        headers: stored ? { Authorization: `Bearer ${stored}` } : {},
        credentials: "include",
      })
        .then(async (res) => {
          if (sessionEpochRef.current !== epochAtStart) return
          if (res.status === 401) {
            if (stored) logout()
            return
          }
          if (!res.ok) return
          const data = (await res.json()) as AuthUser
          // Re-check after the await — a concurrent login()/logout() could have
          // bumped the epoch while the body was being parsed, in which case this
          // response is stale and must not clobber the newer session.
          if (sessionEpochRef.current !== epochAtStart) return
          if (stored) setToken(stored)
          setUser(data)
          setAuthUnconfirmed(false)
        })
        .catch(() => {})
    }
    window.addEventListener("online", handleOnline)
    return () => window.removeEventListener("online", handleOnline)
  }, [authUnconfirmed, logout])

  const login = useCallback(async (email: string, password: string) => {
    const res = await fetch(`${BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail ?? "Login failed")
    const { access_token, user: u, pending_invitations } = data as {
      access_token: string
      user: AuthUser
      pending_invitations?: PendingInvitationSummary[]
    }
    bumpSessionEpoch()
    clearLogoutWarning()
    localStorage.setItem(_TOKEN_KEY, access_token)
    setToken(access_token)
    setUser(u)
    setPendingInvitations(pending_invitations ?? [])
    setAuthUnconfirmed(false)
    setIsLoading(false)
  }, [bumpSessionEpoch, clearLogoutWarning])

  const updateUser = useCallback((patch: Partial<AuthUser>) => {
    setUser((prev) => (prev ? { ...prev, ...patch } : prev))
  }, [])

  const setSession = useCallback(
    (jwtToken: string, authUser: AuthUser, invitations: PendingInvitationSummary[] = []) => {
      bumpSessionEpoch()
      clearLogoutWarning()
      localStorage.setItem(_TOKEN_KEY, jwtToken)
      setToken(jwtToken)
      setUser(authUser)
      setPendingInvitations(invitations)
      setAuthUnconfirmed(false)
      setIsLoading(false)
    },
    [bumpSessionEpoch, clearLogoutWarning],
  )

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        isLoading,
        logoutWarning,
        authUnconfirmed,
        pendingInvitations,
        login,
        logout,
        clearLogoutWarning,
        updateUser,
        setSession,
        dismissPendingInvitations,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within AuthProvider")
  return ctx
}

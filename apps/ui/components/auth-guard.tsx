"use client"

import { Fragment, useEffect, useRef } from "react"
import { useRouter, usePathname } from "next/navigation"
import { X } from "@phosphor-icons/react"
import { useAuth } from "@/lib/auth-context"
import { api } from "@/lib/api/client"

// Routes that don't require authentication
const PUBLIC_ROUTES = ["/login", "/setup", "/register", "/verify-email"]
// Prefixes for routes that don't require authentication (dynamic segments)
const PUBLIC_ROUTE_PREFIXES = ["/invite/"]

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, isLoading, logout, authUnconfirmed, pendingInvitations, dismissPendingInvitations } = useAuth()
  const router = useRouter()
  const pathname = usePathname()

  const isPublic =
    PUBLIC_ROUTES.includes(pathname) || PUBLIC_ROUTE_PREFIXES.some((prefix) => pathname.startsWith(prefix))

  useEffect(() => {
    if (isLoading) return

    if (isPublic) {
      // Already on a public route — no redirect needed
      return
    }

    if (!user) {
      // Check if setup is needed before redirecting to /login or /setup
      api.auth.setupRequired()
        .then(({ setup_required }) => {
          router.replace(setup_required ? "/setup" : "/login")
        })
        .catch(() => {
          router.replace("/login")
        })
    }
  }, [isLoading, user, isPublic, router])

  // Listen for 401 events from the API client. A ref (not a useEffect dependency) tracks
  // the current isPublic so the listener reads it fresh on every event without needing to
  // re-subscribe on every pathname change.
  const isPublicRef = useRef(isPublic)
  useEffect(() => {
    isPublicRef.current = isPublic
  }, [isPublic])

  useEffect(() => {
    function handle401() {
      // A stale/garbage token from a previous session can still get attached to a
      // best-effort call on a public route (e.g. an invite preview) -- that must not
      // force-log-out or redirect someone who was never "logged in" on this page.
      if (isPublicRef.current) return
      logout()
      router.replace("/login")
    }
    window.addEventListener("clevis:unauthorized", handle401)
    return () => window.removeEventListener("clevis:unauthorized", handle401)
  }, [logout, router])

  // Show nothing while checking auth or redirecting
  if (isLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="w-4 h-4 border border-primary/40 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  // Public routes always render
  if (isPublic) return <>{children}</>

  // Protected routes: show spinner while redirect to /login or /setup is in flight
  if (!user) return (
    <div className="min-h-screen bg-background flex items-center justify-center">
      <div className="w-4 h-4 border border-primary/40 border-t-primary rounded-full animate-spin" />
    </div>
  )

  return (
    <>
      {authUnconfirmed && (
        <div className="bg-yellow-500/10 border-b border-yellow-500/30 px-4 py-1.5 text-center text-xs text-yellow-400">
          Couldn&rsquo;t confirm your session with the server — your account info may be out of date until it&rsquo;s reachable again.
        </div>
      )}
      {pendingInvitations.length > 0 && (
        <div className="bg-primary/10 border-b border-primary/30 px-4 py-2 flex items-center justify-center gap-3 text-xs text-primary flex-wrap">
          <span>
            {/* Informational only — deliberately not a link. The accept token isn't
                exposed here (see PendingInvitationSummary), so this can't double as a
                shortcut to accept; find the original invite link, or ask an admin to
                resend it. */}
            You have a pending invite to join{" "}
            {pendingInvitations.map((inv) => inv.org_login).join(", ")} — use your invite link, or ask an org
            admin to resend it.
          </span>
          <button
            onClick={dismissPendingInvitations}
            className="text-primary/60 hover:text-primary"
            aria-label="Dismiss"
          >
            <X className="size-3" />
          </button>
        </div>
      )}
      {/* Remount the whole authenticated page subtree when the signed-in user
          changes on the same tab. QueryAuthSync empties the shared QueryClient
          synchronously on that transition, but a live QueryObserver memoizes its
          last result, so a query keyed on `org` alone (e.g. ["tokens.resolve",
          org]) would still paint the previous user's data for one frame. A fresh
          key forces new observers that read the just-cleared cache instead. */}
      <Fragment key={user.id}>{children}</Fragment>
    </>
  )
}

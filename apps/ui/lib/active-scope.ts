"use client"

// Shared "which GitHub account am I looking at" state, replacing the old
// localStorage.getItem("default_org")-per-page pattern. A scope is either an org
// the user is a member of, or their own personal GitHub account (an installation
// with account_type "User" — see ProfileDropdown in components/app-sidebar.tsx).
// Every /me/* endpoint accepts either kind as `owner` (apps/api/src/services/token_resolution.py's
// resolve_owner_token resolves org-installation or personal-installation tokens the same way);
// only Health & Security is org-only by backend design.

import { useCallback, useSyncExternalStore } from "react"

export type ActiveScope = { kind: "org"; login: string } | { kind: "personal"; login: string }

const STORAGE_KEY = "active_scope"
// Legacy key written by the old Settings "Default organization" select — read once
// as a fallback so existing users don't lose their selection on upgrade.
const LEGACY_ORG_KEY = "default_org"
const CHANGE_EVENT = "clevis:active-scope-changed"

function isActiveScope(value: unknown): value is ActiveScope {
  if (!value || typeof value !== "object") return false
  const v = value as Record<string, unknown>
  return (v.kind === "org" || v.kind === "personal") && typeof v.login === "string"
}

function parse(raw: string | null): ActiveScope | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (isActiveScope(parsed)) return parsed
  } catch {
    // fall through to null
  }
  return null
}

// Memoized so useSyncExternalStore's getSnapshot returns a referentially stable
// value between calls when the underlying localStorage strings haven't changed —
// otherwise a fresh object every render would trigger React's "getSnapshot should
// be cached" infinite-loop warning. Both keys are tracked (not just STORAGE_KEY):
// the legacy key is only consulted while STORAGE_KEY is unset, but caching solely
// on STORAGE_KEY (which stays null the whole time in that case) would let a stale
// legacy-derived scope leak across renders whenever the legacy key changes underneath it.
let cachedActiveRaw: string | null = null
let cachedLegacyRaw: string | null = null
let cachedScope: ActiveScope | null = null
let cachedRead = false

function read(): ActiveScope | null {
  if (typeof window === "undefined") return null
  const activeRaw = localStorage.getItem(STORAGE_KEY)
  const legacyRaw = activeRaw === null ? localStorage.getItem(LEGACY_ORG_KEY) : null
  if (cachedRead && activeRaw === cachedActiveRaw && legacyRaw === cachedLegacyRaw) return cachedScope
  cachedRead = true
  cachedActiveRaw = activeRaw
  cachedLegacyRaw = legacyRaw
  cachedScope = activeRaw !== null ? parse(activeRaw) : legacyRaw ? { kind: "org", login: legacyRaw } : null
  return cachedScope
}

function subscribe(callback: () => void): () => void {
  window.addEventListener("storage", callback)
  window.addEventListener(CHANGE_EVENT, callback)
  return () => {
    window.removeEventListener("storage", callback)
    window.removeEventListener(CHANGE_EVENT, callback)
  }
}

function getServerSnapshot(): ActiveScope | null {
  return null
}

export function setActiveScope(scope: ActiveScope): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(scope))
  window.dispatchEvent(new Event(CHANGE_EVENT))
}

// Called on logout so a new user on the same browser doesn't start already
// scoped into the previous user's org (which would also fire scoped requests
// for an org they may have no relationship with). Clears the legacy key too.
export function clearActiveScope(): void {
  if (typeof window === "undefined") return
  localStorage.removeItem(STORAGE_KEY)
  localStorage.removeItem(LEGACY_ORG_KEY)
  window.dispatchEvent(new Event(CHANGE_EVENT))
}

export function useActiveScope(): { scope: ActiveScope | null; setScope: (scope: ActiveScope) => void } {
  const scope = useSyncExternalStore(subscribe, read, getServerSnapshot)
  const setScope = useCallback((next: ActiveScope) => setActiveScope(next), [])
  return { scope, setScope }
}

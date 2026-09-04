"use client"

import { usePathname, useRouter } from "next/navigation"
import { useEffect, useMemo, useRef, useState } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { GearSix, Check, SignOut, UserPlus, ArrowSquareOut, User } from "@phosphor-icons/react"
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
} from "@/components/ui/sidebar"
import { useAuth } from "@/lib/auth-context"
import { api } from "@/lib/api/client"
import { useActiveScope, type ActiveScope } from "@/lib/active-scope"
import type { InstallationMeta, MyOrgMembership } from "@/lib/api/types"

const ACTIVITY_LAST_SEEN_KEY = "activity_last_seen_at"

function healthDotColor(score: number | null | undefined): string | null {
  if (score == null) return null
  if (score >= 80) return "bg-green-400"
  if (score >= 50) return "bg-yellow-400"
  return "bg-red-400"
}

// Settings is no longer in the sidebar nav — it lives inside the profile dropdown.
const groups = [
  [
    { title: "Overview",         href: "/" },
    { title: "Activity",         href: "/activity", showUnreadBadge: true },
    { title: "Pull Requests",    href: "/pulls" },
    { title: "Releases",         href: "/releases" },
  ],
  [
    { title: "Repositories",     href: "/repos" },
    { title: "Health & Security",href: "/security", showHealthDot: true },
  ],
  [
    { title: "Collaborators",    href: "/collaborators" },
    { title: "Automation",       href: "/automation" },
    { title: "Audit Log",        href: "/audit" },
  ],
  [
    { title: "My Work",    href: "/my" },
  ],
]

interface Profile {
  name: string
  org: string
  email: string
}

interface ScopeOption {
  scope: ActiveScope
  label: string
  sublabel: string
}

function ProfileDropdown({
  profile,
  scopeOptions,
  activeScope,
  onSelectScope,
  addInstallUrl,
  inviteHref,
  onClose,
  onSignOut,
}: {
  profile: Profile
  scopeOptions: ScopeOption[]
  activeScope: ActiveScope | null
  onSelectScope: (scope: ActiveScope) => void
  addInstallUrl: string | null
  inviteHref: string
  onClose: () => void
  onSignOut: () => void
}) {
  const initials = profile.name.charAt(0).toUpperCase()

  return (
    <div
      className="absolute top-full left-0 right-0 z-50 border-b border-sidebar-border bg-sidebar shadow-2xl"
      // prevent clicks inside from bubbling to the click-away handler
      onClick={(e) => e.stopPropagation()}
    >
      {/* Email */}
      <div className="px-3.5 py-2.5 border-b border-sidebar-border/60">
        <p className="text-[0.75rem] text-sidebar-foreground/50 truncate">
          {profile.email || "no email set"}
        </p>
      </div>

      {/* Current identity */}
      <div className="p-1.5">
        <div className="flex items-center gap-2.5 px-2 py-2">
          <div className="size-7 rounded-md bg-primary/15 border border-primary/20 flex items-center justify-center shrink-0">
            <span className="text-[0.6875rem] font-semibold text-primary leading-none">
              {initials}
            </span>
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[0.8125rem] font-medium text-sidebar-foreground leading-none truncate">
              {profile.name}
            </p>
            <p className="text-[0.6875rem] text-sidebar-foreground/40 mt-0.5 leading-none">
              {profile.org || "Members"}
            </p>
          </div>
        </div>
      </div>

      {/* Scope switcher — personal account + orgs you belong to */}
      {(scopeOptions.length > 0 || addInstallUrl) && (
        <div className="px-1.5 pb-1.5 border-b border-sidebar-border/60">
          <p className="px-2 pt-1 pb-1.5 text-[0.6875rem] font-medium uppercase tracking-wide text-sidebar-foreground/40">
            Switch account
          </p>
          {scopeOptions.map((opt) => {
            const isActive =
              activeScope?.kind === opt.scope.kind && activeScope.login === opt.scope.login
            return (
              <button
                key={`${opt.scope.kind}:${opt.scope.login}`}
                onClick={() => { onSelectScope(opt.scope); onClose() }}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left rounded-md hover:bg-sidebar-accent/60 transition-colors"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-[0.8125rem] text-sidebar-foreground leading-none truncate">{opt.label}</p>
                  <p className="text-[0.6875rem] text-sidebar-foreground/40 mt-0.5 leading-none">{opt.sublabel}</p>
                </div>
                {isActive && <Check className="size-3.5 text-primary shrink-0" />}
              </button>
            )
          })}
          {addInstallUrl && (
            <a
              href={addInstallUrl}
              className="flex items-center gap-2 px-2 py-1.5 text-left rounded-md hover:bg-sidebar-accent/60 transition-colors text-sidebar-foreground/70 hover:text-sidebar-foreground"
            >
              <User className="size-3.5 shrink-0" />
              <span className="text-[0.8125rem] flex-1">
                {scopeOptions.some((o) => o.scope.kind === "personal")
                  ? "Add another account or org"
                  : "Connect your personal GitHub account"}
              </span>
              <ArrowSquareOut className="size-3 shrink-0" />
            </a>
          )}
        </div>
      )}

      {/* Settings + Invite members buttons */}
      <div className="px-1.5 pb-1.5 flex gap-1.5">
        <Link
          href="/settings"
          onClick={onClose}
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-[0.75rem] font-medium text-sidebar-foreground/70 hover:text-sidebar-foreground bg-sidebar-accent/60 hover:bg-sidebar-accent border border-sidebar-border/60 transition-colors flex-1 justify-center"
        >
          <GearSix className="size-3" />
          Settings
        </Link>
        <Link
          href={inviteHref}
          onClick={onClose}
          title={inviteHref === "/settings" ? "Switch to an organization you admin from the profile menu first" : undefined}
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-[0.75rem] font-medium text-sidebar-foreground/70 hover:text-sidebar-foreground bg-sidebar-accent/60 hover:bg-sidebar-accent border border-sidebar-border/60 transition-colors flex-1 justify-center"
        >
          <UserPlus className="size-3" />
          Invite members
        </Link>
      </div>

      {/* Sign out */}
      <div className="border-t border-sidebar-border/60 p-1.5">
        <button
          onClick={onSignOut}
          className="flex w-full items-center gap-2 px-2.5 py-1.5 text-[0.8125rem] text-destructive/70 hover:text-destructive hover:bg-sidebar-accent/60 transition-colors"
        >
          <SignOut className="size-3.5" />
          Sign out
        </button>
      </div>
    </div>
  )
}

export function AppSidebar() {
  const pathname = usePathname()
  const router = useRouter()
  const { user, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const { scope, setScope } = useActiveScope()
  const scopeLogin = scope?.login ?? ""

  // Same query key as the /collaborators redirect page so TanStack Query dedupes
  // the request when both are mounted. Only route the "Invite members" link at an
  // org where the user is actually an admin — mirrors the /collaborators fallback
  // logic (prefer the active org scope, else the first admin org, else /settings).
  const {
    data: memberships = [],
    isLoading: membershipsLoading,
    isSuccess: membershipsReady,
  } = useQuery<MyOrgMembership[]>({
    queryKey: ["my-orgs"],
    queryFn: () => api.orgs.mine(),
  })

  const { data: installs = [], isSuccess: installsReady } = useQuery<InstallationMeta[]>({
    queryKey: ["installations"],
    queryFn: () => api.installations.list(),
  })
  const personalInstall = installs.find((i) => i.account_type === "User")
  const slug = process.env.NEXT_PUBLIC_GITHUB_APP_SLUG
  // Always offer a way to install the App on another account/org (there's no cap on how
  // many an org admin can connect) — not only when the user has no personal install yet.
  const addInstallUrl = slug ? `https://github.com/apps/${slug}/installations/new` : null

  const scopeOptions: ScopeOption[] = useMemo(
    () => [
      ...(personalInstall
        ? [{ scope: { kind: "personal", login: personalInstall.account_login } as ActiveScope, label: personalInstall.account_login, sublabel: "Personal account" }]
        : []),
      ...memberships.map((m) => ({
        scope: { kind: "org", login: m.org_login } as ActiveScope,
        label: m.org_login,
        sublabel: `Organization · ${m.role}`,
      })),
    ],
    [personalInstall, memberships],
  )

  // Issue #371: when nothing is persisted yet, auto-select the first available scope (the
  // personal install if there is one, else the first org membership) as the real active
  // scope once the data loads -- otherwise the sidebar cosmetically shows an org under the
  // avatar while every page still says "no account selected yet" because `scope` was never
  // set. Only fires when nothing is persisted (`scope === null`); an explicit pick from the
  // profile menu always persists, so it's never overridden, and a multi-scope user can
  // still switch freely. `useRef` keeps it to a single attempt.
  //
  // Gate on isSuccess (not just !isLoading): a failed query also clears isLoading but
  // leaves the `= []` fallback in place, which could make a partial/empty result look like
  // "exactly one scope" and persist it. If a query genuinely errors we simply don't
  // auto-select and the user picks manually -- same as the pre-#371 baseline, no regression.
  const autoSelectedScope = useRef(false)
  useEffect(() => {
    if (autoSelectedScope.current || scope !== null) return
    if (!membershipsReady || !installsReady) return
    if (scopeOptions.length >= 1) {
      autoSelectedScope.current = true
      setScope(scopeOptions[0].scope)
    }
  }, [scope, membershipsReady, installsReady, scopeOptions, setScope])

  // Profile identity row reflects the active scope, falling back to real
  // membership/installation data if nothing has been picked yet.
  const displayLogin = scopeLogin || personalInstall?.account_login || memberships[0]?.org_login || ""
  const profile: Profile = {
    name: user?.name || user?.email || "Guest",
    org: displayLogin || "no organization connected",
    email: user?.email || "",
  }

  const adminOrgs = memberships.filter((m) => m.role === "admin")
  const inviteTarget = membershipsLoading
    ? undefined
    : adminOrgs.find((m) => scope?.kind === "org" && m.org_login === scope.login) || adminOrgs[0]
  const inviteHref = inviteTarget
    ? `/settings/org/${encodeURIComponent(inviteTarget.org_login)}/members`
    : "/settings"

  // Same resolve-then-use pattern as the Overview page — falls back to a saved
  // PAT for orgs without a GitHub App installation.
  const tokenQuery = useQuery({
    queryKey: ["tokens.resolve", scopeLogin],
    queryFn: () => api.tokens.resolve(scopeLogin),
    enabled: scopeLogin.trim().length > 0,
    retry: false,
  })

  // Same query key as the Overview page's cockpit query so TanStack Query dedupes
  // the request when both are mounted (same dedup pattern as ["my-orgs"] above).
  const { data: cockpit } = useQuery({
    queryKey: ["analytics.cockpit", scopeLogin],
    queryFn: () => api.analytics.cockpit(scopeLogin, tokenQuery.data?.token),
    enabled: scopeLogin.trim().length > 0 && !tokenQuery.isLoading,
    retry: false,
    refetchInterval: 30_000,
  })

  const healthDot = healthDotColor(cockpit?.latest_score)
  const lastSeenAt = typeof window !== "undefined" ? localStorage.getItem(ACTIVITY_LAST_SEEN_KEY) : null
  const unreadCount = (cockpit?.recent_events ?? []).filter(
    (e) => !lastSeenAt || e.created_at > lastSeenAt,
  ).length

  // Close on click outside
  useEffect(() => {
    if (!open) return
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [open])

  const initials = profile.name.charAt(0).toUpperCase()

  function isActive(href: string) {
    if (href === "/") return pathname === "/"
    return pathname === href || pathname.startsWith(href + "/")
  }

  return (
    <Sidebar>
      {/* Profile widget — opens dropdown */}
      <SidebarHeader className="border-b border-sidebar-border p-0 relative" ref={containerRef}>
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex w-full items-center gap-2.5 px-3.5 py-3 hover:bg-sidebar-accent/60 transition-colors group text-left"
        >
          <div className="size-7 rounded-md bg-primary/15 border border-primary/25 flex items-center justify-center shrink-0">
            <span className="text-[0.6875rem] font-semibold text-primary leading-none">{initials}</span>
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[0.8125rem] font-medium text-sidebar-foreground leading-none truncate">
              {profile.name}
            </p>
            <p className="text-[0.6875rem] text-sidebar-foreground/40 mt-0.5 leading-none truncate">
              {profile.org}
            </p>
          </div>
          <GearSix className="size-3 text-sidebar-foreground/20 group-hover:text-sidebar-foreground/50 transition-colors shrink-0" />
        </button>

        {open && (
          <ProfileDropdown
            profile={profile}
            scopeOptions={scopeOptions}
            activeScope={scope}
            onSelectScope={setScope}
            addInstallUrl={addInstallUrl}
            inviteHref={inviteHref}
            onClose={() => setOpen(false)}
            onSignOut={() => { logout(); setOpen(false); router.replace("/login") }}
          />
        )}
      </SidebarHeader>

      <SidebarContent>
        {groups.map((items, groupIndex) => (
          <div key={groupIndex}>
            {groupIndex > 0 && <SidebarSeparator className="my-1 bg-sidebar-border/60" />}
            <SidebarGroup className="py-1">
              <SidebarGroupContent>
                <SidebarMenu>
                  {items.map((item) => {
                    const active = isActive(item.href)
                    return (
                      <SidebarMenuItem key={item.title}>
                        <SidebarMenuButton
                          isActive={active}
                          className={[
                            "flex items-center rounded-md px-3 py-1.5 text-[0.8125rem]",
                            active
                              ? "bg-sidebar-accent text-sidebar-foreground font-medium"
                              : "text-sidebar-foreground/50 hover:text-sidebar-foreground hover:bg-sidebar-accent/60",
                          ].join(" ")}
                          render={<Link href={item.href} />}
                        >
                          <span>{item.title}</span>
                          {"showHealthDot" in item && item.showHealthDot && healthDot && (
                            <span className={`ml-auto size-1.5 rounded-full ${healthDot}`} />
                          )}
                          {"showUnreadBadge" in item && item.showUnreadBadge && unreadCount > 0 && (
                            <span className="ml-auto text-[0.625rem] font-medium bg-primary/20 text-primary rounded-full px-1.5 py-0.5 tabular-nums">
                              {unreadCount}
                            </span>
                          )}
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    )
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </div>
        ))}
      </SidebarContent>
    </Sidebar>
  )
}

"use client"

import { useParams, useRouter, useSearchParams } from "next/navigation"
import { useState } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { DataTable, type DataTableColumn } from "@/components/ui/data-table"
import { CircleNotch, EnvelopeSimple, Warning, X } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { addRevokingId, isRevoking, removeRevokingId } from "@/lib/revoke-pending"
import { relativeTime } from "@/lib/format"
import type { GithubOrgMember, InvitationOut } from "@/lib/api/types"

const MEMBER_COLUMNS: DataTableColumn<GithubOrgMember>[] = [
  {
    key: "login",
    header: "Member",
    sortValue: (m) => m.login.toLowerCase(),
    render: (m) => (
      <div className="flex items-center gap-2">
        {/* Decorative: the member login is the adjacent link text, so an empty alt
            avoids a duplicate screen-reader announcement. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={m.avatar_url} alt="" className="size-5 rounded-full" />
        <a
          href={`https://github.com/${m.login}`}
          target="_blank"
          rel="noreferrer"
          className="text-foreground/80 hover:text-foreground"
        >
          {m.login}
        </a>
      </div>
    ),
  },
  {
    key: "role",
    header: "Role",
    sortValue: (m) => m.role,
    cellClassName: "text-muted-foreground capitalize",
    render: (m) => m.role,
  },
  {
    key: "two_factor_enabled",
    header: "2FA",
    // Groups unknown (null) between the two known states rather than sorting it to an
    // arbitrary end -- there's no natural "less/more 2FA" ordering for "we don't know".
    sortValue: (m) => (m.two_factor_enabled === true ? 1 : m.two_factor_enabled === false ? -1 : 0),
    render: (m) => (
      <>
        {m.two_factor_enabled === true && <span className="stat-chip">✓ 2FA</span>}
        {m.two_factor_enabled === false && (
          <span className="stat-chip text-red-400 border-red-500/30">No 2FA</span>
        )}
      </>
    ),
  },
]

const ROSTER_TABS = [
  { id: "members", label: "Members" },
  { id: "outside", label: "Outside" },
  { id: "pending", label: "Pending GitHub invitations" },
  { id: "audit", label: "Audit" },
] as const

type RosterTabId = (typeof ROSTER_TABS)[number]["id"]

function GithubRoster({ orgLogin }: { orgLogin: string }) {
  const router = useRouter()
  const searchParams = useSearchParams()
  const rawTab = searchParams.get("roster") ?? "members"
  const tab = (ROSTER_TABS.some((t) => t.id === rawTab) ? rawTab : "members") as RosterTabId
  const [search, setSearch] = useState("")
  const [roleFilter, setRoleFilter] = useState<"all" | "member" | "admin">("all")

  function setTab(id: RosterTabId) {
    const params = new URLSearchParams(searchParams.toString())
    if (id === "members") params.delete("roster")
    else params.set("roster", id)
    router.replace(`?${params.toString()}`, { scroll: false })
  }

  // Falls back to a client-supplied PAT saved for this org when no GitHub App
  // installation covers it — same resolve-then-use pattern as security/page.tsx.
  // A missing/failed resolution just means the App installation (if any) is used.
  const tokenQuery = useQuery({
    queryKey: ["tokens.resolve", orgLogin],
    queryFn: () => api.tokens.resolve(orgLogin),
    retry: false,
  })
  const token = tokenQuery.data?.token

  // Wait for the token resolution to settle before firing so a saved PAT isn't
  // missed on the very first request (queryKey excludes token, so a late-arriving
  // token wouldn't otherwise trigger a refetch of an already-errored query).
  const tokenSettled = !tokenQuery.isLoading

  const membersQuery = useQuery({
    queryKey: ["collab", "members", orgLogin, roleFilter],
    queryFn: () => api.collab.members(orgLogin, roleFilter, token),
    enabled: tab === "members" && tokenSettled,
    // Keeps the previous role filter's rows on screen (with isFetching still true) while
    // the new one loads, instead of flashing an empty table -- a role change now looks
    // like a background refetch rather than a full reload, distinct from client-side search.
    placeholderData: keepPreviousData,
  })
  const outsideQuery = useQuery({
    queryKey: ["collab", "outside", orgLogin],
    queryFn: () => api.collab.outsideCollaborators(orgLogin, token),
    enabled: tab === "outside" && tokenSettled,
  })
  const pendingQuery = useQuery({
    queryKey: ["collab", "pending", orgLogin],
    queryFn: () => api.collab.invitations(orgLogin, token),
    enabled: tab === "pending" && tokenSettled,
  })
  const permissionAuditQuery = useQuery({
    queryKey: ["collab", "permission-audit", orgLogin],
    queryFn: () => api.collab.permissionAudit(orgLogin, token),
    enabled: tab === "audit" && tokenSettled,
  })
  const inactiveMembersQuery = useQuery({
    queryKey: ["collab", "inactive-members", orgLogin],
    queryFn: () => api.collab.inactiveMembers(orgLogin, 30, token),
    enabled: tab === "audit" && tokenSettled,
  })

  const activeQuery =
    tab === "members" ? membersQuery
    : tab === "outside" ? outsideQuery
    : tab === "pending" ? pendingQuery
    : permissionAuditQuery

  const outsideWithElevatedRepos = new Map<string, string[]>()
  for (const repo of permissionAuditQuery.data?.repos ?? []) {
    for (const c of repo.collaborators) {
      if (c.is_outside_collaborator && (c.permission === "write" || c.permission === "maintain" || c.permission === "admin")) {
        const existing = outsideWithElevatedRepos.get(c.login)
        if (existing) existing.push(repo.repo)
        else outsideWithElevatedRepos.set(c.login, [repo.repo])
      }
    }
  }

  const filteredMembers = (membersQuery.data?.members ?? []).filter((m) =>
    m.login.toLowerCase().includes(search.toLowerCase()),
  )
  // Derived from filteredMembers (not the unfiltered roster) so this count always matches
  // what's actually visible in the table below it -- a stale org-wide total next to a
  // filtered table reads as a data-integrity bug, especially for a 2FA-compliance number.
  const membersWithout2fa = filteredMembers.filter((m) => m.two_factor_enabled === false).length
  // roleFilter is part of membersQuery's queryKey (server-side refetch), while `search` is
  // client-side -- distinguish the two so a role change doesn't look identical to typing.
  const isRefetchingByRole = membersQuery.isFetching && !membersQuery.isLoading

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-title">GitHub organization roster</span>
      </div>

      <div className="px-4 py-2.5 border-b border-border flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-1.5">
          {ROSTER_TABS.map((t) => {
            const active = tab === t.id
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                aria-pressed={active}
                className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${
                  active
                    ? "border-border bg-elevated text-foreground"
                    : "border-transparent text-muted-foreground hover:bg-elevated"
                }`}
              >
                {t.label}
              </button>
            )
          })}
        </div>
        {tab === "members" && (
          <>
            <Input
              placeholder="Search by login…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="max-w-48 h-7 text-xs"
            />
            <select
              value={roleFilter}
              onChange={(e) => setRoleFilter(e.target.value as "all" | "member" | "admin")}
              className="text-xs card text-muted-foreground px-2 py-1 focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="all">All roles</option>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
            {isRefetchingByRole && (
              <span className="text-xs text-muted-foreground flex items-center gap-1">
                <CircleNotch className="size-3 animate-spin" /> Updating…
              </span>
            )}
          </>
        )}
      </div>

      {/* The audit tab renders two independently-fetched sections (permission audit,
          inactive members) and gives each its own loading/error state below -- gating the
          whole tab on permissionAuditQuery alone would hide inactive-members data that
          loaded fine just because the audit call happened to fail (#252). */}
      {tab !== "audit" && activeQuery.isLoading ? (
        <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-3.5 animate-spin" /> Loading…
        </div>
      ) : tab !== "audit" && activeQuery.isError ? (
        <div className="px-4 py-6">
          <p className="text-xs text-destructive">{activeQuery.error.message}</p>
        </div>
      ) : tab === "members" ? (
        filteredMembers.length === 0 ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground">No members found</p>
          </div>
        ) : (
          <>
            <DataTable columns={MEMBER_COLUMNS} data={filteredMembers} getRowKey={(m) => m.login} />
            {membersQuery.data?.two_factor_overlay_available && (
              <div className="px-4 py-2.5 border-t border-border">
                <span className="text-xs text-muted-foreground">Members without 2FA: {membersWithout2fa}</span>
              </div>
            )}
          </>
        )
      ) : tab === "outside" ? (
        (outsideQuery.data?.collaborators.length ?? 0) === 0 ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground">No outside collaborators</p>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Collaborator</th>
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Repos</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {outsideQuery.data!.collaborators.map((c) => (
                    <tr key={c.login}>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          {/* Decorative: the collaborator login is the adjacent text, so an
                              empty alt avoids a duplicate screen-reader announcement. */}
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img src={c.avatar_url} alt="" className="size-5 rounded-full" />
                          <span className="text-foreground/80">{c.login}</span>
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">
                        <div className="flex flex-wrap gap-1">
                          {c.repos.map((r) => (
                            <span key={r} className="stat-chip">{r}</span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {outsideQuery.data && outsideQuery.data.repos_scanned < outsideQuery.data.repos_total && (
              <div className="px-4 py-2.5 border-t border-border">
                <span className="text-xs text-muted-foreground">
                  Scanned {outsideQuery.data.repos_scanned} of {outsideQuery.data.repos_total} repos
                </span>
              </div>
            )}
          </>
        )
      ) : tab === "pending" ? ((pendingQuery.data?.invitations.length ?? 0) === 0 ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground">No pending GitHub invitations</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Invitee</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Role</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Invited</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Inviter</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {pendingQuery.data!.invitations.map((inv, i) => (
                  <tr key={i}><td className="px-4 py-2.5 text-foreground/80">{inv.login ?? inv.email}</td><td className="px-4 py-2.5 text-muted-foreground capitalize">{inv.role}</td><td className="px-4 py-2.5 text-muted-foreground">{relativeTime(inv.invited_at)}</td><td className="px-4 py-2.5 text-muted-foreground">{inv.inviter ?? "—"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : (
        <>
          {outsideWithElevatedRepos.size > 0 && (
            <div className="px-4 py-3 border-b border-border bg-yellow-500/5">
              <p className="text-xs font-medium text-yellow-400 flex items-center gap-1.5 mb-1.5"><Warning className="size-3.5" /> Access Risk: {outsideWithElevatedRepos.size} outside collaborator{outsideWithElevatedRepos.size === 1 ? "" : "s"} with write/admin access</p>
              <ul className="text-xs text-muted-foreground flex flex-col gap-0.5">
                {[...outsideWithElevatedRepos.entries()].map(([login, repos]) => (
                  <li key={login}>{login} · {repos.join(", ")}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="px-4 py-2.5 border-b border-border">
            <span className="text-xs font-medium text-foreground">Permission audit</span>
            <span className="text-xs text-muted-foreground ml-2">
              generated {permissionAuditQuery.data ? relativeTime(permissionAuditQuery.data.generated_at) : "—"}
              {permissionAuditQuery.data && permissionAuditQuery.data.repos_scanned < permissionAuditQuery.data.repos_total && (
                <> · scanned {permissionAuditQuery.data.repos_scanned} of {permissionAuditQuery.data.repos_total} repos</>
              )}
            </span>
          </div>
          {permissionAuditQuery.isLoading ? (
            <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
              <CircleNotch className="size-3.5 animate-spin" /> Loading…
            </div>
          ) : permissionAuditQuery.isError ? (
            <div className="px-4 py-6">
              <p className="text-xs text-destructive">{permissionAuditQuery.error.message}</p>
            </div>
          ) : (
            <div className="overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Repo</th>
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Collaborator</th>
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Affiliation</th>
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Permission</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(permissionAuditQuery.data?.repos ?? []).flatMap((repo) =>
                    repo.collaborators.map((c) => (
                      <tr key={`${repo.repo}-${c.login}`} className={c.is_outside_collaborator && (c.permission === "write" || c.permission === "maintain" || c.permission === "admin") ? "bg-yellow-500/5" : ""}>
                        <td className="px-4 py-2 font-mono text-muted-foreground truncate max-w-[10rem]">{repo.repo}</td>
                        <td className="px-4 py-2 text-foreground/80">{c.login}</td>
                        <td className="px-4 py-2 text-muted-foreground">{c.affiliation}</td>
                        <td className="px-4 py-2 text-muted-foreground capitalize">{c.permission}</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          )}

          <div className="px-4 py-2.5 border-b border-t border-border">
            <span className="text-xs font-medium text-foreground">Inactive members (30d+)</span>
            {inactiveMembersQuery.data && inactiveMembersQuery.data.sampled_repos.length > 0 && (
              <span className="text-xs text-muted-foreground ml-2">
                sampled from {inactiveMembersQuery.data.sampled_repos.join(", ")} — approximation, not exact
              </span>
            )}
          </div>
          {inactiveMembersQuery.isLoading ? (
            <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
              <CircleNotch className="size-3.5 animate-spin" /> Loading…
            </div>
          ) : inactiveMembersQuery.error ? (
            // Distinct from the "no inactive members" empty state below -- an error here
            // must not silently read as "org is clean," which would be a false-negative
            // access-risk signal.
            <div className="px-4 py-6">
              <p className="text-xs text-destructive">{inactiveMembersQuery.error.message}</p>
            </div>
          ) : (inactiveMembersQuery.data?.members.length ?? 0) === 0 ? (
            <div className="px-4 py-6">
              <p className="text-sm text-muted-foreground">No inactive members found (within the sampled repos)</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <tbody className="divide-y divide-border">
                  {inactiveMembersQuery.data!.members.map((m) => (
                    <tr key={m.login}>
                      <td className="px-4 py-2 text-foreground/80">{m.login}</td>
                      <td className="px-4 py-2 text-muted-foreground capitalize">{m.role}</td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {m.last_commit_days_ago != null
                          ? `last commit ${m.last_commit_days_ago}d ago in ${m.last_commit_repo}`
                          : "no commits found in sampled repos"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default function OrgMembersPage() {
  const params = useParams<{ login: string }>()
  const orgLogin = params.login
  const queryClient = useQueryClient()
  const [email, setEmail] = useState("")
  const [lastLink, setLastLink] = useState<string | null>(null)

  const { data: invitations = [], isLoading } = useQuery<InvitationOut[]>({
    queryKey: ["invitations", orgLogin],
    queryFn: () => api.invitations.list(orgLogin),
  })

  const [revokingIds, setRevokingIds] = useState<Set<number>>(() => new Set())

  const invite = useMutation({
    mutationFn: () => api.invitations.create(orgLogin, email.trim()),
    onSuccess: (data) => {
      setLastLink(data.invite_link)
      setEmail("")
      queryClient.invalidateQueries({ queryKey: ["invitations", orgLogin] })
    },
  })

  const revoke = useMutation({
    mutationFn: (id: number) => api.invitations.revoke(orgLogin, id),
    onMutate: (id) => setRevokingIds((prev) => addRevokingId(prev, id)),
    onSettled: (_data, _error, id) => setRevokingIds((prev) => removeRevokingId(prev, id)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["invitations", orgLogin] }),
  })

  return (
    <>
      <PageHeader title="Members" description={`Manage who can access ${orgLogin} in Clevis.`} />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-title">Invite a member</span>
          </div>
          <div className="p-4 flex flex-col gap-3">
            <div>
              <label className="text-xs font-medium text-foreground block mb-1.5">Email</label>
              <Input
                placeholder="teammate@example.com"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && email && !invite.isPending && invite.mutate()}
              />
            </div>
            <Button onClick={() => invite.mutate()} disabled={invite.isPending || !email}>
              <EnvelopeSimple className="size-3.5" />
              {invite.isPending ? "Inviting…" : "Send invite"}
            </Button>
            {invite.isError && <p className="text-xs text-destructive">{invite.error.message}</p>}
            {lastLink && (
              <div className="text-xs text-muted-foreground break-all bg-muted/30 border border-border/50 rounded-md p-2">
                Share this link — no email is sent automatically:
                <div className="font-mono text-foreground/80 mt-1">{lastLink}</div>
              </div>
            )}
          </div>
        </div>

        <div className="card lg:col-span-2">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-title">Clevis workspace invitations</span>
          </div>
          {isLoading ? (
            <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
              <CircleNotch className="size-3.5 animate-spin" /> Loading…
            </div>
          ) : invitations.length === 0 ? (
            <div className="px-4 py-8">
              <p className="text-sm text-muted-foreground">No invitations yet</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Email</th>
                    <th className="text-left text-muted-foreground font-medium px-4 py-2">Status</th>
                    <th className="text-right text-muted-foreground font-medium px-4 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {invitations.map((inv) => (
                    <tr key={inv.id}>
                      <td className="px-4 py-2.5 text-foreground/80">{inv.email}</td>
                      <td className="px-4 py-2.5 text-muted-foreground">{inv.status}</td>
                      <td className="px-4 py-2.5 text-right">
                        {inv.status === "pending" && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => revoke.mutate(inv.id)}
                            disabled={isRevoking(revokingIds, inv.id)}
                          >
                            <X className="size-3" />
                            Revoke
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="mt-4">
        <GithubRoster orgLogin={orgLogin} />
      </div>
    </>
  )
}

"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useMutation, useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Warning, Key, CircleNotch, Lock, Star, GitPullRequest, ArrowSquareOut } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import { shouldApplyResolvedToken } from "@/lib/token-resolve"
import { MiniSparkline } from "@/components/charts/mini-sparkline"
import { relativeTime } from "@/lib/format"
import { useInView } from "@/lib/use-in-view"
import type { InstallationMeta, RepoSummary } from "@/lib/api/types"

type SortKey = "pushed" | "stars" | "name"

function sortRepos(repos: RepoSummary[], sort: SortKey): RepoSummary[] {
  const sorted = [...repos]
  if (sort === "stars") sorted.sort((a, b) => b.stargazers_count - a.stargazers_count)
  else if (sort === "name") sorted.sort((a, b) => a.name.localeCompare(b.name))
  else sorted.sort((a, b) => (b.pushed_at ?? "").localeCompare(a.pushed_at ?? ""))
  return sorted
}

function CellLoadError() {
  return (
    <span className="inline-flex items-center gap-1 text-destructive text-[0.6875rem]" title="Failed to load">
      <Warning className="size-3" /> failed to load
    </span>
  )
}

function RepoActivityCell({ org, repo, token }: { org: string; repo: string; token: string }) {
  const [ref, inView] = useInView<HTMLDivElement>()
  const { data, isLoading, isError } = useQuery({
    queryKey: ["repo-stats", org, repo, token],
    queryFn: () => api.repos.stats(org, org, repo, token),
    enabled: inView,
  })

  const weeks = (data?.commit_activity ?? []).slice(-8).map((w) => w.total)
  return (
    <div ref={ref}>
      {!inView || isLoading ? (
        <Skeleton className="h-8 w-24" />
      ) : isError ? (
        <CellLoadError />
      ) : weeks.length === 0 || weeks.every((n) => n === 0) ? (
        <span className="text-muted-foreground text-[0.6875rem]">— no recent activity</span>
      ) : (
        <MiniSparkline data={weeks} height={28} />
      )}
    </div>
  )
}

function RepoReleaseCell({ org, repo, token }: { org: string; repo: string; token: string }) {
  const [ref, inView] = useInView<HTMLDivElement>()
  // Same query key as RepoActivityCell — React Query dedupes the fetch, this just
  // reads a different field off the already-fetched (or in-flight) stats response.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["repo-stats", org, repo, token],
    queryFn: () => api.repos.stats(org, org, repo, token),
    enabled: inView,
  })

  const release = data?.latest_release
  return (
    <div ref={ref}>
      {!inView || isLoading ? (
        <Skeleton className="h-3 w-16 ml-auto" />
      ) : isError ? (
        <CellLoadError />
      ) : !release ? (
        <span className="text-muted-foreground text-[0.6875rem]">—</span>
      ) : (
        <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap">
          {release.tag_name}
          {release.published_at && <> · {relativeTime(release.published_at)}</>}
        </span>
      )}
    </div>
  )
}

function RepoPullsCell({ org, repo, token }: { org: string; repo: string; token: string }) {
  const [ref, inView] = useInView<HTMLDivElement>()
  const { data, isLoading, isError } = useQuery({
    queryKey: ["repo-pulls", org, repo, token],
    queryFn: () => api.repos.pulls(org, org, repo, token),
    enabled: inView,
  })

  return (
    <div ref={ref}>
      {!inView || isLoading ? (
        <Skeleton className="h-4 w-10 ml-auto" />
      ) : isError ? (
        <CellLoadError />
      ) : (
        <span className="inline-flex items-center gap-1 text-muted-foreground tabular-nums">
          <GitPullRequest className="size-3.5" />
          {data?.total ?? 0}
        </span>
      )}
    </div>
  )
}

function RepoRow({ org, repo, token }: { org: string; repo: RepoSummary; token: string }) {
  return (
    <tr className="hover:bg-muted/40 transition-colors">
      <td className="px-4 py-2.5 max-w-[16rem]">
        <div className="flex items-center gap-1.5">
          {repo.private && <Lock className="size-3 text-muted-foreground shrink-0" />}
          <Link
            href={`/repos/${encodeURIComponent(org)}~${encodeURIComponent(repo.name)}`}
            className="font-mono text-foreground/90 hover:text-primary transition-colors truncate"
          >
            {repo.name}
          </Link>
          <a
            href={repo.html_url}
            target="_blank"
            rel="noreferrer"
            title="Open on GitHub"
            className="text-muted-foreground hover:text-foreground transition-colors shrink-0"
          >
            <ArrowSquareOut className="size-3" />
          </a>
        </div>
        {repo.description && (
          <p className="text-[0.6875rem] text-muted-foreground truncate mt-0.5">{repo.description}</p>
        )}
      </td>
      <td className="px-4 py-2.5 text-muted-foreground">{repo.language ?? "—"}</td>
      <td className="px-4 py-2.5 text-right text-muted-foreground tabular-nums">
        <span className="inline-flex items-center gap-1">
          <Star className="size-3.5" />
          {repo.stargazers_count}
        </span>
      </td>
      <td className="px-4 py-2.5 text-right">
        <RepoPullsCell org={org} repo={repo.name} token={token} />
      </td>
      <td className="px-4 py-2.5 w-32">
        <RepoActivityCell org={org} repo={repo.name} token={token} />
      </td>
      <td className="px-4 py-2.5 text-right">
        <RepoReleaseCell org={org} repo={repo.name} token={token} />
      </td>
      <td className="px-4 py-2.5 text-right text-muted-foreground whitespace-nowrap">
        {repo.pushed_at ? relativeTime(repo.pushed_at) : "—"}
      </td>
      <td className="px-4 py-2.5 text-right whitespace-nowrap">
        <Link
          href={`/repos/${encodeURIComponent(org)}~${encodeURIComponent(repo.name)}/cache`}
          className="text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          Cache →
        </Link>
      </td>
    </tr>
  )
}

export default function ReposPage() {
  const [owner, setOwner] = useState("")
  const [token, setToken] = useState("")
  const [tokenSaved, setTokenSaved] = useState(false)
  const [search, setSearch] = useState("")
  const [sort, setSort] = useState<SortKey>("pushed")

  const { scope } = useActiveScope()
  const scopeOrgLogin = scope?.kind === "org" ? scope.login : ""
  // Repo listing is org-only (/orgs/{org}/repos) — pre-fill from an org scope, and
  // clear (not just skip) when switching to personal so a stale org doesn't linger.
  useEffect(() => {
    setOwner(scopeOrgLogin)
  }, [scopeOrgLogin])

  const { data: installs = [] } = useQuery<InstallationMeta[]>({
    queryKey: ["installations"],
    queryFn: () => api.installations.list(),
  })
  // list() above only covers the caller's *personal* installations -- an org's App
  // installation requires the separate org-scoped endpoint. Errors (403/404, e.g. the
  // org isn't a recognized Clevis org yet) are treated as "not installed" rather than
  // surfaced, matching this query's only purpose here (a soft signal to hide the token
  // field, not something the user needs an error for).
  const orgInstallsQuery = useQuery<InstallationMeta[]>({
    queryKey: ["installations.org", owner.trim()],
    queryFn: () => api.installations.listForOrg(owner.trim()),
    enabled: owner.trim().length > 0,
    retry: false,
  })
  const hasInstallationForOwner =
    installs.some((i) => i.account_login === owner.trim()) || (orgInstallsQuery.data?.length ?? 0) > 0

  const resolveMutation = useMutation({
    mutationFn: (org: string) => api.tokens.resolve(org),
    onSuccess: (data, org) => {
      // Skip applying a legacy saved token once an installation covers this owner --
      // otherwise it'd be silently used (the token field, and its "saved" indicator,
      // are hidden in that case) and could override the installation-token path the
      // hidden field implies is now authoritative.
      if (shouldApplyResolvedToken(org, owner) && !hasInstallationForOwner) {
        setToken(data.token)
        setTokenSaved(true)
      }
    },
    onError: () => setTokenSaved(false),
  })

  useEffect(() => {
    setToken("")
    setTokenSaved(false)
    if (owner.trim().length > 2) resolveMutation.mutate(owner.trim())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner])

  const saveTokenMutation = useMutation({
    mutationFn: () => api.tokens.upsert(owner.trim(), token.trim()),
    onSuccess: () => setTokenSaved(true),
  })

  // Frozen at the moment "Load repositories" is triggered — otherwise editing the org or
  // token fields after a list is loaded would point already-rendered rows at the wrong
  // scope (per-row requests for the new org against the old org's repo names) or refetch
  // every row on each keystroke. Passed as mutate()'s per-call onSuccess rather than the
  // hook-level one: TanStack Query re-binds hook-level callbacks on every render, so if the
  // user edits owner/token again *while the request is still in flight*, a hook-level
  // onSuccess would use the edited (wrong) values instead of the ones actually requested.
  const [loadedOrg, setLoadedOrg] = useState("")
  const [loadedToken, setLoadedToken] = useState("")

  const listMutation = useMutation({
    mutationFn: () => api.repos.list(owner.trim(), token),
  })

  function loadRepos() {
    const requestedOrg = owner.trim()
    const requestedToken = token
    listMutation.mutate(undefined, {
      onSuccess: () => {
        setLoadedOrg(requestedOrg)
        setLoadedToken(requestedToken)
      },
    })
  }

  const repos = sortRepos(
    (listMutation.data?.repos ?? []).filter((r) => r.name.toLowerCase().includes(search.toLowerCase())),
    sort,
  )

  return (
    <>
      <PageHeader
        title="Repositories"
        description="Browse an organization's repositories — activity, open PRs, and cache access."
      />

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Config panel */}
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-title">Organization</span>
          </div>
          <div className="p-4 flex flex-col gap-3">
            <div>
              <label className="text-xs font-medium text-foreground block mb-1.5">Organization</label>
              <Input
                placeholder="e.g. octocat"
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && owner.trim() && !listMutation.isPending && loadRepos()}
              />
            </div>
            {!hasInstallationForOwner && (
            <div>
              <label className="text-xs font-medium text-foreground mb-1.5 flex items-center gap-1.5">
                GitHub Token
                <span className="text-[0.6875rem] text-muted-foreground font-normal">
                  optional if the GitHub App is connected for this org
                </span>
                {tokenSaved && (
                  <span className="inline-flex items-center gap-1 text-[0.6875rem] text-primary">
                    <Key className="size-3" />saved
                  </span>
                )}
              </label>
              <Input
                placeholder="ghp_... (leave blank to use the connected GitHub App)"
                type="password"
                value={token}
                onChange={(e) => { setToken(e.target.value); setTokenSaved(false) }}
                className="font-mono"
                onKeyDown={(e) => e.key === "Enter" && owner.trim() && !listMutation.isPending && loadRepos()}
              />
            </div>
            )}
            <Button
              onClick={loadRepos}
              disabled={listMutation.isPending || !owner.trim()}
              className="mt-1"
            >
              {listMutation.isPending ? (
                <><CircleNotch className="size-3.5 animate-spin" />Loading…</>
              ) : (
                "Load repositories"
              )}
            </Button>
            {!tokenSaved && token && owner && (
              <Button
                variant="outline"
                onClick={() => saveTokenMutation.mutate()}
                disabled={saveTokenMutation.isPending}
              >
                <Key className="size-3.5" />
                {saveTokenMutation.isPending ? "Saving…" : "Save token for this org"}
              </Button>
            )}
            {listMutation.isError && (
              <div className="flex items-start gap-2 text-xs text-destructive">
                <Warning className="size-3.5 mt-0.5 shrink-0" />
                {listMutation.error.message}
              </div>
            )}
          </div>
        </div>

        {/* Repo table */}
        {(listMutation.data || listMutation.isPending) && (
          <div className="card lg:col-span-2">
            <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
              <span className="section-title">Repositories</span>
              <div className="flex items-center gap-2">
                {listMutation.data && (
                  <>
                    <Input
                      placeholder="Filter by name…"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      className="h-7 w-40 text-xs"
                    />
                    <select
                      value={sort}
                      onChange={(e) => setSort(e.target.value as SortKey)}
                      className="text-xs card text-muted-foreground px-2 py-1 focus:outline-none focus:ring-1 focus:ring-ring"
                    >
                      <option value="pushed">Sort: Pushed</option>
                      <option value="stars">Sort: Stars</option>
                      <option value="name">Sort: Name</option>
                    </select>
                    <span className="stat-chip">{repos.length} of {listMutation.data.total}</span>
                  </>
                )}
              </div>
            </div>

            {listMutation.isPending ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-border">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <tr key={i}>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-32" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-16" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-10 ml-auto" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-10 ml-auto" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-6 w-24" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-16 ml-auto" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-16 ml-auto" /></td>
                        <td className="px-4 py-3"><Skeleton className="h-3 w-10 ml-auto" /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : repos.length === 0 ? (
              <div className="px-4 py-8">
                <p className="text-sm text-muted-foreground">
                  No repositories match{search ? " your filter" : ""}
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left text-muted-foreground font-medium px-4 py-2">Repository</th>
                      <th className="text-left text-muted-foreground font-medium px-4 py-2">Language</th>
                      <th className="text-right text-muted-foreground font-medium px-4 py-2">Stars</th>
                      <th className="text-right text-muted-foreground font-medium px-4 py-2">Open PRs</th>
                      <th className="text-left text-muted-foreground font-medium px-4 py-2">Activity (8w)</th>
                      <th className="text-right text-muted-foreground font-medium px-4 py-2">Release</th>
                      <th className="text-right text-muted-foreground font-medium px-4 py-2">Pushed</th>
                      <th className="px-4 py-2" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {repos.map((r) => (
                      <RepoRow key={r.full_name} org={loadedOrg} repo={r} token={loadedToken} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

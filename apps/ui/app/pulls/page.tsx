"use client"

import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { SectionError } from "@/components/section-error"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { GitPullRequest } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import { relativeTime } from "@/lib/format"
import type { PullSummary } from "@/lib/api/types"

// This is the primary PR view (not a secondary tab like Activity's PR Board), so unlike
// that tab's MAX_REPOS_FOR_PR_BOARD there's no hard cap on repo count -- instead requests
// are fanned out in batches so an org with many repos doesn't fire dozens of simultaneous
// requests at once.
const REPO_BATCH_SIZE = 10

interface PullRow extends PullSummary {
  repo: string
}

type GroupBy = "repo" | "author"

export default function PullRequestsPage() {
  const { scope } = useActiveScope()
  const org = scope?.login ?? ""
  const hasOrg = org.trim().length > 0

  // Activity's old "PR Board" tab was the same open-PRs data grouped by author (issue
  // #284); it's a view toggle here now, and that tab is gone from Activity.
  const [groupBy, setGroupBy] = useState<GroupBy>("repo")

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: hasOrg,
    retry: false,
  })
  const token = resolveQuery.data?.token ?? ""
  // Same reasoning as Activity: queries fire once token resolution has settled either
  // way, so an org connected purely via GitHub App installation (no saved PAT) isn't
  // permanently blocked (see #251).
  const queriesEnabled = hasOrg && !resolveQuery.isLoading

  const reposQuery = useQuery({
    queryKey: ["repos.list", org],
    queryFn: () => api.repos.list(org, token),
    enabled: queriesEnabled,
    retry: false,
  })

  const repoNames = reposQuery.data?.repos.map((r) => r.name) ?? []

  const pullsQuery = useQuery({
    queryKey: ["repos.pulls.all", org, repoNames.join(",")],
    queryFn: async () => {
      const rows: PullRow[] = []
      for (let i = 0; i < repoNames.length; i += REPO_BATCH_SIZE) {
        const batch = repoNames.slice(i, i + REPO_BATCH_SIZE)
        const results = await Promise.all(
          batch.map((repo) =>
            api.repos
              .pulls(org, org, repo, token)
              .then((r) => r.pulls.map((p) => ({ ...p, repo })))
              .catch(() => [] as PullRow[]),
          ),
        )
        rows.push(...results.flat())
      }
      return rows.sort((a, b) => b.created_at.localeCompare(a.created_at))
    },
    enabled: queriesEnabled && !!reposQuery.data,
    retry: false,
  })

  const pulls = pullsQuery.data ?? []
  const isLoading = reposQuery.isLoading || (reposQuery.isSuccess && pullsQuery.isLoading)

  const byAuthor = new Map<string, PullRow[]>()
  for (const p of pulls) {
    const author = p.user ?? "unknown"
    const existing = byAuthor.get(author)
    if (existing) existing.push(p)
    else byAuthor.set(author, [p])
  }

  return (
    <>
      <PageHeader title="Pull Requests" description="Open PRs across your organization." />
      <div className="card">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3 flex-wrap">
          <span className="section-title">Open Pull Requests</span>
          <div className="flex items-center gap-3">
            {pulls.length > 0 && <span className="stat-chip">{pulls.length} total</span>}
            <div className="flex items-center gap-1.5" role="group" aria-label="Group pull requests by">
              {(["repo", "author"] as const).map((g) => (
                <button
                  key={g}
                  type="button"
                  onClick={() => setGroupBy(g)}
                  aria-pressed={groupBy === g}
                  className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${
                    groupBy === g
                      ? "border-border bg-elevated text-foreground"
                      : "border-transparent text-muted-foreground hover:bg-elevated"
                  }`}
                >
                  by {g}
                </button>
              ))}
            </div>
          </div>
        </div>
        {!hasOrg ? (
          <EmptyStateNoAccount bare />
        ) : reposQuery.isError ? (
          <SectionError
            message={reposQuery.error instanceof Error ? reposQuery.error.message : "Failed to load repositories."}
            onRetry={() => reposQuery.refetch()}
            retrying={reposQuery.isFetching}
          />
        ) : isLoading ? (
          <div className="p-4 flex flex-col gap-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : pulls.length === 0 ? (
          <p className="px-4 py-8 text-sm text-muted-foreground">No open pull requests</p>
        ) : groupBy === "author" ? (
          <div className="p-4 grid gap-3 sm:grid-cols-2">
            {[...byAuthor.entries()].map(([author, authorPulls]) => (
              <div key={author} className="border border-border/60 rounded-md p-3">
                <p className="text-xs font-medium text-foreground mb-2">
                  {author} <span className="text-muted-foreground font-normal">· {authorPulls.length}</span>
                </p>
                <ul className="flex flex-col gap-1.5">
                  {authorPulls.map((p) => (
                    <li key={`${p.repo}-${p.number}`}>
                      <a
                        href={p.html_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-muted-foreground hover:text-foreground transition-colors truncate block"
                      >
                        <span className="text-foreground/60">{p.repo}</span> #{p.number} {p.title}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Repo</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Pull Request</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Author</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Opened</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {pulls.map((p) => (
                  <tr key={`${p.repo}-${p.number}`} className="hover:bg-muted/40 transition-colors">
                    <td className="px-4 py-2.5 text-muted-foreground">{p.repo}</td>
                    <td className="px-4 py-2.5">
                      <a
                        href={p.html_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-foreground/90 hover:text-primary transition-colors"
                      >
                        <GitPullRequest className="size-3.5 shrink-0" />
                        #{p.number} {p.title}
                      </a>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">{p.user ?? "unknown"}</td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground whitespace-nowrap">
                      {relativeTime(p.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

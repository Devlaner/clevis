"use client"

import { useQuery } from "@tanstack/react-query"
import Link from "next/link"
import { PageHeader } from "@/components/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { SectionError } from "@/components/section-error"
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

export default function PullRequestsPage() {
  const { scope } = useActiveScope()
  const org = scope?.login ?? ""
  const hasOrg = org.trim().length > 0

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

  return (
    <>
      <PageHeader title="Pull Requests" description="Open PRs across your organization." />
      <div className="card">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <span className="section-title">Open Pull Requests</span>
          {pulls.length > 0 && <span className="stat-chip">{pulls.length} total</span>}
        </div>
        {!hasOrg ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground">
              No account selected yet. Pick one from the profile menu, or{" "}
              <Link href="/security" className="text-primary hover:underline">
                configure →
              </Link>
            </p>
          </div>
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

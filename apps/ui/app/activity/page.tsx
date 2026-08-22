"use client"

import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { ActivityList } from "@/components/activity-list"
import { EventFeed } from "@/components/event-feed"
import { HeatmapCalendar } from "@/components/charts/heatmap-calendar"
import { SectionError } from "@/components/section-error"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { CHART_COLORS } from "@/lib/charts/theme"
import { relativeTime } from "@/lib/format"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import type { PullSummary } from "@/lib/api/types"

const EVENTS_REFRESH_SECONDS = 30
const HEATMAP_COLOR_SCALE = [CHART_COLORS.grid, "#1d4ed8", "#3b82f6", "#60a5fa", "#93c5fd"]
const MAX_REPOS_FOR_PR_BOARD = 10

// Isolated into its own component so the 1s tick only re-renders this small chip,
// not the whole page (and the feed/job lists below it).
function RefreshCountdown({ resetKey, seconds }: { resetKey: number; seconds: number }) {
  const [remaining, setRemaining] = useState(seconds)

  useEffect(() => {
    setRemaining(seconds)
  }, [resetKey, seconds])

  useEffect(() => {
    const interval = setInterval(() => {
      setRemaining((r) => (r > 0 ? r - 1 : 0))
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  return <span className="stat-chip">refreshes in {remaining}s</span>
}

type PrBoardTab = "feed" | "board"

export default function ActivityPage() {
  // Marks all cockpit-sourced events as read so the sidebar's unread badge
  // clears once the user has actually looked at this page.
  useEffect(() => {
    localStorage.setItem("activity_last_seen_at", new Date().toISOString())
  }, [])

  const { data: jobs = [], isLoading: jobsLoading } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs.list,
    refetchInterval: 15_000,
  })

  const { scope } = useActiveScope()
  const org = scope?.login ?? ""

  const [feedTab, setFeedTab] = useState<PrBoardTab>("feed")

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 0,
    retry: false,
  })

  const token = resolveQuery.data?.token ?? ""
  // Queries fire once an org is set and token resolution has settled, regardless of
  // whether a saved PAT was found -- an org connected purely via GitHub App
  // installation (no PAT ever saved, a fully supported flow) has no `token` here, but
  // the API resolves an installation token server-side the same way Overview's queries
  // already rely on (see app/page.tsx). Gating on a resolved PAT specifically made this
  // page permanently blank for App-only orgs (#251).
  const hasOrg = org.trim().length > 0
  const queriesEnabled = hasOrg && !resolveQuery.isLoading

  const eventsQuery = useQuery({
    queryKey: ["github.events", org],
    queryFn: () => api.github.events(org, token),
    enabled: queriesEnabled,
    retry: false,
    refetchInterval: EVENTS_REFRESH_SECONDS * 1000,
  })

  // Reset the countdown on either a successful fetch OR a failed one, so it always
  // tracks the real refetchInterval cadence instead of sticking at 0 after an error.
  const lastAttemptAt = Math.max(eventsQuery.dataUpdatedAt, eventsQuery.errorUpdatedAt)

  // Heatmap data rides on the personal cockpit endpoint (commit_heatmap_52w) --
  // that endpoint is personal-scoped (no OrgMembership needed), unlike the
  // org-scoped failed-runs/release-timeline calls below, but the same resolved
  // token works for either since it's just a client-supplied PAT either way.
  const cockpitQuery = useQuery({
    queryKey: ["analytics.cockpit-heatmap", org],
    queryFn: () => api.analytics.cockpit(org, token),
    enabled: queriesEnabled,
    retry: false,
  })

  const failedRunsQuery = useQuery({
    queryKey: ["github.failed-runs", org],
    queryFn: () => api.github.failedRuns(org, token),
    enabled: queriesEnabled,
    retry: false,
  })

  const releaseTimelineQuery = useQuery({
    queryKey: ["github.release-timeline", org],
    queryFn: () => api.github.releaseTimeline(org, token),
    enabled: queriesEnabled,
    retry: false,
  })

  const reposQuery = useQuery({
    queryKey: ["repos.list", org],
    queryFn: () => api.repos.list(org, token),
    enabled: queriesEnabled && feedTab === "board",
    retry: false,
  })

  const prBoardQuery = useQuery({
    queryKey: ["repos.pulls.board", org, reposQuery.data?.repos.map((r) => r.name).join(",")],
    queryFn: async () => {
      const repoNames = (reposQuery.data?.repos ?? []).slice(0, MAX_REPOS_FOR_PR_BOARD).map((r) => r.name)
      const results = await Promise.all(
        repoNames.map((repo) =>
          api.repos.pulls(org, org, repo, token).catch(() => ({ repository: repo, total: 0, pulls: [] })),
        ),
      )
      return results.flatMap((r) => r.pulls)
    },
    enabled: queriesEnabled && feedTab === "board" && !!reposQuery.data,
    retry: false,
  })

  const prsByAuthor = new Map<string, PullSummary[]>()
  for (const pr of prBoardQuery.data ?? []) {
    const author = pr.user ?? "unknown"
    const existing = prsByAuthor.get(author)
    if (existing) existing.push(pr)
    else prsByAuthor.set(author, [pr])
  }

  return (
    <>
      <PageHeader
        title="Activity"
        description="Recent GitHub activity and background jobs."
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2 card">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setFeedTab("feed")}
                aria-pressed={feedTab === "feed"}
                className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${
                  feedTab === "feed"
                    ? "border-border bg-elevated text-foreground"
                    : "border-transparent text-muted-foreground hover:bg-elevated"
                }`}
              >
                Activity Feed
              </button>
              <button
                onClick={() => setFeedTab("board")}
                aria-pressed={feedTab === "board"}
                className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${
                  feedTab === "board"
                    ? "border-border bg-elevated text-foreground"
                    : "border-transparent text-muted-foreground hover:bg-elevated"
                }`}
              >
                PR Board
              </button>
            </div>
            {feedTab === "feed" && hasOrg && <RefreshCountdown resetKey={lastAttemptAt} seconds={EVENTS_REFRESH_SECONDS} />}
          </div>
          {!hasOrg ? (
            <EmptyStateNoAccount bare />
          ) : feedTab === "feed" && eventsQuery.isError ? (
            <SectionError
              message={eventsQuery.error instanceof Error ? eventsQuery.error.message : "Failed to load events."}
              onRetry={() => eventsQuery.refetch()}
              retrying={eventsQuery.isFetching}
            />
          ) : feedTab === "feed" ? (
            <EventFeed events={eventsQuery.data?.events ?? []} isLoading={eventsQuery.isLoading} />
          ) : prBoardQuery.isLoading || reposQuery.isLoading ? (
            <p className="px-4 py-8 text-sm text-muted-foreground">Loading…</p>
          ) : prsByAuthor.size === 0 ? (
            <p className="px-4 py-8 text-sm text-muted-foreground">No open pull requests</p>
          ) : (
            <div className="p-4 grid gap-3 sm:grid-cols-2">
              {[...prsByAuthor.entries()].map(([author, prs]) => (
                <div key={author} className="border border-border/60 rounded-md p-3">
                  <p className="text-xs font-medium text-foreground mb-2">{author}</p>
                  <ul className="flex flex-col gap-1.5">
                    {prs.map((pr) => (
                      <li key={pr.html_url}>
                        <a
                          href={pr.html_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-xs text-muted-foreground hover:text-foreground transition-colors truncate block"
                        >
                          #{pr.number} {pr.title}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="lg:col-span-1 card">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <span className="section-label">Jobs</span>
            <span className="stat-chip">auto-refreshes every 15s</span>
          </div>
          <ActivityList jobs={jobs} isLoading={jobsLoading} />
        </div>
      </div>

      {hasOrg && (
        <div className="grid gap-4 lg:grid-cols-2 mt-4">
          <div className="card lg:col-span-2">
            <div className="px-4 py-3 border-b border-border flex items-center gap-2">
              <span className="section-label">Commit Heatmap (52w)</span>
              {cockpitQuery.data?.commit_activity_source === "aggregate" && (
                <span
                  className="text-[0.6875rem] text-muted-foreground/70"
                  title="Estimated from stored push events, not GitHub's exact commit count."
                >
                  (estimated)
                </span>
              )}
            </div>
            <div className="p-4">
              {(cockpitQuery.data?.commit_heatmap_52w ?? []).some((n) => n > 0) ? (
                <HeatmapCalendar data={cockpitQuery.data!.commit_heatmap_52w} colorScale={HEATMAP_COLOR_SCALE} />
              ) : (
                <p className="text-sm text-muted-foreground">No commit activity in the last year</p>
              )}
            </div>
          </div>

          <div className="card">
            <div className="px-4 py-3 border-b border-border">
              <span className="section-label">CI Failure Log</span>
            </div>
            {(failedRunsQuery.data?.runs.length ?? 0) === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">No repeated CI failures</p>
            ) : (
              <div className="divide-y divide-border">
                {failedRunsQuery.data!.runs.map((r) => (
                  <a
                    key={`${r.repo}-${r.run_id}`}
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between px-4 py-2.5 text-sm hover:bg-elevated transition-colors"
                  >
                    <span className="flex flex-col min-w-0">
                      <span className="text-foreground/90 truncate">{r.repo} · {r.workflow_name}</span>
                      <span className="text-[0.6875rem] text-muted-foreground">{r.branch} · {r.actor}</span>
                    </span>
                    <span className="stat-chip text-red-400 border-red-500/30 shrink-0 ml-2">
                      ×{r.consecutive_failures}
                    </span>
                  </a>
                ))}
              </div>
            )}
          </div>

          <div className="card">
            <div className="px-4 py-3 border-b border-border">
              <span className="section-label">Release Timeline</span>
            </div>
            {(releaseTimelineQuery.data?.releases.length ?? 0) === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">No releases in the last 90 days</p>
            ) : (
              <div className="divide-y divide-border">
                {releaseTimelineQuery.data!.releases.map((r) => (
                  <a
                    key={r.url}
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between px-4 py-2.5 text-sm hover:bg-elevated transition-colors"
                  >
                    <span className="flex flex-col min-w-0">
                      <span className="text-foreground/90 truncate">
                        {r.repo} · {r.name}
                        {r.is_prerelease && <span className="stat-chip ml-1.5 align-middle">pre-release</span>}
                      </span>
                      <span className="text-[0.6875rem] text-muted-foreground truncate">{r.body_preview}</span>
                    </span>
                    <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap shrink-0 ml-2">
                      {relativeTime(r.published_at)}
                    </span>
                  </a>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}

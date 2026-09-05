"use client"

import { useEffect } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { ActivityList } from "@/components/activity-list"
import { EventFeed } from "@/components/event-feed"
import { HeatmapCalendar } from "@/components/charts/heatmap-calendar"
import { SectionError } from "@/components/section-error"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { ArrowRight } from "@phosphor-icons/react"
import { CHART_COLORS } from "@/lib/charts/theme"
import { relativeTime } from "@/lib/format"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"

const EVENTS_REFRESH_SECONDS = 30
const HEATMAP_COLOR_SCALE = [CHART_COLORS.grid, "#1d4ed8", "#3b82f6", "#60a5fa", "#93c5fd"]

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

  return (
    <>
      <PageHeader
        title="Activity"
        description="Recent GitHub activity and background jobs."
        actions={
          <Link
            href="/pulls"
            className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
          >
            Open pull requests
            <ArrowRight className="size-3.5" />
          </Link>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2 card">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
            <span className="section-label">Activity Feed</span>
            {hasOrg && <span className="stat-chip">auto-refreshes every {EVENTS_REFRESH_SECONDS}s</span>}
          </div>
          {!hasOrg ? (
            <EmptyStateNoAccount bare />
          ) : eventsQuery.isError ? (
            <SectionError
              message={eventsQuery.error instanceof Error ? eventsQuery.error.message : "Failed to load events."}
              onRetry={() => eventsQuery.refetch()}
              retrying={eventsQuery.isFetching}
            />
          ) : (
            <EventFeed events={eventsQuery.data?.events ?? []} isLoading={eventsQuery.isLoading} />
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

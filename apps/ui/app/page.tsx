"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { StatCard } from "@/components/stat-card"
import { EventActivityList } from "@/components/event-activity-list"
import { AreaTimeChart } from "@/components/charts/area-time-chart"
import { BarGroupChart } from "@/components/charts/bar-group-chart"
import { ArrowRight, Warning } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import { useAuth } from "@/lib/auth-context"
import { CHART_COLORS } from "@/lib/charts/theme"
import { relativeTime } from "@/lib/format"
import { SectionError } from "@/components/section-error"
import { EmptyStateNoAccount } from "@/components/empty-state"
import type { MyViewIssueSummary, MyViewPRSummary } from "@/lib/api/types"

const MY_VIEW_TABS = [
  { id: "prs", label: "My PRs" },
  { id: "reviews", label: "Review Queue" },
  { id: "issues", label: "Assigned Issues" },
] as const

type MyViewTabId = (typeof MY_VIEW_TABS)[number]["id"]

function MyViewRow({ item }: { item: MyViewPRSummary | MyViewIssueSummary }) {
  return (
    <a
      href={item.html_url}
      target="_blank"
      rel="noreferrer"
      className="flex items-center justify-between px-3 py-2 text-sm hover:bg-elevated transition-colors"
    >
      <span className="flex flex-col min-w-0">
        <span className="text-foreground/90 truncate">{item.title}</span>
        <span className="text-[0.6875rem] text-muted-foreground font-mono">
          {item.repository} #{item.number}
        </span>
      </span>
      <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap shrink-0 ml-3">
        {relativeTime(item.updated_at)}
      </span>
    </a>
  )
}

const quickActions = [
  { label: "Run Security Scan",  href: "/security" },
  { label: "Manage Caches",      href: "/repos" },
  { label: "View Collaborators", href: "/collaborators" },
]

function LiveStatCard({ label, href, loading, configured, value, trend }: {
  label: string
  href: string
  loading: boolean
  configured: boolean
  value: number | undefined
  trend?: number[]
}) {
  if (!configured) {
    return (
      <Link href={href} className="card px-4 py-4 block hover:bg-elevated transition-colors">
        <p className="telemetry-label mb-2 block">
          {label}
        </p>
        <p className="text-lg font-semibold text-foreground leading-none">
          Configure →
        </p>
      </Link>
    )
  }

  return <StatCard label={label} value={loading ? "…" : (value ?? "—")} trend={trend} />
}

export default function OverviewPage() {
  const { scope } = useActiveScope()
  const { user } = useAuth()
  const org = scope?.login ?? ""
  const [orgChecked, setOrgChecked] = useState(false)
  useEffect(() => {
    setOrgChecked(true)
  }, [])

  // Not chained to the cockpit query below — both fire on mount. This one only
  // drives the "not configured yet" CTA-card branch, matching the pre-cockpit
  // behavior, so that empty state doesn't regress.
  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 0,
    retry: false,
  })
  const configured = !!resolveQuery.data?.token

  // Both queries fire as soon as `org` is known (no waterfall) — cockpit just
  // waits for resolveQuery's fetch to settle (not to finish loading data through
  // a dependent chain) so a saved PAT isn't missed on the very first request
  // (queryKey excludes token, so a later-arriving token wouldn't otherwise
  // trigger a refetch of an already-fired query).
  const cockpitQuery = useQuery({
    queryKey: ["analytics.cockpit", org],
    queryFn: () => api.analytics.cockpit(org, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
    refetchInterval: 30_000,
  })
  const cockpit = cockpitQuery.data

  // Issue #294: Actions-minutes usage. Org-scoped + admin-only, and needs a GitHub App
  // permission Clevis doesn't request by default -- so this is best-effort: `retry: false`
  // and the card only renders on a *successful* query (never on stale data).
  //
  // The query key is partitioned by the signed-in user: this is org billing data, and
  // the QueryClient outlives a logout/login on the same tab, so keying on `org` alone
  // would let a member inherit an admin's cached usage (CWE-200). Gate on
  // `resolveQuery.isFetched` so the request always carries the resolved token.
  const isOrgScope = scope?.kind === "org"
  const actionsUsageQuery = useQuery({
    queryKey: ["analytics.actions-usage", org, user?.id ?? "anon"],
    queryFn: () => api.analytics.actionsUsage(org, resolveQuery.data?.token),
    enabled: isOrgScope && org.trim().length > 0 && resolveQuery.isFetched,
    retry: false,
  })
  const usage = actionsUsageQuery.data

  const [myViewTab, setMyViewTab] = useState<MyViewTabId>("prs")
  const myViewQuery = useQuery({
    queryKey: ["analytics.my-view", org],
    queryFn: () => api.analytics.myView(org, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
  })
  const myView = myViewQuery.data

  const commitActivityData = (cockpit?.commit_activity_4w ?? []).map((value, i) => ({
    week: `W${i + 1}`,
    value,
  }))
  const scoreTrendData = (cockpit?.score_trend ?? []).map((value, i) => ({
    week: `Scan ${i + 1}`,
    value,
  }))
  const prMergeRateData = (cockpit?.pr_merge_rate_4w ?? []).map((b) => ({
    name: b.week,
    opened: b.opened,
    merged: b.merged,
  }))
  const cycleTimeData = (cockpit?.pr_cycle_time_8w ?? []).map((b, i) => ({
    week: `W${i + 1}`,
    value: b.avg_days,
  }))
  const releaseCadenceData = (cockpit?.release_cadence_4w ?? []).map((count, i) => ({
    name: `W${i + 1}`,
    releases: count,
  }))
  const atRiskRepos = cockpit?.at_risk_repos ?? []
  const milestones = cockpit?.milestones ?? []
  const myViewItems: (MyViewPRSummary | MyViewIssueSummary)[] =
    myViewTab === "prs" ? myView?.my_open_prs ?? [] : myViewTab === "reviews" ? myView?.review_requests ?? [] : myView?.assigned_issues ?? []

  const orgQueryError = cockpitQuery.error ?? myViewQuery.error
  const orgQueryIsError = cockpitQuery.isError || myViewQuery.isError
  const orgQueryIsFetching = cockpitQuery.isFetching || myViewQuery.isFetching
  const orgQueryErrorMessage =
    orgQueryError instanceof Error ? orgQueryError.message : "Failed to load data for this organization."

  return (
    <>
      <PageHeader title="Overview" description="Your GitHub organization at a glance." />

      {orgChecked && !org && <EmptyStateNoAccount />}

      {org && orgQueryIsError && (
        <div className="card mb-6">
          <SectionError
            message={orgQueryErrorMessage}
            onRetry={() => { cockpitQuery.refetch(); myViewQuery.refetch() }}
            retrying={orgQueryIsFetching}
          />
        </div>
      )}

      {org && !orgQueryIsError && cockpit?.degraded && (
        <div className="card mb-6 px-4 py-3 flex items-center gap-2 text-sm text-warning">
          <Warning size={16} weight="fill" />
          <span>Some data below may be incomplete — a GitHub call failed while loading this page.</span>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-px mb-6 border border-border bg-border">
        <LiveStatCard
          label="Repositories"
          href="/repos"
          loading={cockpitQuery.isLoading}
          configured={configured}
          value={cockpit?.repo_count}
        />
        <LiveStatCard
          label="Open PRs"
          href="/pulls"
          loading={cockpitQuery.isLoading}
          configured={configured}
          value={cockpit?.open_pr_count}
        />
        <LiveStatCard
          label="Security Score"
          href="/security"
          loading={cockpitQuery.isLoading}
          configured={configured}
          value={cockpit?.latest_score ?? undefined}
          trend={cockpit?.score_trend}
        />
        <LiveStatCard
          label="Team Members"
          href="/collaborators"
          loading={cockpitQuery.isLoading}
          configured={configured}
          value={cockpit?.member_count}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2 mb-6">
        <div className="card">
          <div className="px-4 py-3 border-b border-border flex items-center gap-2">
            <span className="section-label">Commit Activity (4w)</span>
            {cockpit?.commit_activity_source === "aggregate" && (
              <span
                className="text-[0.6875rem] text-muted-foreground/70"
                title="Estimated from stored push events, not GitHub's exact commit count."
              >
                (estimated)
              </span>
            )}
          </div>
          <div className="p-4">
            {commitActivityData.length > 0 ? (
              <AreaTimeChart data={commitActivityData} label="Commits" color={CHART_COLORS.primary} height={200} />
            ) : (
              <p className="text-sm text-muted-foreground">No commit activity yet</p>
            )}
          </div>
        </div>

        {actionsUsageQuery.isSuccess && usage && usage.total_minutes_used > 0 && (
          <div className="card">
            <div className="px-4 py-3 border-b border-border">
              <span className="section-label">Actions Usage (this month)</span>
            </div>
            <div className="p-4 flex flex-col gap-2">
              {(() => {
                const used = usage.total_minutes_used || 0
                const included = usage.included_minutes_used || 0
                const paid = usage.paid_minutes_used || 0
                // GitHub's usage API reports consumption, not the plan's monthly
                // allowance — so the bar splits what was used into included vs billed,
                // it isn't a "% of quota" gauge.
                const paidPct = used > 0 ? Math.min(100, Math.round((paid / used) * 100)) : 0
                return (
                  <>
                    <div className="flex items-baseline justify-between text-sm">
                      <span className="tabular-nums font-medium text-foreground">
                        {Math.round(used).toLocaleString()} min
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">used this month</span>
                    </div>
                    {used > 0 && (
                      <div className="h-1.5 bg-muted rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${paidPct >= 20 ? "bg-yellow-400" : "bg-green-400"}`}
                          style={{ width: `${100 - paidPct}%` }}
                        />
                      </div>
                    )}
                    <p className="text-xs text-muted-foreground tabular-nums">
                      {Math.round(included).toLocaleString()} included
                      {paid > 0 && ` · ${Math.round(paid).toLocaleString()} billable`}
                    </p>
                    {Object.keys(usage.minutes_used_breakdown).length > 0 && (
                      <ul className="mt-1 flex flex-col gap-0.5 text-xs text-muted-foreground">
                        {Object.entries(usage.minutes_used_breakdown).map(([sku, mins]) => (
                          <li key={sku} className="flex justify-between tabular-nums">
                            <span>{sku}</span>
                            <span>{Math.round(mins).toLocaleString()}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                )
              })()}
            </div>
          </div>
        )}

        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Security Score</span>
          </div>
          <div className="p-4">
            {scoreTrendData.length > 1 ? (
              <AreaTimeChart data={scoreTrendData} label="Score" color={CHART_COLORS.series[1]} height={200} />
            ) : (
              <p className="text-sm text-muted-foreground">Run more scans to see a trend</p>
            )}
          </div>
        </div>

        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">PR Merge Rate (4w)</span>
          </div>
          <div className="p-4">
            {prMergeRateData.length > 0 ? (
              <BarGroupChart
                data={prMergeRateData}
                bars={[
                  { key: "opened", color: CHART_COLORS.primary },
                  { key: "merged", color: CHART_COLORS.series[1] },
                ]}
                height={200}
              />
            ) : (
              <p className="text-sm text-muted-foreground">No pull request activity yet</p>
            )}
          </div>
        </div>

        <div className="card flex flex-col">
          <div className="px-4 py-3 border-b border-border flex items-center gap-2">
            <span className="section-label">Recent Highlights</span>
            {cockpit?.recent_events_stale && (
              <span
                className="text-[0.6875rem] text-warning"
                title="Activity ingestion hasn't synced recently — this list may be out of date."
              >
                (stale)
              </span>
            )}
          </div>
          <EventActivityList
            events={cockpit?.recent_events ?? []}
            isLoading={cockpitQuery.isLoading}
            limit={5}
          />
          <Link
            href="/activity"
            className="group mt-auto flex items-center justify-center gap-1 border-t border-border px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
          >
            View full activity feed
            <ArrowRight className="size-3.5 shrink-0 transition-transform group-hover:translate-x-0.5" />
          </Link>
        </div>
      </div>

      {atRiskRepos.length > 0 && (
        <div className="card mb-6">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Needs Attention</span>
          </div>
          <div className="divide-y divide-border">
            {atRiskRepos.map((r) => (
              <div
                key={r.repo}
                className={`px-4 py-3 border-l-2 ${r.severity === "critical" ? "border-red-500" : "border-yellow-500"}`}
              >
                <div className="flex items-center gap-2">
                  <Warning className={`size-3.5 shrink-0 ${r.severity === "critical" ? "text-red-400" : "text-yellow-400"}`} />
                  <span className="text-sm font-mono text-foreground/90">{r.repo}</span>
                </div>
                <ul className="mt-1 ml-5 text-xs text-muted-foreground list-disc">
                  {r.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2 mb-6">
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Milestone Burndown</span>
          </div>
          {milestones.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No open milestones</p>
          ) : (
            <div className="divide-y divide-border">
              {milestones.map((m) => (
                <div key={`${m.repo}-${m.title}`} className="px-4 py-3">
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span className="text-sm text-foreground/90 truncate">
                      <span className="font-mono text-muted-foreground">{m.repo}</span> · {m.title}
                    </span>
                    <span
                      className={`stat-chip shrink-0 ${
                        m.state === "overdue"
                          ? "text-red-400 border-red-500/30"
                          : m.state === "at_risk"
                            ? "text-yellow-400 border-yellow-500/30"
                            : ""
                      }`}
                    >
                      {m.state.replace("_", " ")}
                    </span>
                  </div>
                  <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
                    <div
                      className="h-full bg-primary"
                      style={{ width: `${Math.min(100, m.progress_pct)}%` }}
                    />
                  </div>
                  <p className="text-[0.6875rem] text-muted-foreground mt-1">
                    {m.closed_issues}/{m.open_issues + m.closed_issues} closed
                    {m.due_on && <> · due {relativeTime(m.due_on)}</>}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="px-4 py-3 border-b border-border flex items-center gap-1.5">
            {MY_VIEW_TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setMyViewTab(t.id)}
                aria-pressed={myViewTab === t.id}
                className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${
                  myViewTab === t.id
                    ? "border-border bg-elevated text-foreground"
                    : "border-transparent text-muted-foreground hover:bg-elevated"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {myViewQuery.isLoading ? (
            <p className="p-4 text-sm text-muted-foreground">Loading…</p>
          ) : myView?.identity_unresolved ? (
            <p className="p-4 text-sm text-muted-foreground">
              Clevis can’t tell who you are on GitHub for this account — sign in with GitHub
              (or add a personal access token) to see this.
            </p>
          ) : myViewItems.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">Nothing here right now</p>
          ) : (
            <div className="divide-y divide-border">
              {myViewItems.map((item) => <MyViewRow key={`${item.repository}-${item.number}`} item={item} />)}
            </div>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2 mb-6">
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Release Cadence (4w)</span>
          </div>
          <div className="p-4">
            {releaseCadenceData.some((d) => d.releases > 0) ? (
              <BarGroupChart
                data={releaseCadenceData}
                bars={[{ key: "releases", color: CHART_COLORS.series[1] }]}
                height={160}
              />
            ) : (
              <p className="text-sm text-muted-foreground">No releases in the last 4 weeks</p>
            )}
          </div>
        </div>

        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">PR Cycle Time (8w, avg days)</span>
          </div>
          <div className="p-4">
            {cycleTimeData.some((d) => d.value > 0) ? (
              <AreaTimeChart data={cycleTimeData} label="days" color={CHART_COLORS.series[2]} height={160} />
            ) : (
              <p className="text-sm text-muted-foreground">No merged pull requests yet</p>
            )}
          </div>
        </div>
      </div>

      <div className="card mb-6">
        <div className="px-4 py-3 border-b border-border">
          <span className="section-label">Quick Actions</span>
        </div>
        <div className="p-2">
          {quickActions.map((action) => (
            <Link
              key={action.href}
              href={action.href}
              className="flex items-center justify-between px-3 py-2.5 text-sm text-muted-foreground hover:text-foreground hover:bg-elevated transition-colors group"
            >
              {action.label}
              <ArrowRight className="size-3.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
            </Link>
          ))}
        </div>
      </div>
    </>
  )
}

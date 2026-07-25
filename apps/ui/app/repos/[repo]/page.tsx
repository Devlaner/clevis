"use client"

// URL: /repos/<owner~name>. The [repo] folder name is Next.js dynamic segment syntax
// (not a literal path); param holds owner and repo joined with "~" (see repos/page.tsx).

import { useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import Link from "next/link"
import { useMutation, useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { SectionError } from "@/components/section-error"
import { GitPullRequest, ArrowSquareOut, Star, GitFork, Eye, ShieldCheck, ShieldWarning, Shield } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { setActiveScope } from "@/lib/active-scope"
import { parseOwnerRepo } from "@/lib/repo-segment"
import { shouldApplyResolvedToken } from "@/lib/token-resolve"
import { AreaTimeChart } from "@/components/charts/area-time-chart"
import { BarGroupChart } from "@/components/charts/bar-group-chart"
import { HeatmapCalendar } from "@/components/charts/heatmap-calendar"
import { CHART_COLORS } from "@/lib/charts/theme"
import { CachePanel } from "@/components/repo/cache-panel"
import { cn } from "@/lib/utils"

const HEATMAP_SCALE = [CHART_COLORS.grid, "#1e3a8a", "#1d4ed8", "#3b82f6", "#60a5fa"]

type Tab = "overview" | "cache" | "security"

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "cache", label: "Actions Cache" },
  { id: "security", label: "Security" },
]

function tabButtonId(id: Tab) { return `repo-tab-${id}` }
function tabPanelId(id: Tab) { return `repo-tabpanel-${id}` }

type Tone = "good" | "bad" | "unknown"

const TONE_STYLES: Record<Tone, { Icon: typeof ShieldCheck; color: string }> = {
  good: { Icon: ShieldCheck, color: "text-green-400" },
  bad: { Icon: ShieldWarning, color: "text-red-400" },
  unknown: { Icon: Shield, color: "text-muted-foreground" },
}

function SecurityStatusRow({
  label,
  tone,
  goodLabel,
  badLabel,
}: {
  label: string
  tone: Tone
  goodLabel: string
  badLabel: string
}) {
  const { Icon, color } = TONE_STYLES[tone]
  const text = tone === "good" ? goodLabel : tone === "bad" ? badLabel : "Unknown"
  return (
    <div className="flex items-center gap-2 text-xs">
      <Icon className={`size-4 shrink-0 ${color}`} />
      <span className="text-foreground">{label}</span>
      <span className={`ml-auto font-medium ${color}`}>{text}</span>
    </div>
  )
}

export default function RepoDetailPage() {
  const params = useParams<{ repo: string }>()
  const parsed = parseOwnerRepo(params.repo || "")
  const owner = parsed?.owner ?? ""
  const repo = parsed?.repo ?? ""

  const [tab, setTab] = useState<Tab>("overview")
  const [token, setToken] = useState("")

  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ overview: null, cache: null, security: null })

  function focusTab(id: Tab) {
    setTab(id)
    tabRefs.current[id]?.focus()
  }

  // WAI-ARIA tabs pattern: arrow keys move focus + selection between tabs
  // (roving tabindex — only the active tab is in the normal Tab order).
  function handleTabKeyDown(e: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null
    if (e.key === "ArrowRight") nextIndex = (index + 1) % TABS.length
    else if (e.key === "ArrowLeft") nextIndex = (index - 1 + TABS.length) % TABS.length
    else if (e.key === "Home") nextIndex = 0
    else if (e.key === "End") nextIndex = TABS.length - 1
    if (nextIndex !== null) {
      e.preventDefault()
      focusTab(TABS[nextIndex].id)
    }
  }

  const resolveMutation = useMutation({
    mutationFn: (org: string) => api.tokens.resolve(org),
    onSuccess: (data, org) => {
      if (shouldApplyResolvedToken(org, owner)) setToken(data.token)
    },
  })

  useEffect(() => {
    if (owner) {
      setToken("")
      resolveMutation.mutate(owner)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner])

  useEffect(() => {
    setTab("overview")
  }, [params.repo])

  const statsQuery = useQuery({
    queryKey: ["repo-detail-stats", owner, repo, token],
    queryFn: () => api.repos.stats(owner, owner, repo, token),
    enabled: !!owner && !!repo,
  })

  const pullsQuery = useQuery({
    queryKey: ["repo-detail-pulls", owner, repo, token],
    queryFn: () => api.repos.pulls(owner, owner, repo, token),
    enabled: !!owner && !!repo,
  })

  // Per-repo branch-protection/secret-scanning status — still gated on the Security
  // tab actually being opened, so visiting the Overview/Cache tabs never fires it.
  const securityQuery = useQuery({
    queryKey: ["repo-detail-security", owner, repo, token],
    queryFn: () => api.repos.security(owner, owner, repo, token),
    enabled: tab === "security" && !!owner && !!repo,
  })

  if (!parsed) {
    return (
      <>
        <PageHeader title="Repository" description="Invalid repository route." />
        <div className="card px-4 py-6 text-sm text-muted-foreground">
          Expected URL format: <span className="font-mono">/repos/owner~repo</span>
        </div>
      </>
    )
  }

  const commitActivity = statsQuery.data?.commit_activity ?? []
  const areaData = commitActivity.map((w) => ({
    week: new Date(w.week * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
    value: w.total,
  }))
  const heatmapData = commitActivity.map((w) => w.total)
  const topContributors = [...(statsQuery.data?.contributors ?? [])]
    .sort((a, b) => b.total - a.total)
    .slice(0, 8)
    .map((c) => ({ name: c.author?.login ?? "unknown", commits: c.total }))

  return (
    <>
      <PageHeader
        title={repo}
        description={`${owner}/${repo}`}
        actions={
          <span className="inline-flex items-center gap-1.5 stat-chip">
            <GitPullRequest className="size-3.5" />
            {pullsQuery.isLoading ? "…" : (pullsQuery.data?.total ?? 0)} open PRs
          </span>
        }
      />

      <div className="flex items-center gap-1 border-b border-border mb-4" role="tablist">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => { tabRefs.current[t.id] = el }}
            role="tab"
            id={tabButtonId(t.id)}
            aria-controls={tabPanelId(t.id)}
            aria-selected={tab === t.id}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            onKeyDown={(e) => handleTabKeyDown(e, i)}
            className={`px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ${
              tab === t.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        className={cn("grid gap-4 lg:grid-cols-2", tab !== "overview" && "hidden")}
        role="tabpanel"
        id={tabPanelId("overview")}
        aria-labelledby={tabButtonId("overview")}
        tabIndex={0}
      >
        <div className="card lg:col-span-2">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Repository</span>
          </div>
          <div className="p-4">
            {statsQuery.isLoading ? (
              <Skeleton className="h-6 w-full" />
            ) : (
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  <Star className="size-3.5" />
                  {statsQuery.data?.stargazers_count ?? 0} stars
                </span>
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  <GitFork className="size-3.5" />
                  {statsQuery.data?.forks_count ?? 0} forks
                </span>
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  <Eye className="size-3.5" />
                  {statsQuery.data?.watchers_count ?? 0} watchers
                </span>
                <span className="text-muted-foreground">
                  {statsQuery.data?.open_issues_count ?? 0} open issues
                </span>
                <span className="text-muted-foreground">
                  Default branch <span className="font-mono">{statsQuery.data?.default_branch ?? "—"}</span>
                </span>
                <span className="text-muted-foreground">
                  Latest release{" "}
                  {statsQuery.data?.latest_release ? (
                    <span className="font-mono">{statsQuery.data.latest_release.tag_name}</span>
                  ) : (
                    "—"
                  )}
                </span>
              </div>
            )}
          </div>
        </div>

        <div className="card lg:col-span-2">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Commit activity — 52 weeks</span>
          </div>
          <div className="p-4">
            {statsQuery.isLoading ? (
              <Skeleton className="h-60 w-full" />
            ) : areaData.length === 0 ? (
              <p className="text-sm text-muted-foreground font-mono py-8 text-center">
                — no commit activity available yet
              </p>
            ) : (
              <AreaTimeChart data={areaData} label="commits" height={240} />
            )}
          </div>
        </div>

        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Activity calendar</span>
          </div>
          <div className="p-4">
            {statsQuery.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : heatmapData.length === 0 ? (
              <p className="text-sm text-muted-foreground">No data yet</p>
            ) : (
              <HeatmapCalendar data={heatmapData} colorScale={HEATMAP_SCALE} />
            )}
          </div>
        </div>

        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-label">Top contributors</span>
          </div>
          <div className="p-4">
            {statsQuery.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : topContributors.length === 0 ? (
              <p className="text-sm text-muted-foreground">No contributor data yet</p>
            ) : (
              <BarGroupChart
                data={topContributors}
                bars={[{ key: "commits", color: CHART_COLORS.primary }]}
                height={180}
              />
            )}
          </div>
        </div>

        {statsQuery.isError && (
          <p className="text-xs text-destructive lg:col-span-2">{statsQuery.error.message}</p>
        )}
      </div>

      <div
        className={cn(tab !== "cache" && "hidden")}
        role="tabpanel"
        id={tabPanelId("cache")}
        aria-labelledby={tabButtonId("cache")}
        tabIndex={0}
      >
        <CachePanel owner={owner} repo={repo} active={tab === "cache"} />
      </div>

      <div
        className={cn("card", tab !== "security" && "hidden")}
        role="tabpanel"
        id={tabPanelId("security")}
        aria-labelledby={tabButtonId("security")}
        tabIndex={0}
      >
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <span className="section-label">Repository security status</span>
          <Link
            href="/security"
            onClick={() => setActiveScope({ kind: "org", login: owner })}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors inline-flex items-center gap-1"
          >
            Org-wide report <ArrowSquareOut className="size-3" />
          </Link>
        </div>
        <div className="p-4">
          <p className="text-xs text-muted-foreground mb-3">
            Default-branch protection and secret scanning for <span className="font-mono">{owner}/{repo}</span>{" "}
            specifically.
          </p>
          {securityQuery.isLoading ? (
            <Skeleton className="h-16 w-full" />
          ) : securityQuery.isError ? (
            <SectionError
              message={securityQuery.error.message}
              onRetry={() => securityQuery.refetch()}
              retrying={securityQuery.isFetching}
            />
          ) : securityQuery.data ? (
            <div className="flex flex-col gap-2">
              <SecurityStatusRow
                label="Default branch protection"
                tone={
                  securityQuery.data.branch_protection === "protected"
                    ? "good"
                    : securityQuery.data.branch_protection === "unprotected"
                      ? "bad"
                      : "unknown"
                }
                goodLabel="Protected"
                badLabel="Unprotected"
              />
              <SecurityStatusRow
                label="Secret scanning"
                tone={
                  securityQuery.data.secret_scanning === "enabled"
                    ? "good"
                    : securityQuery.data.secret_scanning === "disabled"
                      ? "bad"
                      : "unknown"
                }
                goodLabel="Enabled"
                badLabel="Disabled"
              />
            </div>
          ) : null}
        </div>
      </div>
    </>
  )
}

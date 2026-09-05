"use client"

import { useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { CheckCard } from "@/components/check-card"
import { Skeleton } from "@/components/ui/skeleton"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Warning, Key, ShieldWarning, DownloadSimple } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { toCsv } from "@/lib/csv"
import { downloadTextFile } from "@/lib/download"
import { useActiveScope } from "@/lib/active-scope"
import { shouldApplyResolvedToken } from "@/lib/token-resolve"
import { DonutChart } from "@/components/charts/donut-chart"
import { AreaTimeChart } from "@/components/charts/area-time-chart"
import { BarGroupChart } from "@/components/charts/bar-group-chart"
import { CHART_COLORS } from "@/lib/charts/theme"
import { relativeTime } from "@/lib/format"
import type { CheckResult, InstallationMeta } from "@/lib/api/types"

const TABS = [
  { id: "all", label: "All" },
  { id: "fail", label: "Failed" },
  { id: "severity", label: "By Severity" },
] as const

type TabId = (typeof TABS)[number]["id"]

function ScoreGauge({ score, failed, total }: { score: number; failed: number; total: number }) {
  const r = 38
  const circumference = 2 * Math.PI * r
  const progress = (score / 100) * circumference
  // Use CSS custom property so it respects the theme's primary blue
  const color = score >= 80 ? "#4ade80" : score >= 50 ? "#facc15" : "#f87171"

  return (
    <div className="flex items-center gap-5 px-4 py-4 border-b border-border">
      <div className="relative shrink-0">
        <svg width="96" height="96" className="-rotate-90" aria-label={`Security score: ${score} out of 100`}>
          <circle cx="48" cy="48" r={r} fill="none" stroke="var(--border)" strokeWidth="7" />
          <circle
            cx="48" cy="48" r={r} fill="none"
            stroke={color} strokeWidth="7"
            strokeDasharray={`${progress} ${circumference}`}
            strokeLinecap="round"
            style={{ transition: "stroke-dasharray 0.6s ease" }}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-xl font-bold tabular-nums" style={{ color }}>{score}</span>
        </div>
      </div>
      <div>
        <p className="text-sm font-semibold text-foreground">Security Score</p>
        <div className="flex items-center gap-2 mt-2">
          <span className="stat-chip">{total - failed} passed</span>
          {failed > 0 && <span className="stat-chip text-red-400 border-red-500/30">{failed} failed</span>}
        </div>
      </div>
    </div>
  )
}

const SEVERITY_ORDER = { high: 0, medium: 1, low: 2 }

function sortChecks(checks: CheckResult[]): CheckResult[] {
  return [...checks].sort((a, b) => {
    // Failed before passed
    if (a.status !== b.status) return a.status === "fail" ? -1 : 1
    // Within same status: high → medium → low
    return (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9)
  })
}

export default function SecurityPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const queryClient = useQueryClient()

  const [owner, setOwner] = useState("")
  const [token, setToken] = useState("")
  const [tokenSaved, setTokenSaved] = useState(false)
  const [exportSince, setExportSince] = useState("")
  const [exportUntil, setExportUntil] = useState("")

  const tab = (searchParams.get("tab") ?? "all") as TabId
  const severityFilter = (searchParams.get("severity") ?? "all") as "all" | "high" | "medium" | "low"
  const statusFilter = tab === "fail" ? "fail" : "all"

  function setFilter(key: "tab" | "severity", value: string) {
    const params = new URLSearchParams(searchParams.toString())
    if (value === "all") params.delete(key)
    else params.set(key, value)
    router.replace(`?${params.toString()}`, { scroll: false })
  }

  const { scope } = useActiveScope()
  const scopeOrgLogin = scope?.kind === "org" ? scope.login : ""
  // Security scanning is org-only (the backend 422s for a personal account) — pre-fill
  // from an org scope, and clear (not just skip) when switching to personal so a stale
  // org doesn't linger.
  useEffect(() => {
    setOwner(scopeOrgLogin)
  }, [scopeOrgLogin])

  // Same pattern as the other pages that gate on useActiveScope() (see app/page.tsx,
  // app/repos/page.tsx, etc.) -- deferred a tick so this doesn't flash before
  // useActiveScope's first read of localStorage resolves.
  const [scopeChecked, setScopeChecked] = useState(false)
  useEffect(() => {
    setScopeChecked(true)
  }, [])

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

  const historyQuery = useQuery({
    queryKey: ["analytics.history", owner],
    queryFn: () => api.analytics.history(owner),
    enabled: owner.trim().length > 2,
  })

  const scan = useMutation({
    mutationFn: () => api.analytics.overview(owner, token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["analytics.history", owner] })
    },
  })

  // Compliance export (issue #293): pull the full scan history with per-check
  // detail and hand the auditor a CSV. One row per check per scan (long format);
  // scans that stored no per-check breakdown still contribute one summary row.
  const exportCsv = useMutation({
    mutationFn: () => api.analytics.exportHistory(owner.trim(), exportSince || undefined, exportUntil || undefined),
    onSuccess: (res) => {
      const flat = res.entries.flatMap((scanRow) => {
        const checks = scanRow.checks ?? []
        return (checks.length ? checks : [null]).map((check) => ({ scanRow, check }))
      })
      const csv = toCsv(flat, [
        { header: "scanned_at", value: ({ scanRow }) => scanRow.created_at },
        { header: "owner", value: ({ scanRow }) => scanRow.owner },
        { header: "score", value: ({ scanRow }) => scanRow.score },
        { header: "total_checks", value: ({ scanRow }) => scanRow.total_checks },
        { header: "failed_checks", value: ({ scanRow }) => scanRow.failed_checks },
        { header: "check_id", value: ({ check }) => check?.id ?? "" },
        { header: "check_title", value: ({ check }) => check?.title ?? "" },
        { header: "severity", value: ({ check }) => check?.severity ?? "" },
        { header: "status", value: ({ check }) => check?.status ?? "" },
      ])
      const stamp = new Date().toISOString().slice(0, 10)
      downloadTextFile(`clevis-scan-history-${owner.trim()}-${stamp}.csv`, csv, "text/csv")
    },
  })

  const [selectedRepo, setSelectedRepo] = useState("")
  const matrixMutation = useMutation({
    mutationFn: () => api.security.matrix(owner, token),
    onSuccess: (data) => setSelectedRepo(data.repos[0]?.repo ?? ""),
  })

  const secretScanning = useQuery({
    queryKey: ["security.secret-scanning", owner, selectedRepo],
    queryFn: () => api.security.secretScanning(owner, selectedRepo, token),
    enabled: !!selectedRepo && !!matrixMutation.data,
  })

  const trendData = (historyQuery.data ?? [])
    .slice(0, 10)
    .reverse()
    .map((h) => ({
      week: new Date(h.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      value: h.score,
    }))

  // "Remediation trend": how many of a scan's checks were passing at the time, over
  // the last N scans -- an approximation of dependabot/code-scanning remediation
  // progress using the data already captured per historical scan (ScanHistoryEntry
  // doesn't expose the full checks_json breakdown via this endpoint), rather than a
  // literal sum of historical dependabot critical+high counts.
  const remediationTrendData = (historyQuery.data ?? [])
    .slice(0, 10)
    .reverse()
    .map((h) => ({
      week: new Date(h.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      value: h.total_checks - h.failed_checks,
    }))

  const vulnByRepoData = (matrixMutation.data?.repos ?? []).map((r) => ({
    name: r.repo,
    critical: r.dependabot_critical_count,
    high: r.dependabot_high_count,
  }))

  function runScan() {
    scan.mutate()
    matrixMutation.mutate()
  }

  const filteredChecks = scan.data
    ? sortChecks(
        scan.data.checks.filter((c) => {
          if (statusFilter !== "all" && c.status !== statusFilter) return false
          if (tab === "severity" && severityFilter !== "all" && c.severity !== severityFilter) return false
          return true
        }),
      )
    : []

  const allChecks = scan.data?.checks ?? []
  const statusBreakdown = [
    { name: "Passed", value: allChecks.filter((c) => c.status === "pass").length, color: "#34d399" },
    { name: "Failed · high", value: allChecks.filter((c) => c.status === "fail" && c.severity === "high").length, color: "#f87171" },
    { name: "Failed · med/low", value: allChecks.filter((c) => c.status === "fail" && c.severity !== "high").length, color: "#fbbf24" },
  ].filter((d) => d.value > 0)

  return (
    <>
      <PageHeader
        title="Health & Security"
        description="Run security checks against a GitHub organization."
      />

      {scopeChecked && !scope && (
        <EmptyStateNoAccount message="No account selected — pick an organization from the profile menu to prefill the scan below, or just type one in directly." />
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Config */}
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-title">Scan configuration</span>
          </div>
          <div className="p-4 flex flex-col gap-3">
            <div>
              <label className="text-xs font-medium text-foreground block mb-1.5">Organization</label>
              <Input
                placeholder="e.g. octocat"
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && owner && !scan.isPending && runScan()}
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
                  onKeyDown={(e) => e.key === "Enter" && owner && !scan.isPending && runScan()}
                />
              </div>
            )}
            <Button
              onClick={() => runScan()}
              disabled={scan.isPending || !owner}
              className="mt-1"
            >
              {scan.isPending ? "Scanning…" : "Run scan"}
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
            {scan.isError && (
              <div data-testid="scan-error" className="flex items-start gap-2 text-xs text-destructive">
                <Warning className="size-3.5 mt-0.5 shrink-0" />
                {scan.error.message}
              </div>
            )}
            {(historyQuery.data?.length ?? 0) > 0 && (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input
                    type="date"
                    aria-label="Export from date"
                    value={exportSince}
                    max={exportUntil || undefined}
                    onChange={(e) => setExportSince(e.target.value)}
                    className="card px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                  />
                  <span>–</span>
                  <input
                    type="date"
                    aria-label="Export to date"
                    value={exportUntil}
                    min={exportSince || undefined}
                    onChange={(e) => setExportUntil(e.target.value)}
                    className="card px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                  />
                </div>
                <Button
                  variant="outline"
                  onClick={() => exportCsv.mutate()}
                  disabled={exportCsv.isPending}
                >
                  <DownloadSimple className="size-3.5" />
                  {exportCsv.isPending ? "Exporting…" : "Export history (CSV)"}
                </Button>
              </div>
            )}
            {exportCsv.isError && (
              <div className="flex items-start gap-2 text-xs text-destructive">
                <Warning className="size-3.5 mt-0.5 shrink-0" />
                {exportCsv.error.message}
              </div>
            )}
            {exportCsv.data?.truncated && (
              <div className="flex items-start gap-2 text-xs text-amber-500">
                <Warning className="size-3.5 mt-0.5 shrink-0" />
                Export capped at {exportCsv.data.row_count} scans — narrow the date range for a complete
                period.
              </div>
            )}
          </div>
        </div>

        {/* Results */}
        {(scan.data || scan.isPending) && (
          <div className="card lg:col-span-2">
            {scan.data && (
              <div className="px-4 py-3 border-b border-border">
                <span className="section-title">Results — {scan.data.owner}</span>
              </div>
            )}

            {/* Gauge + donut side-by-side */}
            {scan.data && (
              <div className="grid sm:grid-cols-2 border-b border-border">
                <ScoreGauge
                  score={scan.data.score}
                  failed={scan.data.failed_checks}
                  total={scan.data.total_checks}
                />
                {statusBreakdown.length > 0 && (
                  <div className="px-4 py-4 sm:border-l border-border">
                    <DonutChart data={statusBreakdown} label="checks" height={180} />
                  </div>
                )}
              </div>
            )}

            {/* Score trend */}
            {trendData.length > 1 && (
              <div className="px-4 py-4 border-b border-border">
                <span className="text-xs font-medium text-muted-foreground block mb-2">
                  Score trend (last {trendData.length} scans)
                </span>
                <AreaTimeChart data={trendData} label="score" height={140} />
              </div>
            )}

            {/* Tabs */}
            {scan.data && (
              <div className="px-4 py-2.5 border-b border-border flex items-center gap-3 flex-wrap">
                <div className="flex items-center gap-1.5">
                  {TABS.map((t) => {
                    const active = tab === t.id
                    return (
                      <button
                        key={t.id}
                        onClick={() => setFilter("tab", t.id)}
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
                {tab === "severity" && (
                  <select
                    value={severityFilter}
                    onChange={(e) => setFilter("severity", e.target.value)}
                    className="text-xs card text-muted-foreground px-2 py-1 focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    <option value="all">All severities</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                  </select>
                )}
                <span className="text-xs text-muted-foreground ml-auto">
                  {filteredChecks.length} of {scan.data.checks.length}
                </span>
              </div>
            )}

            <div className="p-4 grid gap-3 sm:grid-cols-2">
              {scan.isPending ? (
                /* Skeleton while scanning */
                Array.from({ length: 3 }).map((_, i) => (
                  <div
                    key={i}
                    className="bg-card border border-border/40 rounded-md p-3.5 flex items-start gap-3 animate-pulse"
                  >
                    <Skeleton className="size-4 shrink-0 mt-0.5 rounded-full" />
                    <div className="flex-1 space-y-2">
                      <Skeleton className="h-3 w-3/4" />
                      <Skeleton className="h-2.5 w-full" />
                      <Skeleton className="h-2.5 w-1/2" />
                    </div>
                  </div>
                ))
              ) : filteredChecks.length === 0 ? (
                <p className="text-sm text-muted-foreground sm:col-span-2">
                  No checks match the current filter
                </p>
              ) : (
                filteredChecks.map((c: CheckResult) => (
                  <CheckCard key={c.id} check={c} owner={scan.data.owner} token={token} onRemediated={() => runScan()} />
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {(matrixMutation.data || matrixMutation.isPending || matrixMutation.error) && (
        <div className="grid gap-4 lg:grid-cols-2 mt-6">
          <div className="card">
            <div className="px-4 py-3 border-b border-border flex items-center justify-between">
              <span className="flex items-center gap-2">
                <span className="section-title">Compliance Matrix</span>
                {matrixMutation.data?.repos.some((r) => r.alerts_source === "aggregate") && (
                  <span
                    className="text-[0.6875rem] text-muted-foreground/70"
                    title="Dependabot/code-scanning columns are from ingested webhook events, not a live GitHub call."
                  >
                    (estimated)
                  </span>
                )}
              </span>
              {matrixMutation.data && (
                <span className="stat-chip">{matrixMutation.data.summary.fully_compliant_count} fully compliant</span>
              )}
            </div>
            {matrixMutation.isPending ? (
              <div className="p-4"><Skeleton className="h-32 w-full" /></div>
            ) : matrixMutation.error ? (
              <p className="p-4 text-sm text-destructive">Compliance matrix unavailable: {matrixMutation.error.message}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left text-muted-foreground font-medium px-4 py-2">Repo</th>
                      <th className="text-center text-muted-foreground font-medium px-2 py-2">Branch</th>
                      <th className="text-center text-muted-foreground font-medium px-2 py-2">Secrets</th>
                      <th className="text-center text-muted-foreground font-medium px-2 py-2">Dependabot</th>
                      <th className="text-center text-muted-foreground font-medium px-2 py-2">Code scan</th>
                      <th className="text-center text-muted-foreground font-medium px-2 py-2">Force-push</th>
                      <th className="text-right text-muted-foreground font-medium px-4 py-2">Score</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {(matrixMutation.data?.repos ?? []).map((r) => (
                      <tr
                        key={r.repo}
                        className={`hover:bg-elevated transition-colors cursor-pointer ${selectedRepo === r.repo ? "bg-elevated" : ""}`}
                        onClick={() => setSelectedRepo(r.repo)}
                      >
                        <td className="px-4 py-2 font-mono text-foreground/90 truncate max-w-[10rem]">{r.repo}</td>
                        <td className="text-center px-2 py-2" title={r.unknown_dimensions.includes("branch_protection") ? "unknown — token can't see this" : undefined}>
                          {r.unknown_dimensions.includes("branch_protection") ? "?" : r.branch_protection ? "✓" : "—"}
                        </td>
                        <td className="text-center px-2 py-2">{r.secret_scanning ? "✓" : "—"}</td>
                        <td className="text-center px-2 py-2" title={r.unknown_dimensions.includes("dependabot") ? "unknown — token can't see this" : undefined}>
                          {r.unknown_dimensions.includes("dependabot") ? (
                            "?"
                          ) : r.dependabot_critical_count + r.dependabot_high_count > 0 ? (
                            <span className="text-red-400">{r.dependabot_critical_count + r.dependabot_high_count}</span>
                          ) : "✓"}
                        </td>
                        <td className="text-center px-2 py-2" title={r.unknown_dimensions.includes("code_scanning") ? "unknown — token can't see this" : undefined}>
                          {r.unknown_dimensions.includes("code_scanning") ? "?" : r.code_scanning ? "✓" : "—"}
                        </td>
                        <td className="text-center px-2 py-2" title={r.unknown_dimensions.includes("force_push") ? "unknown — token can't see this" : undefined}>
                          {r.unknown_dimensions.includes("force_push") ? "?" : r.force_push_allowed ? "✗" : "✓"}
                        </td>
                        <td className="text-right px-4 py-2 tabular-nums">{r.score}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <div className="px-4 py-3 border-b border-border">
              <span className="section-title">Vulnerabilities by Repo</span>
            </div>
            <div className="p-4">
              {vulnByRepoData.some((d) => d.critical + d.high > 0) ? (
                <BarGroupChart
                  data={vulnByRepoData}
                  bars={[
                    { key: "critical", color: "#f87171" },
                    { key: "high", color: CHART_COLORS.series[5] },
                  ]}
                  height={200}
                />
              ) : (
                <p className="text-sm text-muted-foreground">No open critical/high Dependabot alerts</p>
              )}
            </div>
          </div>

          <div className="card">
            <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
              <span className="flex items-center gap-2">
                <span className="section-title">Secret Scanning Alerts</span>
                {secretScanning.data?.source === "aggregate" && (
                  <span
                    className="text-[0.6875rem] text-muted-foreground/70"
                    title="From ingested webhook events, not a live GitHub call."
                  >
                    (estimated)
                  </span>
                )}
              </span>
              {matrixMutation.data && matrixMutation.data.repos.length > 0 && (
                <select
                  value={selectedRepo}
                  onChange={(e) => setSelectedRepo(e.target.value)}
                  className="text-xs card text-muted-foreground px-2 py-1 focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {matrixMutation.data.repos.map((r) => <option key={r.repo} value={r.repo}>{r.repo}</option>)}
                </select>
              )}
            </div>
            <p className="px-4 pt-3 text-[0.6875rem] text-muted-foreground flex items-center gap-1.5">
              <ShieldWarning className="size-3 shrink-0" />
              Secret values are never shown here — metadata only.
            </p>
            {secretScanning.isLoading ? (
              <div className="p-4"><Skeleton className="h-16 w-full" /></div>
            ) : secretScanning.isError ? (
              <p className="p-4 text-sm text-destructive">{secretScanning.error.message}</p>
            ) : (secretScanning.data?.alerts.length ?? 0) === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">No secret scanning alerts for this repo</p>
            ) : (
              <div className="divide-y divide-border">
                {secretScanning.data!.alerts.map((a) => {
                  const row = (
                    <>
                      <span className="flex flex-col">
                        <span className="text-foreground/90">{a.secret_type_display}</span>
                        <span className="text-[0.6875rem] text-muted-foreground">#{a.number}</span>
                      </span>
                      <span
                        className={`stat-chip ${a.state === "open" ? "text-red-400 border-red-500/30" : ""}`}
                      >
                        {a.state}
                      </span>
                    </>
                  )
                  const rowClassName = "flex items-center justify-between px-4 py-2.5 text-sm hover:bg-elevated transition-colors"
                  return a.url ? (
                    <a key={a.number} href={a.url} target="_blank" rel="noreferrer" className={rowClassName}>
                      {row}
                    </a>
                  ) : (
                    <div key={a.number} className={rowClassName}>
                      {row}
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="card">
            <div className="px-4 py-3 border-b border-border">
              <span className="section-title">Remediation Trend</span>
            </div>
            <div className="p-4">
              {remediationTrendData.length > 1 ? (
                <AreaTimeChart data={remediationTrendData} label="checks passing" color={CHART_COLORS.series[1]} height={180} />
              ) : (
                <p className="text-sm text-muted-foreground">Run more scans to see a trend</p>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

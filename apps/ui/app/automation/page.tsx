"use client"

import { useEffect, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { PageHeader } from "@/components/page-header"
import { Warning, Key, CircleNotch, Play, CheckCircle, XCircle, CircleDashed } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import { shouldApplyResolvedToken } from "@/lib/token-resolve"
import { BarGroupChart } from "@/components/charts/bar-group-chart"
import { CHART_COLORS } from "@/lib/charts/theme"
import { relativeTime } from "@/lib/format"
import type { InstallationMeta, RunSummary, WorkflowSummary } from "@/lib/api/types"

function runDurationSeconds(run: RunSummary): number | null {
  return run.duration_ms == null ? null : Math.round(run.duration_ms / 1000)
}

function StatusIcon({ status, conclusion }: { status: string; conclusion: string | null }) {
  if (status !== "completed") {
    return <CircleDashed className="size-3.5 text-yellow-400 animate-pulse" />
  }
  if (conclusion === "success") return <CheckCircle className="size-3.5 text-green-400" />
  if (conclusion === "failure") return <XCircle className="size-3.5 text-red-400" />
  return <CircleDashed className="size-3.5 text-muted-foreground" />
}

export default function AutomationPage() {
  const [owner, setOwner] = useState("")
  const [repo, setRepo] = useState("")
  const [token, setToken] = useState("")
  const [tokenSaved, setTokenSaved] = useState(false)

  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowSummary | null>(null)
  const [ref, setRef] = useState("main")
  const [dispatchArmed, setDispatchArmed] = useState(false)

  const { scope } = useActiveScope()
  const scopeLogin = scope?.login ?? ""
  useEffect(() => {
    if (scopeLogin) setOwner(scopeLogin)
  }, [scopeLogin])

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
    // > 0, not > 2 -- valid GitHub org logins can be 1-2 characters (see activity/page.tsx).
    if (owner.trim().length > 0) resolveMutation.mutate(owner.trim())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner])

  const saveTokenMutation = useMutation({
    mutationFn: () => api.tokens.upsert(owner.trim(), token.trim()),
    onSuccess: () => setTokenSaved(true),
  })

  const loadMutation = useMutation({
    mutationFn: async () => {
      const [workflows, runs] = await Promise.all([
        api.automation.workflows(owner.trim(), repo.trim(), token),
        api.automation.runs(owner.trim(), repo.trim(), token),
      ])
      return { workflows, runs }
    },
    onSuccess: () => {
      setSelectedWorkflow(null)
      setDispatchArmed(false)
    },
  })

  const dispatchMutation = useMutation({
    mutationFn: () => {
      if (!selectedWorkflow) throw new Error("No workflow selected")
      return api.automation.dispatch(owner.trim(), repo.trim(), selectedWorkflow.id, { token, ref: ref.trim() })
    },
    onSuccess: () => setDispatchArmed(false),
  })

  // Auto-disarm if the user doesn't confirm within a few seconds — same pattern
  // as the Actions Cache "Clear" button (see components/repo/cache-panel.tsx).
  useEffect(() => {
    if (!dispatchArmed) return
    const timer = setTimeout(() => setDispatchArmed(false), 4000)
    return () => clearTimeout(timer)
  }, [dispatchArmed])

  const isLoading = loadMutation.isPending
  const workflows = loadMutation.data?.workflows.workflows ?? []
  const runs = loadMutation.data?.runs.runs ?? []

  const durationChartData = runs
    .filter((r) => r.duration_ms != null)
    .slice(0, 10)
    .reverse()
    .map((r) => ({ name: r.head_branch || `#${r.id}`, seconds: runDurationSeconds(r) ?? 0 }))

  return (
    <>
      <PageHeader
        title="Automation"
        description="Trigger GitHub Actions workflows and review run history — dispatch is audit-logged and requires org admin."
      />

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Config panel */}
        <div className="card">
          <div className="px-4 py-3 border-b border-border">
            <span className="section-title">Repository</span>
          </div>
          <div className="p-4 flex flex-col gap-3">
            <div>
              <label className="text-xs font-medium text-foreground block mb-1.5">Organization / Owner</label>
              <Input placeholder="e.g. octocat" value={owner} onChange={(e) => setOwner(e.target.value)} />
            </div>
            <div>
              <label className="text-xs font-medium text-foreground block mb-1.5">Repository</label>
              <Input
                placeholder="e.g. hello-world"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && owner.trim() && repo.trim() && !isLoading && loadMutation.mutate()}
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
                />
              </div>
            )}
            {!tokenSaved && token && owner && (
              <Button variant="outline" onClick={() => saveTokenMutation.mutate()} disabled={saveTokenMutation.isPending}>
                <Key className="size-3.5" />
                {saveTokenMutation.isPending ? "Saving…" : "Save token for this org"}
              </Button>
            )}
            <Button onClick={() => loadMutation.mutate()} disabled={isLoading || !owner.trim() || !repo.trim()} className="mt-1">
              {isLoading ? <><CircleNotch className="size-3.5 animate-spin" />Loading…</> : "Load workflows"}
            </Button>
            {loadMutation.isError && (
              <p className="text-xs text-destructive flex items-center gap-1.5">
                <Warning className="size-3 shrink-0" />
                {loadMutation.error.message}
              </p>
            )}

            {selectedWorkflow && (
              <div className="mt-2 pt-3 border-t border-border flex flex-col gap-2.5">
                <p className="text-xs font-medium text-foreground">Dispatch &ldquo;{selectedWorkflow.name}&rdquo;</p>
                <div>
                  <label className="text-xs font-medium text-foreground block mb-1.5">Ref (branch/tag)</label>
                  <Input value={ref} onChange={(e) => { setRef(e.target.value); setDispatchArmed(false) }} />
                </div>
                <Button
                  onClick={() => {
                    if (dispatchArmed) {
                      setDispatchArmed(false)
                      dispatchMutation.mutate()
                    } else {
                      setDispatchArmed(true)
                    }
                  }}
                  disabled={dispatchMutation.isPending || !ref.trim()}
                >
                  <Play className="size-3.5" />
                  {dispatchArmed ? "Confirm dispatch" : "Dispatch workflow"}
                </Button>
                {dispatchArmed && (
                  <p className="text-xs text-yellow-400/80 flex items-center gap-1.5">
                    <Warning className="size-3 shrink-0" />
                    Click again to trigger this workflow run on GitHub.
                  </p>
                )}
                {dispatchMutation.isError && (
                  <p className="text-xs text-destructive flex items-center gap-1.5">
                    <Warning className="size-3 shrink-0" />
                    {dispatchMutation.error.message}
                  </p>
                )}
                {dispatchMutation.isSuccess && (
                  <p className="text-xs text-green-400">Workflow dispatched — check run history shortly.</p>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Results */}
        {(loadMutation.data || isLoading) && (
          <div className="card lg:col-span-2">
            <>
              <div className="px-4 py-3 border-b border-border flex items-center justify-between">
                <span className="section-title">Workflows</span>
                {workflows.length > 0 && <span className="stat-chip">{workflows.length} total</span>}
              </div>
              {isLoading ? (
                <div className="p-4 flex flex-col gap-2">
                  {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
                </div>
              ) : workflows.length === 0 ? (
                <div className="px-4 py-8">
                  <p className="text-sm text-muted-foreground">No workflows found in this repository.</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border">
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">Workflow</th>
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">State</th>
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">Last run</th>
                        <th className="px-4 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {workflows.map((w) => (
                        <tr key={w.id} className="hover:bg-muted/40 transition-colors">
                          <td className="px-4 py-2.5 font-mono text-foreground/90">{w.name}</td>
                          <td className="px-4 py-2.5 text-muted-foreground">{w.state}</td>
                          <td className="px-4 py-2.5">
                            {w.last_run_status ? (
                              <span className="inline-flex items-center gap-1.5">
                                <StatusIcon status={w.last_run_status} conclusion={w.last_run_conclusion} />
                                <span className="text-muted-foreground">
                                  {w.last_run_at ? relativeTime(w.last_run_at) : w.last_run_status}
                                </span>
                              </span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </td>
                          <td className="px-4 py-2.5 text-right">
                            <Button
                              variant="outline"
                              className="h-6 px-2 text-[0.6875rem]"
                              onClick={() => { setSelectedWorkflow(w); setDispatchArmed(false) }}
                            >
                              <Play className="size-3" />
                              Dispatch
                            </Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {durationChartData.length > 0 && (
                <div className="p-4 border-t border-border">
                  <p className="section-label mb-3">Run duration (seconds, recent runs)</p>
                  <BarGroupChart
                    data={durationChartData}
                    bars={[{ key: "seconds", color: CHART_COLORS.primary }]}
                    height={180}
                  />
                </div>
              )}

              {runs.length > 0 && (
                <div className="border-t border-border overflow-x-auto">
                  <div className="px-4 py-3 border-b border-border">
                    <span className="section-title">Run history</span>
                  </div>
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border">
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">Run</th>
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">Branch</th>
                        <th className="text-left text-muted-foreground font-medium px-4 py-2">Status</th>
                        <th className="text-right text-muted-foreground font-medium px-4 py-2">Created</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {runs.map((r) => (
                        <tr key={r.id} className="hover:bg-muted/40 transition-colors">
                          <td className="px-4 py-2.5 font-mono text-foreground/80">{r.name ?? `#${r.id}`}</td>
                          <td className="px-4 py-2.5 text-muted-foreground">{r.head_branch}</td>
                          <td className="px-4 py-2.5">
                            <span className="inline-flex items-center gap-1.5">
                              <StatusIcon status={r.status} conclusion={r.conclusion} />
                              <span className="text-muted-foreground">{r.conclusion ?? r.status}</span>
                            </span>
                          </td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground whitespace-nowrap">
                            {relativeTime(r.created_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          </div>
        )}
      </div>
    </>
  )
}

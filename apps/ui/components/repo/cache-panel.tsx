"use client"

import { useEffect, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import Link from "next/link"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Warning, Eye, Key, CircleNotch, Trash } from "@phosphor-icons/react"
import { api } from "@/lib/api/client"
import { shouldApplyResolvedToken } from "@/lib/token-resolve"
import { BarGroupChart } from "@/components/charts/bar-group-chart"
import { CHART_COLORS } from "@/lib/charts/theme"
import { formatBytes, relativeTime, classifyStaleness, stalenessColor } from "@/lib/format"
import type { CacheEntry } from "@/lib/api/types"

interface CachePanelProps {
  owner: string
  repo: string
  // The repo detail page keeps this panel mounted behind all three tabs (fixes a
  // dangling aria-controls reference — see repos/[repo]/page.tsx), so without this
  // flag it would auto-resolve a token on every page load even if the user never
  // opens the Actions Cache tab. Defaults to true for the standalone /cache route,
  // which has no tabs and is always "active".
  active?: boolean
}

/** Actions-cache list/clear UI — the standalone /repos/{repo}/cache route and the
 * repo detail page's "Actions Cache" tab both render this. */
export function CachePanel({ owner, repo, active = true }: CachePanelProps) {
  const [token, setToken] = useState("")
  const [tokenSaved, setTokenSaved] = useState(false)
  const [actor, setActor] = useState("")
  const [clearArmed, setClearArmed] = useState(false)
  // null = clearing every cache for this repo (the existing global buttons); set to a
  // specific { key, ref } when the user clicks a row's own "Clear" action instead.
  const [clearTarget, setClearTarget] = useState<{ key: string; ref: string } | null>(null)

  // Auto-resolve saved token for this owner
  const resolveMutation = useMutation({
    mutationFn: (org: string) => api.tokens.resolve(org),
    onSuccess: (data, org) => {
      if (shouldApplyResolvedToken(org, owner)) {
        setToken(data.token)
        setTokenSaved(true)
      }
    },
    onError: () => setTokenSaved(false),
  })

  // Resolve at most once per owner, deferred until the panel is actually active (so
  // opening the repo detail page never resolves a token for a tab the user hasn't
  // clicked into yet) but NOT re-triggered every time the tab is revisited — otherwise
  // switching tabs and back would wipe out whatever the user had typed in the meantime.
  const resolvedForOwnerRef = useRef(false)

  useEffect(() => {
    resolvedForOwnerRef.current = false
    if (owner) {
      setToken("")
      setTokenSaved(false)
    }
  }, [owner])

  useEffect(() => {
    if (owner && active && !resolvedForOwnerRef.current) {
      resolvedForOwnerRef.current = true
      resolveMutation.mutate(owner)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner, active])

  // Reset stale cache-table/clear-result data and any armed "Confirm clear" state
  // whenever the owner/repo this panel is scoped to changes (route navigation, or a
  // tab switch back to a differently-scoped repo).
  useEffect(() => {
    listMutation.reset()
    clearMutation.reset()
    setClearArmed(false)
    setClearTarget(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner, repo])

  // Auto-disarm if the user doesn't confirm within a few seconds, so a stray
  // later click on the same button spot can't be mistaken for a confirmation.
  useEffect(() => {
    if (!clearArmed) return
    const timer = setTimeout(() => setClearArmed(false), 4000)
    return () => clearTimeout(timer)
  }, [clearArmed])


  const saveTokenMutation = useMutation({
    mutationFn: () => api.tokens.upsert(owner, token.trim()),
    onSuccess: () => setTokenSaved(true),
  })

  const listMutation = useMutation({
    mutationFn: () => api.cache.list(owner, repo, token),
  })

  const clearMutation = useMutation({
    mutationFn: (dryRun: boolean) =>
      api.cache.clear(owner, repo, {
        token,
        actor,
        dry_run: dryRun,
        key: clearTarget?.key,
        ref: clearTarget?.ref,
      }),
  })

  const isLoading = listMutation.isPending || clearMutation.isPending
  const caches: CacheEntry[] = listMutation.data?.actions_caches ?? []
  const totalBytes = caches.reduce((sum, c) => sum + c.size_in_bytes, 0)

  // Total cache size per ref, in MB, for the summary bar chart above the table.
  const cacheByRef = caches.reduce<Record<string, number>>((acc, c) => {
    acc[c.ref] = (acc[c.ref] ?? 0) + c.size_in_bytes
    return acc
  }, {})
  const cacheChartData = Object.entries(cacheByRef).map(([ref, bytes]) => ({
    name: ref,
    mb: Math.round((bytes / 1_048_576) * 100) / 100,
  }))

  return (
    <>
    <div className="grid gap-4 lg:grid-cols-3">
      {/* Config panel */}
      <div className="card">
        <div className="px-4 py-3 border-b border-border">
          <span className="section-label">Configuration</span>
        </div>
        <div className="p-4 flex flex-col gap-3">
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
          {!tokenSaved && token && (
            <Button
              variant="outline"
              onClick={() => saveTokenMutation.mutate()}
              disabled={saveTokenMutation.isPending}
              className="w-full"
            >
              <Key className="size-3.5" />
              {saveTokenMutation.isPending ? "Saving…" : "Save token for this org"}
            </Button>
          )}
          {saveTokenMutation.isError && (
            <p className="text-xs text-destructive flex items-center gap-1.5">
              <Warning className="size-3 shrink-0" />
              {saveTokenMutation.error.message}
            </p>
          )}
          <div>
            <label className="text-xs font-medium text-foreground block mb-1.5">Actor</label>
            <Input
              placeholder="actor"
              value={actor}
              onChange={(e) => { setActor(e.target.value); setClearArmed(false) }}
            />
          </div>
          <Button
            onClick={() => { setClearArmed(false); listMutation.mutate() }}
            disabled={isLoading}
            className="mt-1"
          >
            {listMutation.isPending ? (
              <><CircleNotch className="size-3.5 animate-spin" />Loading…</>
            ) : (
              "Load caches"
            )}
          </Button>
          {listMutation.isError && (
            <p className="text-xs text-destructive flex items-center gap-1.5">
              <Warning className="size-3 shrink-0" />
              {listMutation.error.message}
            </p>
          )}
          <div className="grid grid-cols-2 gap-2">
            <Button
              variant="outline"
              onClick={() => { setClearArmed(false); setClearTarget(null); clearMutation.mutate(true) }}
              disabled={isLoading || !actor}
            >
              <Eye className="size-3.5" />
              Dry run
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (clearArmed && clearTarget === null) {
                  setClearArmed(false)
                  clearMutation.mutate(false)
                } else {
                  setClearTarget(null)
                  setClearArmed(true)
                }
              }}
              disabled={isLoading || !actor}
            >
              <Trash className="size-3.5" />
              {clearArmed && clearTarget === null ? "Confirm clear" : "Clear"}
            </Button>
          </div>
          {clearArmed && (
            <p className="text-xs text-yellow-400/80 flex items-center gap-1.5">
              <Warning className="size-3 shrink-0" />
              {clearTarget
                ? <>Click the row&rsquo;s confirm action again to permanently delete <span className="font-mono">{clearTarget.key}</span> — this can&rsquo;t be undone.</>
                : "Click again to permanently delete these caches — this can’t be undone."}
            </p>
          )}
          {clearMutation.isError && (
            <p className="text-xs text-destructive flex items-center gap-1.5">
              <Warning className="size-3 shrink-0" />
              {clearMutation.error.message}
            </p>
          )}
        </div>
      </div>

      {/* Cache entries table */}
      <div className="card lg:col-span-2">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <span className="section-label">Cache entries</span>
          <div className="flex items-center gap-2">
            {caches.length > 0 && (
              <>
                <span className="stat-chip">{formatBytes(totalBytes)}</span>
                <span className="stat-chip">{caches.length} total</span>
              </>
            )}
          </div>
        </div>

        {caches.length > 0 && (
          <div className="p-4 border-b border-border">
            <p className="section-label mb-3">MB cached by ref</p>
            <BarGroupChart
              data={cacheChartData}
              bars={[{ key: "mb", color: CHART_COLORS.primary }]}
              height={180}
            />
          </div>
        )}

        {listMutation.isPending ? (
          /* Skeleton while loading */
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Key</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Ref</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Size</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Created</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Last accessed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {Array.from({ length: 4 }).map((_, i) => (
                  <tr key={i}>
                    <td className="px-4 py-3"><Skeleton className="h-3 w-36" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-3 w-20" /></td>
                    <td className="px-4 py-3 text-right"><Skeleton className="h-3 w-12 ml-auto" /></td>
                    <td className="px-4 py-3 text-right"><Skeleton className="h-3 w-16 ml-auto" /></td>
                    <td className="px-4 py-3 text-right"><Skeleton className="h-3 w-20 ml-auto" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : caches.length === 0 ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground font-mono">
              — click &ldquo;Load caches&rdquo; to list entries
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Key</th>
                  <th className="text-left text-muted-foreground font-medium px-4 py-2">Ref</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Size</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Created</th>
                  <th className="text-right text-muted-foreground font-medium px-4 py-2">Last accessed</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {caches.map((c) => {
                  const staleness = classifyStaleness(c.last_accessed_at)
                  const { text: staleText, dot: staleDot } = stalenessColor[staleness]
                  const isThisRowArmed = clearArmed && clearTarget?.key === c.key && clearTarget?.ref === c.ref
                  return (
                    <tr key={c.id} className="hover:bg-muted/40 transition-colors">
                      <td className="px-4 py-2.5 font-mono text-foreground/80 max-w-[14rem] truncate">{c.key}</td>
                      <td className="px-4 py-2.5 text-muted-foreground max-w-[8rem] truncate">{c.ref}</td>
                      <td className="px-4 py-2.5 text-right font-mono text-muted-foreground tabular-nums">
                        {formatBytes(c.size_in_bytes)}
                      </td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground whitespace-nowrap">
                        {relativeTime(c.created_at)}
                      </td>
                      <td className="px-4 py-2.5 text-right whitespace-nowrap">
                        <span className={`inline-flex items-center gap-1 font-mono text-[0.6875rem] ${staleText}`}>
                          <span className={`inline-block size-1.5 rounded-full ${staleDot}`} />
                          {relativeTime(c.last_accessed_at)}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Button
                          variant="outline"
                          className="h-6 px-2 text-[0.6875rem]"
                          disabled={isLoading || !actor}
                          onClick={() => {
                            if (isThisRowArmed) {
                              setClearArmed(false)
                              clearMutation.mutate(false)
                            } else {
                              setClearTarget({ key: c.key, ref: c.ref })
                              setClearArmed(true)
                            }
                          }}
                        >
                          <Trash className="size-3" />
                          {isThisRowArmed ? "Confirm clear key" : "Clear key"}
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>

    {/* Clear result card */}
    {clearMutation.data && (
      <div className="card mt-4">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <span className="section-label">Result</span>
          {clearMutation.data.dry_run && (
            <span className="stat-chip text-yellow-400 border-yellow-500/30">dry run</span>
          )}
        </div>
        <div className="p-4">
          {clearMutation.data.dry_run ? (
            <p className="text-sm text-yellow-400/80">
              Dry run complete — no caches were deleted.
            </p>
          ) : clearMutation.data.job_id ? (
            <div className="flex items-center gap-3">
              <p className="text-sm text-green-400">
                Cache clear queued — Job #{clearMutation.data.job_id}
              </p>
              <Link
                href={`/jobs?id=${clearMutation.data.job_id}`}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors"
              >
                View in Job Queue →
              </Link>
            </div>
          ) : (
            <pre className="font-mono text-xs text-muted-foreground leading-relaxed overflow-auto bg-muted/30 rounded-md p-3 border border-border/50">
              {JSON.stringify(clearMutation.data, null, 2)}
            </pre>
          )}
        </div>
      </div>
    )}
    </>
  )
}

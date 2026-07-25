"use client"

import { useEffect, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateInline } from "@/components/empty-state"
import { SectionError } from "@/components/section-error"
import { api } from "@/lib/api/client"
import type { JobOut } from "@/lib/api/types"

const ACTION_TYPES = [
  "",
  "cache.clear",
  "cache.clear.dry_run",
  "cache.clear.queued",
  "installation.connected",
  "installation.connected.personal",
]

const JOB_STATUS_COLOR: Record<JobOut["status"], string> = {
  queued:     "text-muted-foreground",
  processing: "text-yellow-400",
  done:       "text-accent",
  failed:     "text-destructive",
}

// audit_logs.payload embeds job_id for cache-clear rows (a deliberate cross-reference,
// not duplicate data -- see #281) so a row's live job status can be shown inline instead
// of sending the user to a separate Job Queue page for exactly one operation type.
function parseJobId(payload: string): number | null {
  try {
    const parsed: unknown = JSON.parse(payload)
    const jobId = (parsed as { job_id?: unknown })?.job_id
    return typeof jobId === "number" ? jobId : null
  } catch {
    return null
  }
}

export default function AuditPage() {
  const [actionFilter, setActionFilter] = useState("")
  const searchParams = useSearchParams()
  const highlightJobId = Number(searchParams.get("job_id")) || null

  const { data: logs = [], isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ["audit", actionFilter],
    queryFn: () => api.audit.list(actionFilter || undefined),
    refetchInterval: 30_000,
  })

  // Reused across every row rather than one query per row -- same list the old Jobs
  // page polled, just fetched once here and matched by id.
  const { data: jobs = [], isError: isJobsError } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs.list,
    refetchInterval: 15_000,
  })
  const jobsById = new Map(jobs.map((j) => [j.id, j]))

  const highlightRef = useRef<HTMLTableRowElement>(null)
  useEffect(() => {
    if (highlightRef.current) {
      highlightRef.current.scrollIntoView({ block: "center", behavior: "smooth" })
    }
  }, [highlightJobId, logs.length])

  return (
    <>
      <PageHeader title="Audit Log" description="Immutable record of all significant actions." />

      <div className="card">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-4">
          <span className="section-label">Events</span>
          <div className="flex items-center gap-3">
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              className="bg-elevated border border-border rounded-md text-xs text-muted-foreground font-mono px-2 py-1 focus:outline-none focus:border-primary"
            >
              <option value="">all actions</option>
              {ACTION_TYPES.filter(Boolean).map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
            {!isLoading && !isError && <span className="stat-chip">{logs.length} entries</span>}
          </div>
        </div>

        {isLoading ? (
          <div className="px-4 py-8">
            <p className="text-sm text-muted-foreground animate-pulse">Loading…</p>
          </div>
        ) : isError ? (
          <SectionError
            message={error instanceof Error ? error.message : "Failed to load audit events."}
            onRetry={() => refetch()}
            retrying={isFetching}
          />
        ) : logs.length === 0 ? (
          <EmptyStateInline noun="audit events" qualifier={actionFilter || undefined} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left section-label px-4 py-2">Actor</th>
                  <th className="text-left section-label px-4 py-2">Action</th>
                  <th className="text-left section-label px-4 py-2">Target</th>
                  <th className="text-left section-label px-4 py-2">Job status</th>
                  <th className="text-right section-label px-4 py-2">Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {logs.map((log) => {
                  const jobId = parseJobId(log.payload)
                  const job = jobId !== null ? jobsById.get(jobId) : undefined
                  const isHighlighted = highlightJobId !== null && jobId === highlightJobId
                  return (
                    <tr
                      key={log.id}
                      ref={isHighlighted ? highlightRef : null}
                      className={[
                        "hover:bg-elevated transition-colors",
                        isHighlighted ? "ring-1 ring-inset ring-primary/40 bg-primary/5" : "",
                      ].join(" ")}
                    >
                      <td className="px-4 py-2.5 font-mono text-foreground/80">{log.actor}</td>
                      <td className="px-4 py-2.5 text-primary font-mono">{log.action}</td>
                      <td className="px-4 py-2.5 text-muted-foreground max-w-[14rem] truncate">{log.target}</td>
                      <td className="px-4 py-2.5 font-mono">
                        {job ? (
                          <span className={`font-medium ${JOB_STATUS_COLOR[job.status]}`}>{job.status}</span>
                        ) : jobId !== null && isJobsError ? (
                          <span className="text-destructive" title="Failed to load job status">failed to load</span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-right font-mono text-muted-foreground whitespace-nowrap">
                        {new Date(log.created_at).toLocaleString()}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

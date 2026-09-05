"use client"

import { useEffect, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateInline } from "@/components/empty-state"
import { SectionError } from "@/components/section-error"
import { Button } from "@/components/ui/button"
import { DataTable, type DataTableColumn } from "@/components/ui/data-table"
import { api } from "@/lib/api/client"
import type { AuditLogOut, JobOut } from "@/lib/api/types"

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

// The backend has no true offset/cursor pagination -- /audit only ever returns the N
// most recent rows (Query(default=100, le=500)) -- so "Load more" re-fetches with a
// bigger limit rather than requesting a next page.
const INITIAL_LIMIT = 100
const LIMIT_STEP = 100
const MAX_LIMIT = 500

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
  const [limit, setLimit] = useState(INITIAL_LIMIT)
  const searchParams = useSearchParams()
  const highlightJobId = Number(searchParams.get("job_id")) || null

  // A new filter starts back at the default window -- an old "load more" bump for a
  // different (or no) filter isn't a meaningful limit for this one.
  useEffect(() => {
    setLimit(INITIAL_LIMIT)
  }, [actionFilter])

  const { data: logs = [], isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ["audit", actionFilter, limit],
    queryFn: () => api.audit.list(actionFilter || undefined, limit),
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

  const columns: DataTableColumn<AuditLogOut>[] = [
    {
      key: "actor",
      header: "Actor",
      sortValue: (log) => log.actor,
      cellClassName: "font-mono text-foreground/80",
      render: (log) => log.actor,
    },
    {
      key: "action",
      header: "Action",
      sortValue: (log) => log.action,
      cellClassName: "text-primary font-mono",
      render: (log) => log.action,
    },
    {
      key: "target",
      header: "Target",
      sortValue: (log) => log.target,
      cellClassName: "text-muted-foreground max-w-[14rem] truncate",
      render: (log) => log.target,
    },
    {
      key: "job_status",
      header: "Job status",
      cellClassName: "font-mono",
      render: (log) => {
        const jobId = parseJobId(log.payload)
        const job = jobId !== null ? jobsById.get(jobId) : undefined
        return job ? (
          <span className={`font-medium ${JOB_STATUS_COLOR[job.status]}`}>{job.status}</span>
        ) : jobId !== null && isJobsError ? (
          <span className="text-destructive" title="Failed to load job status">failed to load</span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )
      },
    },
    {
      key: "created_at",
      header: "Time",
      align: "right",
      sortValue: (log) => new Date(log.created_at).getTime(),
      cellClassName: "font-mono text-muted-foreground whitespace-nowrap",
      render: (log) => new Date(log.created_at).toLocaleString(),
    },
  ]

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
          <>
            <DataTable
              columns={columns}
              data={logs}
              getRowKey={(log) => log.id}
              // Set high enough that this table's own pagination never triggers (the
              // backend already caps rows at MAX_LIMIT) -- "Load more" below, not
              // client-side paging, is what lets a highlighted row past the current
              // window stay reachable instead of hiding behind an unrelated page click.
              pageSize={MAX_LIMIT}
              getRowRef={(log) => {
                const jobId = parseJobId(log.payload)
                return highlightJobId !== null && jobId === highlightJobId ? highlightRef : undefined
              }}
              rowClassName={(log) => {
                const jobId = parseJobId(log.payload)
                const isHighlighted = highlightJobId !== null && jobId === highlightJobId
                return [
                  "hover:bg-elevated transition-colors",
                  isHighlighted ? "ring-1 ring-inset ring-primary/40 bg-primary/5" : "",
                ].join(" ")
              }}
            />
            {logs.length >= limit && limit < MAX_LIMIT && (
              <div className="px-4 py-3 border-t border-border flex justify-center">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={isFetching}
                  onClick={() => setLimit((l) => Math.min(l + LIMIT_STEP, MAX_LIMIT))}
                >
                  {isFetching ? "Loading…" : "Load more"}
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}

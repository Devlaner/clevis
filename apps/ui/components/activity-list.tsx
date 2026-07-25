"use client"

import Link from "next/link"
import { relativeTime, jobTypeLabel } from "@/lib/format"
import type { JobOut } from "@/lib/api/types"

const statusStyle: Record<JobOut["status"], string> = {
  queued:     "text-muted-foreground",
  processing: "text-yellow-400 animate-pulse",
  done:       "text-green-400",
  failed:     "text-red-400",
}

interface ActivityListProps {
  jobs: JobOut[]
  isLoading: boolean
  limit?: number
}

export function ActivityList({ jobs, isLoading, limit }: ActivityListProps) {
  if (isLoading) {
    return (
      <div className="divide-y divide-border">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="px-4 py-3 flex items-center justify-between gap-4 animate-pulse">
            <div className="flex flex-col gap-1.5">
              <div className="h-3 w-40 bg-muted rounded-md" />
              <div className="h-2.5 w-24 bg-muted/60 rounded-md" />
            </div>
            <div className="h-2.5 w-14 bg-muted/60 rounded-md" />
          </div>
        ))}
      </div>
    )
  }

  const visible = limit ? jobs.slice(0, limit) : jobs

  if (visible.length === 0) {
    return (
      <div className="px-4 py-8">
        <p className="text-sm text-muted-foreground font-mono">— no jobs yet</p>
      </div>
    )
  }

  return (
    <div className="divide-y divide-border">
      {visible.map((job, i) => (
        <div
          key={job.id}
          className="stagger-item px-4 py-3 flex items-start justify-between gap-4 hover:bg-elevated transition-colors duration-150 ease-(--ease-out)"
          style={{ animationDelay: `${Math.min(i, 6) * 45}ms` }}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-mono text-muted-foreground shrink-0">#{job.id}</span>
              <span className="text-sm text-foreground/90 truncate">{jobTypeLabel(job.job_type)}</span>
              <span className={`text-[0.6875rem] font-mono font-medium shrink-0 ${statusStyle[job.status]}`}>
                {job.status}
              </span>
            </div>
            <p className="text-[0.6875rem] text-muted-foreground/60 font-mono mt-0.5 truncate">
              {job.job_type}
            </p>
          </div>
          <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap shrink-0 mt-0.5">
            {relativeTime(job.created_at)}
          </span>
        </div>
      ))}
      {limit && jobs.length > limit && (
        <div className="px-4 py-2.5">
          <Link href="/audit" className="text-xs text-muted-foreground hover:text-foreground transition-colors">
            View all {jobs.length} jobs →
          </Link>
        </div>
      )}
    </div>
  )
}

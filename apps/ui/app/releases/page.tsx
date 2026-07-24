"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateInline } from "@/components/empty-state"
import { SectionError } from "@/components/section-error"
import { relativeTime } from "@/lib/format"
import { api } from "@/lib/api/client"

const DAY_OPTIONS = [30, 90, 180] as const

export default function ReleasesPage() {
  const [org, setOrg] = useState("")
  const [orgChecked, setOrgChecked] = useState(false)
  const [days, setDays] = useState<(typeof DAY_OPTIONS)[number]>(90)
  useEffect(() => {
    setOrg(localStorage.getItem("default_org") || "")
    setOrgChecked(true)
  }, [])

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 2,
    retry: false,
  })

  const token = resolveQuery.data?.token ?? ""
  const queriesEnabled = org.trim().length > 2 && !resolveQuery.isLoading

  const releaseTimelineQuery = useQuery({
    queryKey: ["github.release-timeline", org, days],
    queryFn: () => api.github.releaseTimeline(org, token, days),
    enabled: queriesEnabled,
    retry: false,
  })

  const releases = releaseTimelineQuery.data?.releases ?? []

  return (
    <>
      <PageHeader title="Releases" description="Release history across your organization." />

      {orgChecked && !org && (
        <div className="card mb-6">
          <p className="px-4 py-6 text-sm text-muted-foreground">
            No default organization selected yet — this page has nothing to query. Set one in{" "}
            <Link href="/settings" className="text-primary hover:underline">Settings</Link>, or connect a GitHub
            org there first if you haven&rsquo;t already.
          </p>
        </div>
      )}

      {org && (
        <div className="card">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-4">
            <span className="section-label">Releases</span>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value) as (typeof DAY_OPTIONS)[number])}
              aria-label="Release time range"
              className="bg-elevated border border-border rounded-md text-xs text-muted-foreground font-mono px-2 py-1 focus:outline-none focus:border-primary"
            >
              {DAY_OPTIONS.map((d) => (
                <option key={d} value={d}>last {d} days</option>
              ))}
            </select>
          </div>

          {releaseTimelineQuery.isLoading || resolveQuery.isLoading ? (
            <div className="px-4 py-8">
              <p className="text-sm text-muted-foreground animate-pulse">Loading…</p>
            </div>
          ) : releaseTimelineQuery.isError ? (
            <SectionError
              message={
                releaseTimelineQuery.error instanceof Error
                  ? releaseTimelineQuery.error.message
                  : "Failed to load releases."
              }
              onRetry={() => releaseTimelineQuery.refetch()}
              retrying={releaseTimelineQuery.isFetching}
            />
          ) : releases.length === 0 ? (
            <EmptyStateInline noun={`releases in the last ${days} days`} />
          ) : (
            <div className="divide-y divide-border">
              {releases.map((r) => (
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
                    <span className="text-[0.6875rem] text-muted-foreground truncate">
                      {r.tag_name} — {r.body_preview}
                    </span>
                  </span>
                  <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap shrink-0 ml-3">
                    {relativeTime(r.published_at)}
                  </span>
                </a>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  )
}

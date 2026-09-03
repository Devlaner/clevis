"use client"

import { useState } from "react"
import { relativeTime } from "@/lib/format"
import type { OrgEvent } from "@/lib/api/types"

const FILTERS = [
  { id: "all", label: "All", type: null },
  { id: "pushes", label: "Pushes", type: "PushEvent" },
  { id: "prs", label: "Pull Requests", type: "PullRequestEvent" },
  { id: "issues", label: "Issues", type: "IssuesEvent" },
  { id: "releases", label: "Releases", type: "ReleaseEvent" },
] as const

type FilterId = (typeof FILTERS)[number]["id"]

interface EventFeedProps {
  events: OrgEvent[]
  isLoading: boolean
}

export function EventFeed({ events, isLoading }: EventFeedProps) {
  const [filter, setFilter] = useState<FilterId>("all")

  const activeType = FILTERS.find((f) => f.id === filter)?.type ?? null
  const visible = activeType ? events.filter((e) => e.type === activeType) : events

  return (
    <div>
      <div className="px-4 py-2.5 border-b border-border flex items-center gap-1.5 flex-wrap">
        {FILTERS.map((f) => {
          const active = filter === f.id
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              aria-pressed={active}
              className={`text-xs font-mono px-2 py-1 border transition-colors ${
                active
                  ? "border-primary bg-primary/10 text-foreground"
                  : "border-border text-muted-foreground hover:bg-elevated"
              }`}
            >
              {f.label}
            </button>
          )
        })}
      </div>

      {isLoading ? (
        <div className="divide-y divide-border">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="px-4 py-3 flex items-center gap-3 animate-pulse">
              <div className="h-6 w-6 bg-muted rounded-full shrink-0" />
              <div className="flex flex-col gap-1.5 flex-1">
                <div className="h-3 w-56 bg-muted rounded-md" />
                <div className="h-2.5 w-28 bg-muted/60 rounded-md" />
              </div>
            </div>
          ))}
        </div>
      ) : visible.length === 0 ? (
        <div className="px-4 py-8">
          <p className="text-sm text-muted-foreground font-mono">— no events yet</p>
        </div>
      ) : (
        <div className="divide-y divide-border">
          {visible.map((event, i) => (
            <div
              key={event.id}
              className="stagger-item px-4 py-3 flex items-start gap-3 hover:bg-elevated transition-colors duration-150 ease-(--ease-out)"
              style={{ animationDelay: `${Math.min(i, 6) * 45}ms` }}
            >
              {/* Decorative: the actor login is rendered as adjacent visible text below,
                  so an empty alt keeps screen readers from announcing the avatar twice. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={event.actor_avatar} alt="" className="h-6 w-6 shrink-0 mt-0.5" />
              <div className="min-w-0 flex-1">
                <p className="text-sm text-foreground/90 truncate">
                  <span className="font-medium">{event.actor}</span> {event.summary}
                </p>
                <p className="text-[0.6875rem] text-muted-foreground/60 font-mono mt-0.5 truncate">
                  {event.repo}
                </p>
              </div>
              <span className="text-[0.6875rem] text-muted-foreground whitespace-nowrap shrink-0 mt-0.5">
                {relativeTime(event.created_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

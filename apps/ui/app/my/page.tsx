"use client"

import { useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { MyItemsList } from "@/components/my-items-list"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"

const PER_PAGE = 25

// The three "My …" views were separate near-identical pages (issue #283); they're one
// tabbed page now, with the tab in `?tab=` so a link/bookmark still lands on the right
// view. `prs` is the default and omits the param.
const TABS = [
  {
    id: "prs",
    label: "My PRs",
    description: "Pull requests you've authored.",
    queryKey: "analytics.my-prs",
    emptyNoun: "open pull requests",
    errorMessage: "Failed to load your pull requests.",
    fetch: api.analytics.myPrs,
  },
  {
    id: "reviews",
    label: "My Reviews",
    description: "PRs awaiting your review.",
    queryKey: "analytics.my-reviews",
    emptyNoun: "pull requests awaiting your review",
    errorMessage: "Failed to load your review queue.",
    fetch: api.analytics.myReviews,
  },
  {
    id: "issues",
    label: "My Issues",
    description: "Issues assigned to you.",
    queryKey: "analytics.my-issues",
    emptyNoun: "assigned issues",
    errorMessage: "Failed to load your assigned issues.",
    fetch: api.analytics.myIssues,
  },
] as const

type TabId = (typeof TABS)[number]["id"]

export default function MyWorkPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const rawTab = searchParams.get("tab")
  const activeTab: TabId = TABS.some((t) => t.id === rawTab) ? (rawTab as TabId) : "prs"
  const config = TABS.find((t) => t.id === activeTab)!

  const { scope } = useActiveScope()
  const org = scope?.login ?? ""
  const [orgChecked, setOrgChecked] = useState(false)
  const [page, setPage] = useState(1)
  useEffect(() => {
    setOrgChecked(true)
  }, [])
  // Switching accounts or tabs changes the query key but not the page number — reset to
  // page 1 so a stale offset doesn't query the new list out of range.
  useEffect(() => {
    setPage(1)
  }, [org, activeTab])

  function selectTab(id: TabId) {
    const params = new URLSearchParams(searchParams.toString())
    if (id === "prs") params.delete("tab")
    else params.set("tab", id)
    router.replace(`?${params.toString()}`, { scroll: false })
  }

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 0,
    retry: false,
  })

  const itemsQuery = useQuery({
    queryKey: [config.queryKey, org, page],
    queryFn: () => config.fetch(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My Work" description={config.description} />

      <div className="flex items-center gap-1.5 mb-5">
        {TABS.map((t) => {
          const active = t.id === activeTab
          return (
            <button
              key={t.id}
              type="button"
              aria-pressed={active}
              onClick={() => selectTab(t.id)}
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

      {orgChecked && !org && <EmptyStateNoAccount />}

      {org && (
        <MyItemsList
          items={itemsQuery.data?.items ?? []}
          isLoading={itemsQuery.isLoading || resolveQuery.isLoading}
          isError={itemsQuery.isError}
          errorMessage={
            itemsQuery.error instanceof Error ? itemsQuery.error.message : config.errorMessage
          }
          onRetry={() => itemsQuery.refetch()}
          retrying={itemsQuery.isFetching}
          emptyNoun={config.emptyNoun}
          totalCount={itemsQuery.data?.total_count ?? 0}
          page={page}
          perPage={PER_PAGE}
          onPageChange={setPage}
          identityUnresolved={itemsQuery.data?.identity_unresolved}
        />
      )}
    </>
  )
}

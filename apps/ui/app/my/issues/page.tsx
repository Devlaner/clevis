"use client"

import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { MyItemsList } from "@/components/my-items-list"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"

const PER_PAGE = 25

export default function MyIssuesPage() {
  const { scope } = useActiveScope()
  const org = scope?.login ?? ""
  const [orgChecked, setOrgChecked] = useState(false)
  const [page, setPage] = useState(1)
  useEffect(() => {
    setOrgChecked(true)
  }, [])
  // Switching accounts changes the query key but not the page number — reset to
  // page 1 so a stale offset doesn't query the new account out of range.
  useEffect(() => {
    setPage(1)
  }, [org])

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 0,
    retry: false,
  })

  const myIssuesQuery = useQuery({
    queryKey: ["analytics.my-issues", org, page],
    queryFn: () => api.analytics.myIssues(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My Issues" description="Issues assigned to you." />

      {orgChecked && !org && <EmptyStateNoAccount />}

      {org && (
        <MyItemsList
          items={myIssuesQuery.data?.items ?? []}
          isLoading={myIssuesQuery.isLoading || resolveQuery.isLoading}
          isError={myIssuesQuery.isError}
          errorMessage={
            myIssuesQuery.error instanceof Error ? myIssuesQuery.error.message : "Failed to load your assigned issues."
          }
          onRetry={() => myIssuesQuery.refetch()}
          retrying={myIssuesQuery.isFetching}
          emptyNoun="assigned issues"
          totalCount={myIssuesQuery.data?.total_count ?? 0}
          page={page}
          perPage={PER_PAGE}
          onPageChange={setPage}
        />
      )}
    </>
  )
}

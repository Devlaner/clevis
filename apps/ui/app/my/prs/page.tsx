"use client"

import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { MyItemsList } from "@/components/my-items-list"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"

const PER_PAGE = 25

export default function MyPRsPage() {
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

  const myPrsQuery = useQuery({
    queryKey: ["analytics.my-prs", org, page],
    queryFn: () => api.analytics.myPrs(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My PRs" description="Pull requests you've authored." />

      {orgChecked && !org && <EmptyStateNoAccount />}

      {org && (
        <MyItemsList
          items={myPrsQuery.data?.items ?? []}
          isLoading={myPrsQuery.isLoading || resolveQuery.isLoading}
          isError={myPrsQuery.isError}
          errorMessage={
            myPrsQuery.error instanceof Error ? myPrsQuery.error.message : "Failed to load your pull requests."
          }
          onRetry={() => myPrsQuery.refetch()}
          retrying={myPrsQuery.isFetching}
          emptyNoun="open pull requests"
          totalCount={myPrsQuery.data?.total_count ?? 0}
          page={page}
          perPage={PER_PAGE}
          onPageChange={setPage}
        />
      )}
    </>
  )
}

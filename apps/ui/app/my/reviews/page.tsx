"use client"

import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyStateNoAccount } from "@/components/empty-state"
import { MyItemsList } from "@/components/my-items-list"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"

const PER_PAGE = 25

export default function MyReviewsPage() {
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

  const myReviewsQuery = useQuery({
    queryKey: ["analytics.my-reviews", org, page],
    queryFn: () => api.analytics.myReviews(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 0 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My Reviews" description="PRs awaiting your review." />

      {orgChecked && !org && <EmptyStateNoAccount />}

      {org && (
        <MyItemsList
          items={myReviewsQuery.data?.items ?? []}
          isLoading={myReviewsQuery.isLoading || resolveQuery.isLoading}
          isError={myReviewsQuery.isError}
          errorMessage={
            myReviewsQuery.error instanceof Error ? myReviewsQuery.error.message : "Failed to load your review queue."
          }
          onRetry={() => myReviewsQuery.refetch()}
          retrying={myReviewsQuery.isFetching}
          emptyNoun="pull requests awaiting your review"
          totalCount={myReviewsQuery.data?.total_count ?? 0}
          page={page}
          perPage={PER_PAGE}
          onPageChange={setPage}
        />
      )}
    </>
  )
}

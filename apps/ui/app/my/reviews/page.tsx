"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { MyItemsList } from "@/components/my-items-list"
import { api } from "@/lib/api/client"

const PER_PAGE = 25

export default function MyReviewsPage() {
  const [org, setOrg] = useState("")
  const [orgChecked, setOrgChecked] = useState(false)
  const [page, setPage] = useState(1)
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

  const myReviewsQuery = useQuery({
    queryKey: ["analytics.my-reviews", org, page],
    queryFn: () => api.analytics.myReviews(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 2 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My Reviews" description="PRs awaiting your review." />

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

"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
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

  const resolveQuery = useQuery({
    queryKey: ["tokens.resolve", org],
    queryFn: () => api.tokens.resolve(org),
    enabled: org.trim().length > 2,
    retry: false,
  })

  const myPrsQuery = useQuery({
    queryKey: ["analytics.my-prs", org, page],
    queryFn: () => api.analytics.myPrs(org, page, PER_PAGE, resolveQuery.data?.token),
    enabled: org.trim().length > 2 && !resolveQuery.isLoading,
    retry: false,
  })

  return (
    <>
      <PageHeader title="My PRs" description="Pull requests you've authored." />

      {orgChecked && !org && (
        <div className="card mb-6">
          <p className="px-4 py-6 text-sm text-muted-foreground">
            No account selected yet — this page has nothing to query. Pick an organization or your personal
            account from the profile menu, or connect one in{" "}
            <Link href="/settings" className="text-primary hover:underline">Settings</Link> first if you
            haven&rsquo;t already.
          </p>
        </div>
      )}

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

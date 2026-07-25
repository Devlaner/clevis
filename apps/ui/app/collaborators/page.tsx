"use client"

import { useEffect } from "react"
import { useRouter } from "next/navigation"
import { useQuery } from "@tanstack/react-query"
import { PageHeader } from "@/components/page-header"
import { EmptyState } from "@/components/empty-state"
import { api } from "@/lib/api/client"
import { useActiveScope } from "@/lib/active-scope"
import type { MyOrgMembership } from "@/lib/api/types"

export default function CollaboratorsPage() {
  const router = useRouter()

  const { data: memberships = [], isLoading, isError } = useQuery<MyOrgMembership[]>({
    queryKey: ["my-orgs"],
    queryFn: () => api.orgs.mine(),
  })

  const { scope } = useActiveScope()
  const adminOrgs = memberships.filter((m) => m.role === "admin")
  const target = adminOrgs.find((m) => scope?.kind === "org" && m.org_login === scope.login) || adminOrgs[0]

  useEffect(() => {
    if (target) router.replace(`/settings/org/${encodeURIComponent(target.org_login)}/members`)
  }, [target, router])

  if (isLoading || target) {
    return (
      <>
        <PageHeader
          title="Collaborators"
          description="Manage your organization's team and invitations."
        />
        <div className="card">
          <EmptyState title="Redirecting…" description="Taking you to member management." />
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Collaborators"
        description="Manage your organization's team and invitations."
      />
      <div className="card">
        {isError ? (
          <EmptyState
            title="Couldn't load your organizations"
            description="Something went wrong checking your organization memberships. Try refreshing the page."
          />
        ) : memberships.length > 0 ? (
          <div className="border border-dashed border-border rounded-md px-6 py-12">
            <p className="text-sm text-muted-foreground">
              Member management is only available for organizations where you&rsquo;re an admin. You&rsquo;re a
              member (not admin) of:
            </p>
            <ul className="mt-2 text-sm text-foreground list-disc list-inside">
              {memberships.map((m) => (
                <li key={m.org_login}>{m.org_login}</li>
              ))}
            </ul>
            <p className="mt-3 text-xs text-muted-foreground">
              Ask an admin of one of these organizations to grant you admin access.
            </p>
          </div>
        ) : (
          <EmptyState
            title="No organization to manage"
            description="Member management is available for organizations where you're an admin. Visit Settings to see your organizations."
          />
        )}
      </div>
    </>
  )
}

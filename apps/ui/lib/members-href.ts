import type { ActiveScope } from "@/lib/active-scope"
import type { MyOrgMembership } from "@/lib/api/types"

// Resolves the URL of the org members-management page for the current user.
//
// The members page (apps/ui/app/settings/org/[login]/members/page.tsx) is admin-only
// on the backend, so this only ever points at an org where the user is an admin:
// prefer the org matching the active scope, else the first admin org. When the user
// admins no org there is nothing to manage, so it falls back to /settings (which lists
// every membership with its own "Manage members" link). This is the same resolution
// the profile-menu "Invite members" link uses.
//
// Replaces the old /collaborators redirect stub (issue #282), which did this same
// lookup only to immediately router.replace() into the resolved route.
export function membersHref(memberships: MyOrgMembership[], scope: ActiveScope | null): string {
  const adminOrgs = memberships.filter((m) => m.role === "admin")
  const target =
    adminOrgs.find((m) => scope?.kind === "org" && m.org_login === scope.login) ?? adminOrgs[0]
  return target ? `/settings/org/${encodeURIComponent(target.org_login)}/members` : "/settings"
}

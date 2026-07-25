import Link from "next/link"

/**
 * Empty state variants — no icons, no centered layouts, just text.
 *
 * <EmptyStateInline>    — inside a card/table where data would be
 * <EmptyStatePage>      — when a whole feature section has no data yet
 * <EmptyStateNoAccount> — no org/personal account selected in the profile menu
 */

interface EmptyStateInlineProps {
  noun: string         // e.g. "jobs", "caches", "members"
  qualifier?: string   // e.g. filter value currently applied
}

export function EmptyStateInline({ noun, qualifier }: EmptyStateInlineProps) {
  return (
    <div className="px-4 py-8 border-t border-border/60">
      <p className="text-sm text-muted-foreground">
        No {noun}{qualifier ? ` matching "${qualifier}"` : ""}
      </p>
    </div>
  )
}

interface EmptyStatePageProps {
  message: string
  action?: { href: string; label: string }
}

export function EmptyStatePage({ message, action }: EmptyStatePageProps) {
  return (
    <div className="border border-dashed border-border rounded-md px-6 py-12">
      <p className="text-sm text-muted-foreground">
        {message}
        {action && (
          <>
            {" — "}
            <a
              href={action.href}
              className="text-primary underline-offset-2 hover:underline"
            >
              {action.label}
            </a>
          </>
        )}
      </p>
    </div>
  )
}

interface EmptyStateNoAccountProps {
  /** Skip the outer card wrapper -- for use inside a panel that's already a card. */
  bare?: boolean
  /** Override the default "this page has nothing to query" copy -- for pages (like
   * Repos) where a manual search still works without an active scope, so the default
   * wording would be misleading. */
  message?: string
}

export function EmptyStateNoAccount({ bare = false, message: messageOverride }: EmptyStateNoAccountProps) {
  const message = (
    <p className="px-4 py-6 text-sm text-muted-foreground">
      {messageOverride ?? (
        <>
          No account selected yet — this page has nothing to query. Pick an organization or your personal
          account from the profile menu, or connect one in{" "}
          <Link href="/settings" className="text-primary hover:underline">Settings</Link> first if you
          haven&rsquo;t already.
        </>
      )}
    </p>
  )
  return bare ? message : <div className="card mb-6">{message}</div>
}

/**
 * Legacy compat shim — existing pages that pass icon/title/description
 * are redirected to EmptyStatePage so the old pattern doesn't crash.
 * Remove once all callers are migrated.
 */
interface LegacyEmptyStateProps {
  icon?: unknown
  title: string
  description: string
  cta?: React.ReactNode
}

export function EmptyState({ title, description }: LegacyEmptyStateProps) {
  return <EmptyStatePage message={`${title} — ${description}`} />
}

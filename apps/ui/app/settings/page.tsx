"use client"

import { Fragment, useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query"
import { Trash, Plus, CircleNotch, Check, ArrowSquareOut, CheckCircle } from "@phosphor-icons/react"
import { PageHeader } from "@/components/page-header"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel, FieldDescription } from "@/components/ui/field"
import { SectionError } from "@/components/section-error"
import { EmptyStatePage } from "@/components/empty-state"
import { PermissionDriftNotice } from "@/components/permission-drift-notice"
import { ConfirmDialog } from "@/components/ui/confirm-dialog"
import { toast } from "@/components/ui/toast"
import Link from "next/link"
import { api } from "@/lib/api/client"
import { initialConfigValues, mergeSavedConfigValue } from "@/lib/config-values"
import { useAuth } from "@/lib/auth-context"
import { THEMES, useTheme } from "@/lib/theme"
import type { InstallationMeta, MyOrgMembership, SavedTokenMeta } from "@/lib/api/types"

// ── Profile section ──────────────────────────────────────────────────────────

function ProfileSection() {
  const { user, updateUser, logout } = useAuth()
  const [name, setName] = useState(user?.name || "")
  const [saved, setSaved] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [revokeArmed, setRevokeArmed] = useState(false)

  const revokeSessions = useMutation({
    mutationFn: () => api.auth.revokeSessions(),
    // Bumping token_version invalidates this device's own token too, so finish by
    // logging out locally rather than leaving the UI in a now-unauthenticated state.
    onSuccess: () => logout(),
  })

  // Auto-disarm if the user doesn't confirm within a few seconds, matching the
  // cache-clear confirm pattern (components/repo/cache-panel.tsx).
  useEffect(() => {
    if (!revokeArmed) return
    const timer = setTimeout(() => setRevokeArmed(false), 4000)
    return () => clearTimeout(timer)
  }, [revokeArmed])

  useEffect(() => {
    setName(user?.name || "")
  }, [user])

  async function save() {
    setIsSaving(true)
    try {
      if (name.trim() !== (user?.name || "")) {
        const updated = await api.auth.patchMe(name.trim())
        updateUser({ name: updated.name })
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setIsSaving(false)
    }
  }

  let buttonContent: React.ReactNode = "Save profile"
  if (saved) {
    buttonContent = <><Check className="size-3.5" />Saved</>
  } else if (isSaving) {
    buttonContent = <><CircleNotch className="size-3.5 animate-spin" />Saving…</>
  }

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-label">Profile</span>
      </div>
      <div className="p-4 flex flex-col gap-3 max-w-sm">
        <Field>
          <FieldLabel>Display name</FieldLabel>
          <Input
            placeholder="Your name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel>Email</FieldLabel>
          <Input value={user?.email || ""} disabled className="opacity-60 cursor-not-allowed" />
          <FieldDescription>Email cannot be changed.</FieldDescription>
        </Field>
        <p className="text-xs text-muted-foreground -mt-1">
          Switch between your organizations and personal GitHub account from the profile menu in the sidebar.
        </p>
        <Button onClick={save} disabled={isSaving} className="mt-1 w-fit">
          {buttonContent}
        </Button>
      </div>
      <div className="px-4 py-3 border-t border-border flex flex-col gap-2 items-start">
        <p className="text-xs text-muted-foreground max-w-sm">
          Invalidates every session on every device, including this one — signs you out
          everywhere. Use this if a device was lost or a session token may have leaked.
        </p>
        <Button
          variant="destructive"
          onClick={() => {
            if (revokeArmed) {
              setRevokeArmed(false)
              revokeSessions.mutate()
            } else {
              setRevokeArmed(true)
            }
          }}
          disabled={revokeSessions.isPending}
        >
          {revokeSessions.isPending ? (
            <><CircleNotch className="size-3.5 animate-spin" />Signing out…</>
          ) : revokeArmed ? (
            "Click again to confirm"
          ) : (
            "Sign out of all devices"
          )}
        </Button>
      </div>
    </div>
  )
}

// ── Appearance section ───────────────────────────────────────────────────────

function AppearanceSection() {
  const { theme, setTheme } = useTheme()

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-label">Appearance</span>
        <p className="text-xs text-muted-foreground mt-0.5">Theme is saved to this browser.</p>
      </div>
      <div className="p-4 grid grid-cols-2 sm:grid-cols-3 gap-2">
        {THEMES.map((t) => {
          const active = theme === t.name
          return (
            <button
              key={t.name}
              onClick={() => setTheme(t.name)}
              aria-pressed={active}
              className={[
                "flex items-center gap-2.5 px-3 py-2.5 border text-left transition-colors",
                active ? "border-primary bg-primary/10" : "border-border hover:bg-elevated",
              ].join(" ")}
            >
              <span
                data-theme={t.name}
                className="flex shrink-0 overflow-hidden rounded-md border border-border/60"
              >
                <span className="size-3.5 bg-background" />
                <span className="size-3.5 bg-card" />
                <span className="size-3.5 bg-primary" />
              </span>
              <span className="flex-1 text-xs font-medium text-foreground">{t.label}</span>
              {active && <Check className="size-3.5 text-primary shrink-0" />}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Org memberships section ───────────────────────────────────────────────────

// Shared by OrgMembershipsSection and ConnectedOrgsSection below -- both need to know which
// orgs the caller belongs to (the latter specifically to know which orgs it admins, to fetch
// their installations). One hook, one queryFn reference, so React Query's dedup-by-key doesn't
// depend on which of the two mounts first actually issuing the request.
function useMyOrgMemberships() {
  return useQuery<MyOrgMembership[]>({
    queryKey: ["my-orgs"],
    queryFn: () => api.orgs.mine(),
  })
}

function OrgMembershipsSection() {
  const { data: memberships = [], isLoading, isError, error, isFetching, refetch } = useMyOrgMemberships()

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <span className="section-label">Your organizations</span>
        {memberships.length > 0 && <span className="stat-chip">{memberships.length}</span>}
      </div>

      {isLoading ? (
        <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-3.5 animate-spin" /> Loading…
        </div>
      ) : isError ? (
        <SectionError
          message={error instanceof Error ? error.message : "Failed to load organizations."}
          onRetry={() => refetch()}
          retrying={isFetching}
        />
      ) : memberships.length === 0 ? (
        <div className="px-4 py-6">
          <p className="text-sm text-muted-foreground">
            You&rsquo;re not a member of any organization yet. Sign in as a GitHub org admin to connect one, or
            accept an invite link from an org admin.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Organization</th>
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Role</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {memberships.map((m) => (
                <tr key={m.org_login} className="hover:bg-elevated transition-colors">
                  <td className="px-4 py-2.5 font-mono text-foreground/80">{m.org_login}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{m.role}</td>
                  <td className="px-4 py-2.5 text-right">
                    {m.role === "admin" && (
                      <Link
                        href={`/settings/org/${encodeURIComponent(m.org_login)}/members`}
                        className="text-xs text-primary hover:underline"
                      >
                        Manage members
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Connected organizations (GitHub App, personal + org-scoped installs) ─────

type ConnectedInstallation = InstallationMeta & (
  | { scope: "me" }
  | { scope: "org"; orgLogin: string }
)

function ConnectedOrgsSection() {
  const queryClient = useQueryClient()
  const [confirmRow, setConfirmRow] = useState<ConnectedInstallation | null>(null)

  const personalQuery = useQuery<InstallationMeta[]>({
    queryKey: ["installations", "me"],
    queryFn: () => api.installations.list(),
  })
  const membershipsQuery = useMyOrgMemberships()
  const adminOrgLogins = (membershipsQuery.data ?? []).filter((m) => m.role === "admin").map((m) => m.org_login)

  // Only orgs the caller admins -- listForOrg is 403 for a plain member (installations are
  // an admin concern, matching the DELETE endpoint's own require_org_role(min_role="admin")).
  const orgInstallQueries = useQueries({
    queries: adminOrgLogins.map((orgLogin) => ({
      queryKey: ["installations", "org", orgLogin],
      queryFn: () => api.installations.listForOrg(orgLogin),
      enabled: !membershipsQuery.isLoading,
    })),
  })

  const isLoading = personalQuery.isLoading || membershipsQuery.isLoading
  const isError = personalQuery.isError || membershipsQuery.isError || orgInstallQueries.some((q) => q.isError)
  const isFetching = personalQuery.isFetching || membershipsQuery.isFetching || orgInstallQueries.some((q) => q.isFetching)
  const refetchAll = () => {
    personalQuery.refetch()
    membershipsQuery.refetch()
    orgInstallQueries.forEach((q) => q.refetch())
  }

  const rows: ConnectedInstallation[] = [
    ...(personalQuery.data ?? []).map((i) => ({ ...i, scope: "me" as const })),
    ...adminOrgLogins.flatMap((orgLogin, idx) =>
      (orgInstallQueries[idx]?.data ?? []).map((i) => ({ ...i, scope: "org" as const, orgLogin })),
    ),
  ]

  const disconnect = useMutation({
    mutationFn: (row: ConnectedInstallation) => {
      if (row.installation_id == null) return Promise.reject(new Error("This connection has no GitHub installation to disconnect."))
      return api.installations.remove(row.scope === "me" ? { scope: "me" } : { scope: "org", orgLogin: row.orgLogin }, row.installation_id)
    },
    onSuccess: (_data, row) => {
      queryClient.invalidateQueries({ queryKey: ["installations"] })
      setConfirmRow(null)
      toast.success(`Disconnected ${row.account_login}.`)
    },
    onError: (error) => {
      setConfirmRow(null)
      toast.error(error instanceof Error ? error.message : "Disconnect failed.")
    },
  })

  const rowKey = (row: ConnectedInstallation) => `${row.scope}:${row.id}`

  const slug = process.env.NEXT_PUBLIC_GITHUB_APP_SLUG
  const installUrl = slug ? `https://github.com/apps/${slug}/installations/new` : null

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <span className="section-label">Connected GitHub accounts</span>
        {rows.length > 0 && <span className="stat-chip">{rows.length} connected</span>}
      </div>

      {disconnect.isError && (
        <p role="alert" className="px-4 pt-3 text-xs text-destructive">
          {disconnect.error instanceof Error ? disconnect.error.message : "Disconnect failed."}
        </p>
      )}

      {isLoading ? (
        <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-3.5 animate-spin" /> Loading…
        </div>
      ) : isError ? (
        <SectionError
          message="Failed to load connected accounts."
          onRetry={refetchAll}
          retrying={isFetching}
        />
      ) : rows.length === 0 ? (
        <EmptyStatePage message="No accounts connected yet. Install the Clevis GitHub App on an organization or your personal account to get started." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Account</th>
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Type</th>
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Connected</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => {
                const key = rowKey(row)
                const isThisRowMutating = disconnect.isPending && disconnect.variables && rowKey(disconnect.variables) === key
                const showDrift = (row.blocked_features?.length ?? 0) > 0 || row.permissions_synced_at === null
                return (
                  <Fragment key={key}>
                  <tr className="hover:bg-elevated transition-colors">
                    <td className="px-4 py-2.5 font-mono text-foreground/80">
                      {row.account_login}
                      {row.scope === "org" && <span className="ml-1.5 text-muted-foreground">(org)</span>}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">{row.account_type}</td>
                    <td className="px-4 py-2.5 text-muted-foreground whitespace-nowrap">
                      {new Date(row.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
                    </td>
                    <td className="px-4 py-2.5 text-right whitespace-nowrap">
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={disconnect.isPending}
                        onClick={() => setConfirmRow(row)}
                      >
                        {isThisRowMutating ? (
                          <CircleNotch className="size-3 animate-spin" />
                        ) : (
                          <><Trash className="size-3" />Disconnect</>
                        )}
                      </Button>
                    </td>
                  </tr>
                  {showDrift && (
                    <tr>
                      <td colSpan={4} className="px-4 pb-3 pt-0">
                        <PermissionDriftNotice install={row} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="border-t border-border p-4">
        {installUrl ? (
          <Button onClick={() => { window.location.href = installUrl }}>
            <ArrowSquareOut className="size-3.5" />
            {rows.length > 0 ? "Install on another account or org" : "Install GitHub App"}
          </Button>
        ) : (
          <p className="text-xs text-muted-foreground">
            GitHub App integration isn&rsquo;t set up on this instance yet — ask a workspace admin to configure it.
          </p>
        )}
      </div>

      <ConfirmDialog
        open={confirmRow !== null}
        onOpenChange={(open) => { if (!open) setConfirmRow(null) }}
        title="Disconnect this account?"
        description={
          confirmRow
            ? `Clevis will stop being able to access ${confirmRow.account_login} until it's reinstalled.`
            : ""
        }
        confirmLabel="Disconnect"
        onConfirm={() => confirmRow && disconnect.mutate(confirmRow)}
        pending={disconnect.isPending}
      />
    </div>
  )
}

// ── Saved tokens section (legacy — being replaced by the GitHub App) ──────────

function SavedTokensSection() {
  const qc = useQueryClient()
  const [addOrg, setAddOrg] = useState("")
  const [addToken, setAddToken] = useState("")
  const [addLabel, setAddLabel] = useState("")

  const { data: tokens = [], isLoading, isError, error, isFetching, refetch } = useQuery<SavedTokenMeta[]>({
    queryKey: ["tokens"],
    queryFn: () => api.tokens.list(),
  })

  const upsert = useMutation({
    mutationFn: () => api.tokens.upsert(addOrg.trim(), addToken.trim(), addLabel.trim() || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tokens"] })
      setAddOrg("")
      setAddToken("")
      setAddLabel("")
    },
  })

  const remove = useMutation({
    mutationFn: (org: string) => api.tokens.delete(org),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens"] }),
  })

  const canAdd = addOrg.trim().length > 0 && addToken.trim().length > 0

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <div className="flex items-center justify-between">
          <span className="section-label">Personal access tokens (legacy)</span>
          {tokens.length > 0 && (
            <span className="stat-chip">{tokens.length} saved</span>
          )}
        </div>
        <p className="text-xs text-muted-foreground mt-0.5">
          Being replaced by the GitHub App. Still used by Health &amp; Security and Cache pages until they move to the App.
        </p>
      </div>

      {isLoading ? (
        <div className="px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-3.5 animate-spin" /> Loading…
        </div>
      ) : isError ? (
        <SectionError
          message={error instanceof Error ? error.message : "Failed to load tokens."}
          onRetry={() => refetch()}
          retrying={isFetching}
        />
      ) : tokens.length === 0 ? (
        <div className="px-4 py-6">
          <p className="text-sm text-muted-foreground">No saved tokens yet. Add one below.</p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Org</th>
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Label</th>
                <th className="text-left text-muted-foreground font-medium px-4 py-2">Saved</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {tokens.map((t: SavedTokenMeta) => (
                <tr key={t.org} className="hover:bg-elevated transition-colors">
                  <td className="px-4 py-2.5 font-mono text-foreground/80">{t.org}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{t.label ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground whitespace-nowrap">
                    {new Date(t.created_at).toLocaleDateString(undefined, {
                      month: "short", day: "numeric", year: "numeric",
                    })}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => remove.mutate(t.org)}
                      disabled={remove.isPending}
                      className="text-muted-foreground hover:text-destructive transition-colors"
                      aria-label={`Delete token for ${t.org}`}
                    >
                      <Trash className="size-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="border-t border-border p-4">
        <p className="text-xs font-medium text-foreground mb-3">Add token</p>
        <div className="grid gap-2 sm:grid-cols-3">
          <Field>
            <FieldLabel className="sr-only">Org or owner</FieldLabel>
            <Input
              placeholder="Org or owner"
              value={addOrg}
              onChange={(e) => setAddOrg(e.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel className="sr-only">Token</FieldLabel>
            <Input
              placeholder="ghp_… token"
              type="password"
              value={addToken}
              onChange={(e) => setAddToken(e.target.value)}
              className="font-mono"
            />
          </Field>
          <Field>
            <FieldLabel className="sr-only">Label (optional)</FieldLabel>
            <Input
              placeholder="Label (optional)"
              value={addLabel}
              onChange={(e) => setAddLabel(e.target.value)}
            />
          </Field>
        </div>
        <Button
          onClick={() => upsert.mutate()}
          disabled={!canAdd || upsert.isPending}
          className="mt-2"
        >
          {upsert.isPending ? (
            <><CircleNotch className="size-3.5 animate-spin" />Saving…</>
          ) : (
            <><Plus className="size-3.5" />Save token</>
          )}
        </Button>
        {upsert.isError && (
          <p className="text-xs text-destructive mt-2">{upsert.error.message}</p>
        )}
      </div>
    </div>
  )
}

// ── Instance configuration section (owner only) ──────────────────────────────

const CONFIG_FIELDS: {
  key: string
  label: string
  description: string
  type?: string
  options?: { value: string; label: string }[]
}[] = [
  { key: "worker_poll_seconds", label: "Worker Poll Interval",    description: "Seconds between job queue polls.", type: "number" },
  { key: "registration_enabled", label: "Self-Registration",     description: "Allow anyone to create an account via /register.", type: "boolean" },
  {
    key: "digest_cadence",
    label: "Leadership Digest",
    description: "Email org admins a periodic security-score + risk summary. Requires SMTP to be configured.",
    type: "select",
    options: [
      { value: "off", label: "Off" },
      { value: "weekly", label: "Weekly" },
      { value: "monthly", label: "Monthly" },
    ],
  },
]

function InstanceConfigSection() {
  const { data: config, isLoading, isError, error, isFetching, refetch } = useQuery<Record<string, string>>({
    queryKey: ["config"],
    queryFn: api.config.getAll,
  })
  const qc = useQueryClient()
  const [saving, setSaving] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (config) setValues((prev) => initialConfigValues(prev, config))
  }, [config])

  async function saveKey(key: string, explicitValue?: string) {
    setSaving(key)
    setErrors((prev) => ({ ...prev, [key]: "" }))
    try {
      await api.config.update(key, explicitValue ?? values[key] ?? "")
      const updated = await qc.fetchQuery<Record<string, string>>({
        queryKey: ["config"],
        queryFn: api.config.getAll,
      })
      setValues((prev) => mergeSavedConfigValue(prev, updated, key))
    } catch (err) {
      setErrors((prev) => ({ ...prev, [key]: err instanceof Error ? err.message : "Save failed" }))
    } finally {
      setSaving(null)
    }
  }

  if (isLoading) {
    return (
      <div className="card px-4 py-6 flex items-center gap-2 text-sm text-muted-foreground">
        <CircleNotch className="size-3.5 animate-spin" /> Loading config…
      </div>
    )
  }

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-label">Instance configuration</span>
        <p className="text-xs text-muted-foreground mt-0.5">Visible to instance owner only.</p>
      </div>
      {isError && (
        <SectionError
          message={error instanceof Error ? error.message : "Failed to load config."}
          onRetry={() => refetch()}
          retrying={isFetching}
        />
      )}
      <div className="divide-y divide-border">
        {CONFIG_FIELDS.map((field) => {
          const isSavingField = saving === field.key
          const saveContent: React.ReactNode = isSavingField ? <CircleNotch className="size-3 animate-spin" /> : "Save"
          const fieldId = `cfg-${field.key}`
          // The value a select shows before the user touches it / before the key
          // is persisted server-side: its first option ("" for non-select fields).
          const firstOption = field.options?.[0]?.value ?? ""

          return (
          <div key={field.key} className="p-4 max-w-lg">
            <label htmlFor={fieldId} className="text-xs font-medium text-foreground block mb-1">{field.label}</label>
            <div className="flex items-center gap-2">
              {field.type === "boolean" ? (
                <select
                  id={fieldId}
                  value={values[field.key] ?? "true"}
                  onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
                  className="h-8 border border-border bg-transparent px-2 font-mono text-xs"
                >
                  <option value="true">Enabled</option>
                  <option value="false">Disabled</option>
                </select>
              ) : field.type === "select" ? (
                <select
                  id={fieldId}
                  value={values[field.key] ?? firstOption}
                  onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
                  className="h-8 border border-border bg-transparent px-2 font-mono text-xs"
                >
                  {field.options?.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              ) : (
                <Input
                  id={fieldId}
                  value={values[field.key] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
                  type={field.type === "number" ? "number" : "text"}
                  className="font-mono text-xs"
                />
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  saveKey(
                    field.key,
                    // A select with no persisted value shows its first option but has
                    // no entry in `values` yet -- save that visible value, not "".
                    field.type === "select"
                      ? (values[field.key] ?? firstOption)
                      : undefined,
                  )
                }
                disabled={isSavingField}
              >
                {saveContent}
              </Button>
            </div>
            {errors[field.key] && (
              <p className="text-xs text-destructive mt-1">{errors[field.key]}</p>
            )}
            <p className="text-xs text-muted-foreground mt-1">{field.description}</p>
          </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const { user } = useAuth()
  const router = useRouter()
  const searchParams = useSearchParams()
  const [justInstalled, setJustInstalled] = useState(false)

  useEffect(() => {
    if (searchParams.get("installed") === "1") {
      setJustInstalled(true)
      router.replace("/settings")
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <>
      <PageHeader title="Settings" description="Configure your workspace." />
      {justInstalled && (
        <div className="mb-4 px-4 py-2.5 bg-primary/10 border border-primary/30 text-sm text-primary flex items-center gap-1.5">
          <CheckCircle className="size-3.5" /> GitHub App installation connected.
        </div>
      )}
      <div className="flex flex-col gap-4">
        <ProfileSection />
        <AppearanceSection />
        <OrgMembershipsSection />
        <ConnectedOrgsSection />
        <SavedTokensSection />
        {user?.is_workspace_admin && <InstanceConfigSection />}
      </div>
    </>
  )
}

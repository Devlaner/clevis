"use client"

import { useEffect, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api/client"
import type {
  BranchProtectionBulkResponse,
  BranchProtectionPreset,
  RepoSummary,
} from "@/lib/api/types"

// Bulk branch-protection apply (issue #288). Org-admin only; needs `Administration: write`
// on the connected GitHub App. A dry-run diff is always shown before an apply, and the
// apply button is two-step (matching the dispatch / cache-clear buttons) so one click
// can't rewrite protection across every selected repo.

interface Props {
  org: string
  token: string
  repos: RepoSummary[]
}

export function BranchProtectionCard({ org, token, repos }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [reviewCount, setReviewCount] = useState(1)
  const [enforceAdmins, setEnforceAdmins] = useState(false)
  const [blockForcePush, setBlockForcePush] = useState(true)
  const [savePreset, setSavePreset] = useState(false)
  const [applyArmed, setApplyArmed] = useState(false)

  // Selected repos that no longer exist in the list (owner switched) shouldn't linger.
  useEffect(() => {
    const names = new Set(repos.map((r) => r.name))
    setSelected((prev) => new Set([...prev].filter((n) => names.has(n))))
  }, [repos])

  useEffect(() => {
    if (!applyArmed) return
    const t = setTimeout(() => setApplyArmed(false), 4000)
    return () => clearTimeout(t)
  }, [applyArmed])

  const preset = (): BranchProtectionPreset => ({
    required_pull_request_reviews: { required_approving_review_count: reviewCount },
    enforce_admins: enforceAdmins,
    allow_force_pushes: !blockForcePush,
    allow_deletions: false,
    required_status_checks: null,
    restrictions: null,
  })

  const repoList = () => [...selected]

  const preview = useMutation({
    mutationFn: () =>
      api.branchProtection.bulk(org, { repos: repoList(), preset: preset(), dry_run: true, token }),
  })

  const apply = useMutation({
    mutationFn: () =>
      api.branchProtection.bulk(org, {
        repos: repoList(),
        preset: preset(),
        dry_run: false,
        save_preset: savePreset,
        token,
      }),
  })

  // A preview is only valid for the exact repos + knobs it ran against. Apply is
  // blocked until the current selection/knobs match what was previewed, so a bulk
  // rewrite can't hit repos the admin never saw a diff for.
  const previewSig = [...selected].sort().join(",") + `|${reviewCount}|${enforceAdmins}|${blockForcePush}`
  const [previewedSig, setPreviewedSig] = useState<string | null>(null)
  const previewStale = !preview.isSuccess || previewedSig !== previewSig

  const errText = (e: unknown) => (e instanceof Error ? e.message : null)
  let message: string | null = errText(apply.error) ?? errText(preview.error)
  if (!message && apply.data) {
    const results = (apply.data as BranchProtectionBulkResponse).results ?? []
    const applied = results.filter((r) => r.applied).length
    const failed = results.filter((r) => !r.applied)
    message =
      failed.length === 0
        ? `Applied branch protection to ${applied} repositor${applied === 1 ? "y" : "ies"}.`
        : `Applied to ${applied}; ${failed.length} failed: ${failed.map((r) => r.repo).join(", ")}.`
  }

  const diffs = apply.data ? [] : preview.data?.diffs ?? []
  const changing = diffs.filter((d) => d.would_change && !d.error)
  const canSubmit = selected.size > 0 && org.trim().length > 0 && !preview.isPending && !apply.isPending

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-title">Branch protection</span>
        <p className="text-xs text-muted-foreground mt-1">
          Apply a protection preset to each selected repo&rsquo;s default branch. Preview the diff
          first. Needs the GitHub App&rsquo;s <code>Administration: write</code> permission.
        </p>
      </div>

      <div className="p-4 flex flex-col gap-4">
        {repos.length === 0 ? (
          <p className="text-sm text-muted-foreground">Enter an organization above to list its repositories.</p>
        ) : (
          <>
            <fieldset className="flex flex-col gap-1.5 max-h-48 overflow-y-auto border border-border rounded-md p-2">
              <legend className="sr-only">Repositories</legend>
              {repos.map((r) => (
                <label key={r.name} className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={selected.has(r.name)}
                    onChange={(e) => {
                      setSelected((prev) => {
                        const next = new Set(prev)
                        if (e.target.checked) next.add(r.name)
                        else next.delete(r.name)
                        return next
                      })
                    }}
                  />
                  {r.name}
                </label>
              ))}
            </fieldset>

            <div className="flex flex-wrap items-center gap-4 text-xs">
              <label className="flex items-center gap-1.5">
                Required approvals
                <Input
                  type="number"
                  min={0}
                  max={6}
                  value={reviewCount}
                  onChange={(e) => setReviewCount(Math.max(0, Math.min(6, Number(e.target.value) || 0)))}
                  className="h-7 w-16"
                />
              </label>
              <label className="flex items-center gap-1.5">
                <input type="checkbox" checked={blockForcePush} onChange={(e) => setBlockForcePush(e.target.checked)} />
                Block force-pushes
              </label>
              <label className="flex items-center gap-1.5">
                <input type="checkbox" checked={enforceAdmins} onChange={(e) => setEnforceAdmins(e.target.checked)} />
                Enforce on admins
              </label>
              <label className="flex items-center gap-1.5">
                <input type="checkbox" checked={savePreset} onChange={(e) => setSavePreset(e.target.checked)} />
                Save preset per repo
              </label>
            </div>

            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!canSubmit}
                onClick={() => {
                  setApplyArmed(false)
                  apply.reset()
                  setPreviewedSig(previewSig)
                  preview.mutate()
                }}
              >
                {preview.isPending ? "Previewing…" : `Preview changes (${selected.size})`}
              </Button>
              <Button
                size="sm"
                disabled={!canSubmit || previewStale}
                onClick={() => {
                  if (applyArmed) {
                    setApplyArmed(false)
                    apply.mutate()
                  } else {
                    setApplyArmed(true)
                  }
                }}
              >
                {apply.isPending
                  ? "Applying…"
                  : applyArmed
                    ? "Click again to confirm"
                    : `Apply to ${selected.size} repo${selected.size === 1 ? "" : "s"}`}
              </Button>
            </div>

            {message && <p className="text-xs text-muted-foreground">{message}</p>}

            {diffs.length > 0 && (
              <div className="overflow-x-auto border border-border rounded-md">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left text-muted-foreground font-medium px-3 py-2">Repo</th>
                      <th className="text-left text-muted-foreground font-medium px-3 py-2">Branch</th>
                      <th className="text-left text-muted-foreground font-medium px-3 py-2">Change</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {diffs.map((d) => (
                      <tr key={d.repo}>
                        <td className="px-3 py-2 text-foreground/90">{d.repo}</td>
                        <td className="px-3 py-2 text-muted-foreground">{d.branch || "—"}</td>
                        <td className="px-3 py-2 text-muted-foreground">
                          {d.error
                            ? `Error: ${d.error}`
                            : d.would_change
                              ? Object.keys(d.changes).join(", ")
                              : "No change"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {preview.isSuccess && !apply.data && (
              <p className="text-xs text-muted-foreground">
                {changing.length === 0
                  ? "Every selected repo already matches this preset."
                  : `${changing.length} of ${diffs.length} selected repos would change.`}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

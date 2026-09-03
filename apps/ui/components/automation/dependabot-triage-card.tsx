"use client"

import { useEffect, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api/client"
import type { DependabotTriageResponse } from "@/lib/api/types"

// Dependabot auto-triage (issue #290) — the highest-risk automation, so the UI leads
// with the guardrails. Per-repo opt-in (default off). approve_only just approves;
// approve_and_merge is the only mode that merges, and "Run for real" is two-step. Only
// patch-level dependabot[bot] bumps with every check green and no pending human review
// are ever acted on.

type Mode = "approve_only" | "approve_and_merge"

interface Props {
  org: string
  owner: string
  repo: string
  token: string
}

export function DependabotTriageCard({ org, owner, repo, token }: Props) {
  const [enabled, setEnabled] = useState(false)
  const [mode, setMode] = useState<Mode>("approve_only")
  const [mergeMethod, setMergeMethod] = useState("squash")
  const [runArmed, setRunArmed] = useState(false)

  const ready = org.trim().length > 0 && owner.trim().length > 0 && repo.trim().length > 0

  useEffect(() => {
    setRunArmed(false)
  }, [enabled, mode, mergeMethod, repo])

  const save = useMutation({
    mutationFn: () =>
      api.dependabotTriage.setRepo(org.trim(), owner.trim(), repo.trim(), {
        enabled,
        mode,
        merge_method: mergeMethod,
      }),
  })

  const run = useMutation({
    mutationFn: (dryRun: boolean) =>
      api.dependabotTriage.run(org.trim(), { repos: [`${owner.trim()}/${repo.trim()}`], dry_run: dryRun }, token),
  })

  const saveErr = save.error instanceof Error ? save.error.message : null
  const runErr = run.error instanceof Error ? run.error.message : null
  const decisions = (run.data as DependabotTriageResponse | undefined)?.decisions ?? []

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-title">Dependabot auto-triage</span>
        <p className="text-xs text-muted-foreground mt-1">
          Off by default per repo. Only patch-level <code>dependabot[bot]</code> bumps with every
          check green and no pending human review are acted on. <strong>approve_and_merge</strong> is
          the only mode that merges. Needs the App&rsquo;s Pull requests (+ Contents, to merge) write.
        </p>
      </div>

      <div className="p-4 flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-4 text-xs">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Enabled for {repo || "this repo"}
          </label>
          <label className="flex items-center gap-1.5">
            Mode
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as Mode)}
              className="h-7 rounded-md border border-input bg-transparent px-2 text-xs"
            >
              <option value="approve_only">approve_only</option>
              <option value="approve_and_merge">approve_and_merge</option>
            </select>
          </label>
          {mode === "approve_and_merge" && (
            <label className="flex items-center gap-1.5">
              Merge
              <select
                value={mergeMethod}
                onChange={(e) => setMergeMethod(e.target.value)}
                className="h-7 rounded-md border border-input bg-transparent px-2 text-xs"
              >
                <option value="squash">squash</option>
                <option value="merge">merge</option>
                <option value="rebase">rebase</option>
              </select>
            </label>
          )}
          <Button size="sm" variant="outline" disabled={!ready || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save settings"}
          </Button>
        </div>

        {save.isSuccess && <p className="text-xs text-muted-foreground">Settings saved.</p>}
        {saveErr && <p className="text-xs text-red-500">{saveErr}</p>}

        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!ready || run.isPending}
            onClick={() => {
              setRunArmed(false)
              run.mutate(true)
            }}
          >
            {run.isPending ? "Running…" : "Dry run"}
          </Button>
          {mode === "approve_and_merge" && (
            <Button
              size="sm"
              disabled={!ready || run.isPending}
              onClick={() => {
                if (runArmed) {
                  setRunArmed(false)
                  run.mutate(false)
                } else {
                  setRunArmed(true)
                }
              }}
            >
              {runArmed ? "Click again to run for real" : "Run for real"}
            </Button>
          )}
        </div>

        {runErr && <p className="text-xs text-red-500">{runErr}</p>}

        {run.data && !runErr && (
          <div className="overflow-x-auto border border-border rounded-md">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted-foreground font-medium px-3 py-2">PR</th>
                  <th className="text-left text-muted-foreground font-medium px-3 py-2">Decision</th>
                  <th className="text-left text-muted-foreground font-medium px-3 py-2">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {decisions.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="px-3 py-2 text-muted-foreground">
                      No open pull requests to triage.
                    </td>
                  </tr>
                ) : (
                  decisions.map((d, i) => (
                    <tr key={i}>
                      <td className="px-3 py-2 text-foreground/90">{d.number ? `#${d.number}` : d.repo}</td>
                      <td className="px-3 py-2 text-muted-foreground">{d.action}</td>
                      <td className="px-3 py-2 text-muted-foreground">{d.reason || "—"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

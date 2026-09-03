"use client"

import { useMutation } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api/client"
import type { WorkflowLintResponse } from "@/lib/api/types"

// Workflow-policy lint (issue #291). Scans {owner}/{repo}'s .github/workflows for a
// dangerous `pull_request_target` + PR-head-checkout pattern (and untrusted input in
// `run:`). "Open fix PR" is org-admin only and needs Contents/Pull requests/Workflows
// write on the App; a 400 with a docs pointer means it's missing.

interface Props {
  owner: string
  repo: string
  token: string
}

const SEVERITY_STYLE: Record<string, string> = {
  critical: "text-red-500",
  high: "text-orange-400",
  warning: "text-yellow-500",
}

export function WorkflowLintCard({ owner, repo, token }: Props) {
  const scan = useMutation({
    mutationFn: (openPr: boolean) =>
      api.workflowLint.scan(owner.trim(), repo.trim(), { open_pr: openPr }, token),
  })

  const data = scan.data as WorkflowLintResponse | undefined
  const error = scan.error instanceof Error ? scan.error.message : null
  const ready = owner.trim().length > 0 && repo.trim().length > 0

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-border">
        <span className="section-title">Workflow policy lint</span>
        <p className="text-xs text-muted-foreground mt-1">
          Flags <code>pull_request_target</code> workflows that check out untrusted PR code.
          Opening a fix PR needs the App&rsquo;s Contents / Pull requests / Workflows write scopes.
        </p>
      </div>

      <div className="p-4 flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" disabled={!ready || scan.isPending} onClick={() => scan.mutate(false)}>
            {scan.isPending ? "Scanning…" : "Lint workflows"}
          </Button>
          {data?.fixable && (
            <Button size="sm" disabled={scan.isPending} onClick={() => scan.mutate(true)}>
              Open fix PR
            </Button>
          )}
        </div>

        {error && <p className="text-xs text-red-500">{error}</p>}

        {data?.pr_url && (
          <p className="text-xs">
            Fix PR opened:{" "}
            <a href={data.pr_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
              {data.pr_url}
            </a>
          </p>
        )}

        {data && !error && (
          <>
            {data.findings.length === 0 ? (
              <p className="text-xs text-muted-foreground">No policy issues found.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {data.findings.map((f, i) => (
                  <li key={i} className="text-xs">
                    <span className={`font-medium ${SEVERITY_STYLE[f.severity] ?? "text-muted-foreground"}`}>
                      {f.severity}
                    </span>{" "}
                    <span className="font-mono text-foreground/80">{f.path}</span>
                    <p className="text-muted-foreground mt-0.5">{f.message}</p>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  )
}

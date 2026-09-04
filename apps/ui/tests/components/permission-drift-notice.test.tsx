import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { PermissionDriftNotice } from "@/components/permission-drift-notice"
import type { InstallationMeta } from "@/lib/api/types"

function install(overrides: Partial<InstallationMeta> = {}): InstallationMeta {
  return {
    id: 1,
    account_login: "acme",
    account_type: "Organization",
    installation_id: 42,
    created_at: "2026-01-01T00:00:00Z",
    permissions_synced_at: "2026-09-01T00:00:00Z",
    blocked_features: [],
    ...overrides,
  }
}

beforeEach(() => vi.stubEnv("NEXT_PUBLIC_GITHUB_APP_SLUG", "clevis"))
afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe("PermissionDriftNotice", () => {
  it("renders nothing when the install is fully permissioned", () => {
    const { container } = render(<PermissionDriftNotice install={install()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it("lists blocked feature labels and a Review on GitHub link", () => {
    render(
      <PermissionDriftNotice
        install={install({
          blocked_features: [
            { feature: "stale_pr_nudges", label: "Stale pull-request nudges", missing: { pull_requests: "write" } },
            { feature: "bulk_branch_protection", label: "Bulk branch-protection apply", missing: { administration: "write" } },
          ],
        })}
      />,
    )
    expect(screen.getByText(/2 automations need extra GitHub access/i)).toBeInTheDocument()
    expect(screen.getByText("Stale pull-request nudges")).toBeInTheDocument()
    const link = screen.getByRole("link", { name: /review on github/i })
    expect(link).toHaveAttribute("href", expect.stringContaining("/installations/42"))
  })

  it("shows a 'not yet checked' line when permissions have never been observed", () => {
    render(<PermissionDriftNotice install={install({ permissions_synced_at: null })} />)
    expect(screen.getByText(/not yet checked/i)).toBeInTheDocument()
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
  })

  it("uses the singular 'automation' for exactly one blocked feature", () => {
    render(
      <PermissionDriftNotice
        install={install({
          blocked_features: [
            { feature: "stale_pr_nudges", label: "Stale pull-request nudges", missing: { pull_requests: "write" } },
          ],
        })}
      />,
    )
    expect(screen.getByText(/1 automation needs extra GitHub access/i)).toBeInTheDocument()
  })

  it("explains the slug isn't configured instead of a link when the App slug is unset", () => {
    vi.unstubAllEnvs()
    render(
      <PermissionDriftNotice
        install={install({
          blocked_features: [
            { feature: "stale_pr_nudges", label: "Stale pull-request nudges", missing: { pull_requests: "write" } },
          ],
        })}
      />,
    )
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
    expect(screen.getByText(/slug isn.t configured/i)).toBeInTheDocument()
  })
})

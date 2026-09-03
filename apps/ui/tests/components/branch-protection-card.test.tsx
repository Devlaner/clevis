import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mockBulk = vi.fn()

vi.mock("@/lib/api/client", () => ({
  api: { branchProtection: { bulk: (...args: unknown[]) => mockBulk(...args) } },
}))

import { BranchProtectionCard } from "@/components/automation/branch-protection-card"
import type { RepoSummary } from "@/lib/api/types"

function repo(name: string): RepoSummary {
  return {
    name,
    full_name: `acme/${name}`,
    private: false,
    description: null,
    language: null,
    stargazers_count: 0,
    forks_count: 0,
    watchers_count: 0,
    open_issues_count: 0,
    pushed_at: null,
    default_branch: "main",
    html_url: `https://github.com/acme/${name}`,
  }
}

const DRY_RUN_RESP = {
  dry_run: true,
  diffs: [
    {
      repo: "api",
      branch: "main",
      currently_protected: false,
      would_change: true,
      changes: { enforce_admins: { from: null, to: false } },
      error: null,
    },
  ],
}

function renderCard(repos: RepoSummary[] = [repo("api"), repo("web")]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <BranchProtectionCard org="acme" token="" repos={repos} />
    </QueryClientProvider>,
  )
}

describe("BranchProtectionCard", () => {
  beforeEach(() => mockBulk.mockReset())
  afterEach(cleanup)

  it("prompts for an org when there are no repos", () => {
    renderCard([])
    expect(screen.getByText(/Enter an organization above/)).toBeInTheDocument()
  })

  it("previews a dry-run diff for the selected repos, then applies with a two-step confirm", async () => {
    mockBulk
      .mockResolvedValueOnce(DRY_RUN_RESP)
      .mockResolvedValueOnce({ dry_run: false, results: [{ repo: "api", applied: true, error: null }] })
    renderCard()

    fireEvent.click(screen.getByLabelText("api"))
    fireEvent.click(screen.getByRole("button", { name: /Preview changes \(1\)/ }))

    await waitFor(() => expect(screen.getByText("enforce_admins")).toBeInTheDocument())
    expect(mockBulk).toHaveBeenCalledWith("acme", expect.objectContaining({ repos: ["api"], dry_run: true }))

    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 repo" }))
    fireEvent.click(await screen.findByRole("button", { name: "Click again to confirm" }))

    await waitFor(() =>
      expect(screen.getByText(/Applied branch protection to 1 repository\./)).toBeInTheDocument(),
    )
    expect(mockBulk).toHaveBeenLastCalledWith("acme", expect.objectContaining({ dry_run: false }))
  })

  it("disables Apply after the selection changes until a fresh preview runs", async () => {
    mockBulk.mockResolvedValue(DRY_RUN_RESP)
    renderCard()

    fireEvent.click(screen.getByLabelText("api"))
    fireEvent.click(screen.getByRole("button", { name: /Preview changes \(1\)/ }))
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply to 1 repo" })).toBeEnabled())

    // add another repo -> the previous preview no longer covers the selection
    fireEvent.click(screen.getByLabelText("web"))
    expect(screen.getByRole("button", { name: "Apply to 2 repos" })).toBeDisabled()

    // re-preview -> Apply is available again
    fireEvent.click(screen.getByRole("button", { name: /Preview changes \(2\)/ }))
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply to 2 repos" })).toBeEnabled())
  })

  it("says so when every selected repo already matches the preset, and edits the preset", async () => {
    mockBulk.mockResolvedValueOnce({
      dry_run: true,
      diffs: [{ repo: "api", branch: "main", currently_protected: true, would_change: false, changes: {}, error: null }],
    })
    renderCard()
    fireEvent.click(screen.getByLabelText("api"))
    // tweak the preset controls (covers the input handlers)
    fireEvent.change(screen.getByLabelText(/Required approvals/), { target: { value: "3" } })
    fireEvent.click(screen.getByLabelText("Enforce on admins"))
    fireEvent.click(screen.getByLabelText("Block force-pushes"))
    fireEvent.click(screen.getByLabelText("Save preset per repo"))
    fireEvent.click(screen.getByRole("button", { name: /Preview changes/ }))

    await waitFor(() => expect(screen.getByText(/already matches this preset/)).toBeInTheDocument())
    expect(mockBulk).toHaveBeenCalledWith(
      "acme",
      expect.objectContaining({
        preset: expect.objectContaining({
          required_pull_request_reviews: { required_approving_review_count: 3 },
          enforce_admins: true,
          allow_force_pushes: true,
        }),
      }),
    )
  })

  it("drops a selected repo that disappears from the list", () => {
    const { rerender } = renderCard([repo("api"), repo("web")])
    fireEvent.click(screen.getByLabelText("web"))
    expect(screen.getByRole("button", { name: /Preview changes \(1\)/ })).toBeInTheDocument()
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <BranchProtectionCard org="acme" token="" repos={[repo("api")]} />
      </QueryClientProvider>,
    )
    expect(screen.getByRole("button", { name: /Preview changes \(0\)/ })).toBeInTheDocument()
  })

  it("reports partial apply failures", async () => {
    mockBulk.mockResolvedValueOnce(DRY_RUN_RESP).mockResolvedValueOnce({
      dry_run: false,
      results: [
        { repo: "api", applied: true, error: null },
        { repo: "web", applied: false, error: "GitHub API error: 403" },
      ],
    })
    renderCard()
    fireEvent.click(screen.getByLabelText("api"))
    fireEvent.click(screen.getByLabelText("web"))
    fireEvent.click(screen.getByRole("button", { name: /Preview changes/ }))
    await screen.findByRole("button", { name: "Apply to 2 repos" })
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 repos" }))
    fireEvent.click(await screen.findByRole("button", { name: "Click again to confirm" }))
    await waitFor(() => expect(screen.getByText(/1 failed: web\./)).toBeInTheDocument())
  })
})

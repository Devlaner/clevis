import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mockSetRepo = vi.fn()
const mockRun = vi.fn()

vi.mock("@/lib/api/client", () => ({
  api: {
    dependabotTriage: {
      setRepo: (...a: unknown[]) => mockSetRepo(...a),
      run: (...a: unknown[]) => mockRun(...a),
    },
  },
}))

import { DependabotTriageCard } from "@/components/automation/dependabot-triage-card"

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DependabotTriageCard org="acme" owner="acme" repo="api" token="" />
    </QueryClientProvider>,
  )
}

describe("DependabotTriageCard", () => {
  beforeEach(() => {
    mockSetRepo.mockReset()
    mockRun.mockReset()
  })
  afterEach(cleanup)

  it("saves the per-repo setting with the chosen mode and merge method", async () => {
    mockSetRepo.mockResolvedValueOnce({ enabled: true, mode: "approve_and_merge", merge_method: "rebase" })
    renderCard()

    fireEvent.click(screen.getByLabelText(/Enabled for api/))
    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "approve_and_merge" } })
    fireEvent.change(screen.getByLabelText("Merge"), { target: { value: "rebase" } })
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }))

    await waitFor(() => expect(screen.getByText("Settings saved.")).toBeInTheDocument())
    expect(mockSetRepo).toHaveBeenCalledWith("acme", "acme", "api", {
      enabled: true,
      mode: "approve_and_merge",
      merge_method: "rebase",
    })
  })

  it("shows the dry-run decisions and only offers 'Run for real' in approve_and_merge mode", async () => {
    mockRun.mockResolvedValueOnce({
      decisions: [
        { repo: "acme/api", number: 12, title: "Bump lodash", action: "would_approve", reason: "" },
        { repo: "acme/api", number: 13, title: "Bump x", action: "skipped", reason: "not a patch-level bump" },
      ],
    })
    renderCard()

    expect(screen.queryByRole("button", { name: /Run for real/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }))

    await waitFor(() => expect(screen.getByText("would_approve")).toBeInTheDocument())
    expect(screen.getByText("not a patch-level bump")).toBeInTheDocument()
    expect(mockRun).toHaveBeenCalledWith("acme", { repos: ["acme/api"], dry_run: true }, "")

    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "approve_and_merge" } })
    expect(screen.getByRole("button", { name: "Run for real" })).toBeInTheDocument()
  })

  it("requires a second click to run for real", async () => {
    mockRun.mockResolvedValue({ decisions: [] })
    renderCard()
    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "approve_and_merge" } })

    fireEvent.click(screen.getByRole("button", { name: "Run for real" }))
    expect(mockRun).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "Click again to run for real" }))
    await waitFor(() => expect(mockRun).toHaveBeenCalledWith("acme", { repos: ["acme/api"], dry_run: false }, ""))
  })

  it("surfaces a permission error from the run", async () => {
    mockRun.mockRejectedValueOnce(new Error("GitHub returned 403 ... 'Pull requests' ... See docs/self-hosting.md."))
    renderCard()
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }))
    await waitFor(() => expect(screen.getByText(/Pull requests/)).toBeInTheDocument())
  })
})

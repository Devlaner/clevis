import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mockGetRepo = vi.fn()
const mockSetRepo = vi.fn()
const mockRun = vi.fn()

vi.mock("@/lib/api/client", () => ({
  api: {
    dependabotTriage: {
      getRepo: (...a: unknown[]) => mockGetRepo(...a),
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
    mockGetRepo.mockReset()
    mockSetRepo.mockReset()
    mockRun.mockReset()
    mockGetRepo.mockResolvedValue({ enabled: false, mode: "approve_only", merge_method: "squash" })
  })
  afterEach(cleanup)

  it("hydrates the form from the persisted setting", async () => {
    mockGetRepo.mockResolvedValueOnce({ enabled: true, mode: "approve_and_merge", merge_method: "rebase" })
    renderCard()
    await waitFor(() => expect(screen.getByLabelText(/Enabled for api/)).toBeChecked())
    expect(screen.getByLabelText("Mode")).toHaveValue("approve_and_merge")
    expect(screen.getByLabelText("Merge")).toHaveValue("rebase")
    expect(mockGetRepo).toHaveBeenCalledWith("acme", "acme", "api")
  })

  it("saves the per-repo setting with the chosen mode and merge method", async () => {
    mockSetRepo.mockResolvedValueOnce({ enabled: true, mode: "approve_and_merge", merge_method: "rebase" })
    renderCard()
    // let the initial hydration settle before the user edits, so it can't clobber the edit
    await waitFor(() => expect(screen.getByLabelText(/Enabled for api/)).not.toBeChecked())
    await new Promise((r) => setTimeout(r, 0))

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

  it("offers an approve button in approve_only mode (the safer mode isn't dead in the UI)", async () => {
    mockGetRepo.mockResolvedValueOnce({ enabled: true, mode: "approve_only", merge_method: "squash" })
    mockRun.mockResolvedValueOnce({
      decisions: [{ repo: "acme/api", number: 12, title: "Bump lodash", action: "approved", reason: "" }],
    })
    renderCard()
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve eligible PRs" })).toBeEnabled())

    fireEvent.click(screen.getByRole("button", { name: "Approve eligible PRs" }))
    expect(mockRun).not.toHaveBeenCalled() // two-step
    fireEvent.click(screen.getByRole("button", { name: "Click again to confirm" }))
    await waitFor(() => expect(mockRun).toHaveBeenCalledWith("acme", { repos: ["acme/api"], dry_run: false }, ""))
  })

  it("labels the run button for merge mode and shows the dry-run decisions", async () => {
    mockGetRepo.mockResolvedValueOnce({ enabled: true, mode: "approve_and_merge", merge_method: "squash" })
    mockRun.mockResolvedValueOnce({
      decisions: [
        { repo: "acme/api", number: 12, title: "Bump lodash", action: "would_merge", reason: "" },
        { repo: "acme/api", number: 13, title: "Bump x", action: "skipped", reason: "not a patch-level bump" },
      ],
    })
    renderCard()
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve & merge eligible PRs" })).toBeInTheDocument())

    fireEvent.click(screen.getByRole("button", { name: "Dry run" }))
    await waitFor(() => expect(screen.getByText("would_merge")).toBeInTheDocument())
    expect(screen.getByText("not a patch-level bump")).toBeInTheDocument()
    expect(mockRun).toHaveBeenCalledWith("acme", { repos: ["acme/api"], dry_run: true }, "")
  })

  it("disables the approve button until the repo is enabled", async () => {
    renderCard()
    await waitFor(() => expect(mockGetRepo).toHaveBeenCalled())
    expect(screen.getByRole("button", { name: "Approve eligible PRs" })).toBeDisabled()
  })

  it("surfaces a permission error from the run", async () => {
    mockRun.mockRejectedValueOnce(new Error("GitHub returned 403 ... 'Pull requests' ... See docs/self-hosting.md."))
    renderCard()
    await waitFor(() => expect(mockGetRepo).toHaveBeenCalled())
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }))
    await waitFor(() => expect(screen.getByText(/Pull requests/)).toBeInTheDocument())
  })
})

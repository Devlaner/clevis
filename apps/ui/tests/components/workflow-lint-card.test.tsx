import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mockScan = vi.fn()

vi.mock("@/lib/api/client", () => ({
  api: { workflowLint: { scan: (...args: unknown[]) => mockScan(...args) } },
}))

import { WorkflowLintCard } from "@/components/automation/workflow-lint-card"

function renderCard(props: { owner?: string; repo?: string; token?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkflowLintCard owner={props.owner ?? "acme"} repo={props.repo ?? "api"} token={props.token ?? ""} />
    </QueryClientProvider>,
  )
}

describe("WorkflowLintCard", () => {
  beforeEach(() => mockScan.mockReset())
  afterEach(cleanup)

  it("disables the lint button until an owner and repo are set", () => {
    renderCard({ repo: "" })
    expect(screen.getByRole("button", { name: "Lint workflows" })).toBeDisabled()
  })

  it("lists findings from a scan and offers a fix PR when fixable", async () => {
    mockScan.mockResolvedValueOnce({
      findings: [
        { path: ".github/workflows/ci.yml", rule: "pull_request_target_checks_out_pr_code", severity: "critical", message: "runs untrusted PR code" },
      ],
      fixable: true,
      pr_url: null,
    })
    renderCard()
    fireEvent.click(screen.getByRole("button", { name: "Lint workflows" }))

    await waitFor(() => expect(screen.getByText(/runs untrusted PR code/)).toBeInTheDocument())
    expect(screen.getByText("critical")).toBeInTheDocument()
    expect(mockScan).toHaveBeenCalledWith("acme", "acme", "api", { open_pr: false }, "")

    mockScan.mockResolvedValueOnce({ findings: [], fixable: false, pr_url: "https://github.com/acme/api/pull/5" })
    fireEvent.click(screen.getByRole("button", { name: "Open fix PR" }))
    await waitFor(() => expect(screen.getByText(/pull\/5/)).toBeInTheDocument())
    expect(mockScan).toHaveBeenLastCalledWith("acme", "acme", "api", { open_pr: true }, "")
  })

  it("says so when a clean repo has no findings", async () => {
    mockScan.mockResolvedValueOnce({ findings: [], fixable: false, pr_url: null })
    renderCard()
    fireEvent.click(screen.getByRole("button", { name: "Lint workflows" }))
    await waitFor(() => expect(screen.getByText("No policy issues found.")).toBeInTheDocument())
  })

  it("shows a scanning state while the request is in flight", async () => {
    let finish: (v: unknown) => void = () => {}
    mockScan.mockReturnValueOnce(new Promise((res) => { finish = res }))
    renderCard()
    fireEvent.click(screen.getByRole("button", { name: "Lint workflows" }))
    expect(await screen.findByRole("button", { name: "Scanning…" })).toBeInTheDocument()
    finish({ findings: [], fixable: false, pr_url: null })
    await waitFor(() => expect(screen.getByText("No policy issues found.")).toBeInTheDocument())
  })

  it("surfaces an error message from the API and renders an unknown severity plainly", async () => {
    mockScan.mockResolvedValueOnce({
      findings: [{ path: ".github/workflows/x.yml", rule: "r", severity: "info", message: "fyi" }],
      fixable: false,
      pr_url: null,
    })
    const { rerender } = renderCard()
    fireEvent.click(screen.getByRole("button", { name: "Lint workflows" }))
    await waitFor(() => expect(screen.getByText("info")).toBeInTheDocument())

    mockScan.mockRejectedValueOnce(new Error("needs Contents / Pull requests / Workflows write"))
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <WorkflowLintCard owner="acme" repo="api" token="" />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole("button", { name: "Lint workflows" }))
    await waitFor(() => expect(screen.getByText(/Workflows write/)).toBeInTheDocument())
  })
})

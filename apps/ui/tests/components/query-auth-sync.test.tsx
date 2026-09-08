import { Fragment } from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import { afterEach, describe, expect, it, vi } from "vitest"

let mockUser: { id: number } | null = null

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ user: mockUser }),
}))

import { QueryAuthSync } from "@/components/query-auth-sync"

function renderWith(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <QueryAuthSync />
    </QueryClientProvider>,
  )
}

describe("QueryAuthSync", () => {
  afterEach(() => {
    cleanup()
    mockUser = null
  })

  it("renders nothing", () => {
    mockUser = { id: 1 }
    const qc = new QueryClient()
    const { container } = renderWith(qc)
    expect(container).toBeEmptyDOMElement()
  })

  it("does not clear the cache on the first observation (initial mount)", () => {
    mockUser = { id: 1 }
    const qc = new QueryClient()
    const clear = vi.spyOn(qc, "clear")

    renderWith(qc)

    expect(clear).not.toHaveBeenCalled()
  })

  it("does not clear the cache when the identity is unchanged across re-renders", () => {
    mockUser = { id: 7 }
    const qc = new QueryClient()
    const clear = vi.spyOn(qc, "clear")
    const { rerender } = renderWith(qc)

    const redraw = () =>
      rerender(
        <QueryClientProvider client={qc}>
          <QueryAuthSync />
        </QueryClientProvider>,
      )
    redraw()
    redraw()

    expect(clear).not.toHaveBeenCalled()
  })

  it("clears the cache once when the signed-in user changes (account switch)", () => {
    mockUser = { id: 1 }
    const qc = new QueryClient()
    const clear = vi.spyOn(qc, "clear")
    const { rerender } = renderWith(qc)

    mockUser = { id: 2 }
    rerender(
      <QueryClientProvider client={qc}>
        <QueryAuthSync />
      </QueryClientProvider>,
    )

    expect(clear).toHaveBeenCalledTimes(1)
  })

  it("clears the cache on sign-out (user becomes null)", () => {
    mockUser = { id: 7 }
    const qc = new QueryClient()
    const clear = vi.spyOn(qc, "clear")
    const { rerender } = renderWith(qc)

    mockUser = null
    rerender(
      <QueryClientProvider client={qc}>
        <QueryAuthSync />
      </QueryClientProvider>,
    )

    expect(clear).toHaveBeenCalledTimes(1)
  })

  it("does not re-render previous-user data for a query keyed on org alone", async () => {
    // Regression: ["tokens.resolve", org] is not partitioned by user id, and the
    // QueryClient outlives a same-tab account switch. QueryAuthSync empties the cache
    // synchronously on the identity change; layout.tsx/AuthGuard additionally remount the
    // authenticated subtree (mirrored here by the keyed Fragment) so a memoized
    // QueryObserver result can't paint user A's resolved PAT for user B.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    let resolvedToken = "user-A-PAT"
    function TokenConsumer() {
      const { data } = useQuery({
        queryKey: ["tokens.resolve", "acme"],
        queryFn: () => Promise.resolve({ token: resolvedToken }),
      })
      return <span data-testid="token">{data?.token ?? "none"}</span>
    }

    function Tree() {
      return (
        <QueryClientProvider client={qc}>
          <QueryAuthSync />
          <Fragment key={mockUser?.id ?? "anon"}>
            <TokenConsumer />
          </Fragment>
        </QueryClientProvider>
      )
    }

    mockUser = { id: 1 }
    const { rerender } = render(<Tree />)
    await screen.findByText("user-A-PAT")

    // Account switch on the same tab; the backend would now resolve a different token.
    resolvedToken = "user-B-PAT"
    mockUser = { id: 2 }
    rerender(<Tree />)

    // The stale "user-A-PAT" must never be shown after the identity changed.
    expect(screen.getByTestId("token").textContent).not.toBe("user-A-PAT")
    await screen.findByText("user-B-PAT")
  })
})

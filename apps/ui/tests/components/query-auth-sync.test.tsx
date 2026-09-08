import { cleanup, render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
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

  it("does not clear the cache on the first observation", () => {
    mockUser = { id: 1 }
    const qc = new QueryClient()
    const clear = vi.spyOn(qc, "clear")

    renderWith(qc)

    expect(clear).not.toHaveBeenCalled()
  })

  it("clears the cache when the signed-in user changes", () => {
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

  it("clears the cache on sign-out (user becomes null) but not on an unchanged id", () => {
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

    redraw() // same id -> no clear
    expect(clear).not.toHaveBeenCalled()

    mockUser = null
    redraw() // signed out -> clear
    expect(clear).toHaveBeenCalledTimes(1)
  })
})

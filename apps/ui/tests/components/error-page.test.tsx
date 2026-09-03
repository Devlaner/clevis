import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import ErrorPage from "@/app/error";

describe("app/error.tsx (root error boundary)", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders the fallback heading and generic copy (never the raw error message)", () => {
    render(<ErrorPage error={new Error("Cannot read properties of undefined")} reset={vi.fn()} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(
      screen.getByText(/An unexpected error occurred while rendering this page/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Cannot read properties/)).not.toBeInTheDocument();
  });

  it("shows the digest as a reference when present, and omits it otherwise", () => {
    const withDigest = Object.assign(new Error("x"), { digest: "abc123" });
    const { rerender } = render(<ErrorPage error={withDigest} reset={vi.fn()} />);
    expect(screen.getByText("Reference: abc123")).toBeInTheDocument();

    rerender(<ErrorPage error={new Error("x")} reset={vi.fn()} />);
    expect(screen.queryByText(/Reference:/)).not.toBeInTheDocument();
  });

  it("calls reset when 'Try again' is clicked", () => {
    const reset = vi.fn();
    render(<ErrorPage error={new Error("boom")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
  });
});

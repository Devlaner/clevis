import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import GlobalError from "@/app/global-error";

// global-error.tsx renders its own <html>/<body> (it replaces the root layout). React
// warns about that nesting under jsdom's container div; it's expected here, so silence it
// so a real warning elsewhere still stands out.
const originalError = console.error;
beforeAll(() => {
  vi.spyOn(console, "error").mockImplementation((...args) => {
    const msg = String(args[0] ?? "");
    if (msg.includes("<html>") || msg.includes("<body>") || msg.includes("cannot be a child")) return;
    originalError(...args);
  });
});

describe("app/global-error.tsx (root-layout error boundary)", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders the fallback heading and generic copy (never the raw error message)", () => {
    render(<GlobalError error={new Error("layout blew up: undefined is not a function")} reset={vi.fn()} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText(/The app failed to load/)).toBeInTheDocument();
    expect(screen.queryByText(/undefined is not a function/)).not.toBeInTheDocument();
  });

  it("shows the digest as a reference when present, and omits it otherwise", () => {
    const withDigest = Object.assign(new Error("x"), { digest: "deadbeef" });
    const { rerender } = render(<GlobalError error={withDigest} reset={vi.fn()} />);
    expect(screen.getByText("Reference: deadbeef")).toBeInTheDocument();

    rerender(<GlobalError error={new Error("x")} reset={vi.fn()} />);
    expect(screen.queryByText(/Reference:/)).not.toBeInTheDocument();
  });

  it("calls reset when 'Try again' is clicked", () => {
    const reset = vi.fn();
    render(<GlobalError error={new Error("boom")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
  });
});

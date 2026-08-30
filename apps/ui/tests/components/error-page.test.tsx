import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import ErrorPage from "@/app/error";

describe("app/error.tsx (root error boundary)", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders the fallback heading and the error message", () => {
    render(<ErrorPage error={new Error("boom while rendering")} reset={vi.fn()} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText("boom while rendering")).toBeInTheDocument();
  });

  it("shows generic copy when the error has no message", () => {
    render(<ErrorPage error={new Error("")} reset={vi.fn()} />);

    expect(
      screen.getByText("An unexpected error occurred while rendering this page."),
    ).toBeInTheDocument();
  });

  it("calls reset when 'Try again' is clicked", () => {
    const reset = vi.fn();
    render(<ErrorPage error={new Error("boom")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
  });
});

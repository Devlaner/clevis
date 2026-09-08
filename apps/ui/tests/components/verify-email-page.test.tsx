import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let mockSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

const verifyEmailMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    auth: {
      verifyEmail: (...args: unknown[]) => verifyEmailMock(...args),
    },
  },
}));

import VerifyEmailPage from "@/app/verify-email/page";

describe("VerifyEmailPage", () => {
  beforeEach(() => {
    verifyEmailMock.mockReset();
    mockSearchParams = new URLSearchParams();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows a success message once verification succeeds", async () => {
    mockSearchParams = new URLSearchParams({ token: "good-token" });
    verifyEmailMock.mockResolvedValue({ ok: true });

    render(<VerifyEmailPage />);

    await screen.findByText(/your email is verified/i);
    expect(verifyEmailMock).toHaveBeenCalledWith("good-token");
  });

  it("shows the server's error message when verification fails", async () => {
    mockSearchParams = new URLSearchParams({ token: "bad-token" });
    verifyEmailMock.mockRejectedValue(new Error("Invalid or expired verification link"));

    render(<VerifyEmailPage />);

    await screen.findByText(/invalid or expired verification link/i);
  });

  it("shows an error immediately when the URL has no token", async () => {
    render(<VerifyEmailPage />);

    await screen.findByText(/missing its token/i);
    expect(verifyEmailMock).not.toHaveBeenCalled();
  });

  it("submits the new token when the URL token changes", async () => {
    mockSearchParams = new URLSearchParams({ token: "first-token" });
    verifyEmailMock.mockResolvedValue({ ok: true });

    const { rerender } = render(<VerifyEmailPage />);
    await screen.findByText(/your email is verified/i);
    expect(verifyEmailMock).toHaveBeenCalledWith("first-token");

    mockSearchParams = new URLSearchParams({ token: "second-token" });
    rerender(<VerifyEmailPage />);

    await vi.waitFor(() => expect(verifyEmailMock).toHaveBeenCalledWith("second-token"));
  });

  it("submits once a token appears after initially being absent", async () => {
    verifyEmailMock.mockResolvedValue({ ok: true });

    const { rerender } = render(<VerifyEmailPage />);
    await screen.findByText(/missing its token/i);

    mockSearchParams = new URLSearchParams({ token: "late-token" });
    rerender(<VerifyEmailPage />);

    await vi.waitFor(() => expect(verifyEmailMock).toHaveBeenCalledWith("late-token"));
  });

  it("does not resubmit the same token on re-render (Strict Mode replay)", async () => {
    mockSearchParams = new URLSearchParams({ token: "stable-token" });
    verifyEmailMock.mockResolvedValue({ ok: true });

    const { rerender } = render(<VerifyEmailPage />);
    await screen.findByText(/your email is verified/i);

    rerender(<VerifyEmailPage />);
    rerender(<VerifyEmailPage />);

    expect(verifyEmailMock).toHaveBeenCalledTimes(1);
  });
});

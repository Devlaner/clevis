import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const myPrsMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: { resolve: (...args: unknown[]) => tokensResolveMock(...args) },
    analytics: { myPrs: (...args: unknown[]) => myPrsMock(...args) },
  },
}));

import MyPRsPage from "@/app/my/prs/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MyPRsPage />
    </QueryClientProvider>,
  );
}

const PR_ITEM = {
  number: 12,
  title: "Fix bug",
  repository: "acme/api",
  html_url: "https://github.com/acme/api/pull/12",
  updated_at: "2026-07-20T00:00:00Z",
};

describe("MyPRsPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    myPrsMock.mockReset();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows a no-default-org message when nothing is configured", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/No account selected/)).toBeInTheDocument();
    });
    expect(myPrsMock).not.toHaveBeenCalled();
  });

  it("renders PRs for the default org", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myPrsMock.mockResolvedValue({ items: [PR_ITEM], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Fix bug")).toBeInTheDocument());
    expect(myPrsMock).toHaveBeenCalledWith("acme", 1, 25, "ghp_test");
  });

  it("shows a retry option instead of a fake empty state when the query fails", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myPrsMock.mockRejectedValue(new Error("Workspace admin access required"));

    renderPage();

    await waitFor(() => expect(screen.getByText("Workspace admin access required")).toBeInTheDocument());
    expect(screen.queryByText(/No open pull requests/)).not.toBeInTheDocument();

    myPrsMock.mockResolvedValueOnce({ items: [], total_count: 0, page: 1, per_page: 25 });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText(/No open pull requests/)).toBeInTheDocument());
    expect(myPrsMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to a generic message when the rejection isn't an Error instance", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myPrsMock.mockRejectedValue("boom");

    renderPage();

    await waitFor(() => expect(screen.getByText("Failed to load your pull requests.")).toBeInTheDocument());
  });

  it("shows the empty state only when the query genuinely succeeds with no rows", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myPrsMock.mockResolvedValue({ items: [], total_count: 0, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText(/No open pull requests/)).toBeInTheDocument());
  });

  it("still queries via a connected GitHub App installation when there's no saved PAT to resolve", async () => {
    // /tokens/resolve only bridges the legacy saved-PAT path -- a GitHub App installation is
    // resolved server-side independent of it (see resolve_owner_token), so a 404/"no saved
    // token" here must not block the page from loading real data.
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    myPrsMock.mockResolvedValue({ items: [PR_ITEM], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Fix bug")).toBeInTheDocument());
    expect(myPrsMock).toHaveBeenCalledWith("acme", 1, 25, undefined);
  });

  it("shows the analytics endpoint's own error when neither a saved token nor an installation is available", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    myPrsMock.mockRejectedValue(
      new Error("No GitHub App installation found for 'acme' and no token was provided."),
    );

    renderPage();

    await waitFor(() =>
      expect(
        screen.getByText("No GitHub App installation found for 'acme' and no token was provided."),
      ).toBeInTheDocument(),
    );
  });

  it("advances to page 2 and re-queries when Next is clicked", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myPrsMock.mockResolvedValue({ items: [PR_ITEM], total_count: 30, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Fix bug")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(myPrsMock).toHaveBeenCalledWith("acme", 2, 25, "ghp_test"));
  });
});

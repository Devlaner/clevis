import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const myIssuesMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: { resolve: (...args: unknown[]) => tokensResolveMock(...args) },
    analytics: { myIssues: (...args: unknown[]) => myIssuesMock(...args) },
  },
}));

import MyIssuesPage from "@/app/my/issues/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MyIssuesPage />
    </QueryClientProvider>,
  );
}

const ISSUE_ITEM = {
  number: 3,
  title: "Investigate flake",
  repository: "acme/worker",
  html_url: "https://github.com/acme/worker/issues/3",
  updated_at: "2026-07-19T00:00:00Z",
};

describe("MyIssuesPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    myIssuesMock.mockReset();
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
    expect(myIssuesMock).not.toHaveBeenCalled();
  });

  it("renders assigned issues for the default org", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myIssuesMock.mockResolvedValue({ items: [ISSUE_ITEM], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Investigate flake")).toBeInTheDocument());
    expect(myIssuesMock).toHaveBeenCalledWith("acme", 1, 25, "ghp_test");
  });

  it("shows a retry option instead of a fake empty state when the query fails", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myIssuesMock.mockRejectedValue(new Error("Workspace admin access required"));

    renderPage();

    await waitFor(() => expect(screen.getByText("Workspace admin access required")).toBeInTheDocument());
    expect(screen.queryByText(/No assigned issues/)).not.toBeInTheDocument();

    myIssuesMock.mockResolvedValueOnce({ items: [], total_count: 0, page: 1, per_page: 25 });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText(/No assigned issues/)).toBeInTheDocument());
    expect(myIssuesMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to a generic message when the rejection isn't an Error instance", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myIssuesMock.mockRejectedValue("boom");

    renderPage();

    await waitFor(() => expect(screen.getByText("Failed to load your assigned issues.")).toBeInTheDocument());
  });

  it("shows the empty state only when the query genuinely succeeds with no rows", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    myIssuesMock.mockResolvedValue({ items: [], total_count: 0, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText(/No assigned issues/)).toBeInTheDocument());
  });

  it("still queries via a connected GitHub App installation when there's no saved PAT to resolve", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    myIssuesMock.mockResolvedValue({ items: [ISSUE_ITEM], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Investigate flake")).toBeInTheDocument());
    expect(myIssuesMock).toHaveBeenCalledWith("acme", 1, 25, undefined);
  });

  it("shows the analytics endpoint's own error when neither a saved token nor an installation is available", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    myIssuesMock.mockRejectedValue(
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
    myIssuesMock.mockResolvedValue({ items: [ISSUE_ITEM], total_count: 30, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Investigate flake")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(myIssuesMock).toHaveBeenCalledWith("acme", 2, 25, "ghp_test"));
  });
});

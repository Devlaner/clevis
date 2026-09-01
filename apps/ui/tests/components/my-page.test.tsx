import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const myPrsMock = vi.fn();
const myReviewsMock = vi.fn();
const myIssuesMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: { resolve: (...a: unknown[]) => tokensResolveMock(...a) },
    analytics: {
      myPrs: (...a: unknown[]) => myPrsMock(...a),
      myReviews: (...a: unknown[]) => myReviewsMock(...a),
      myIssues: (...a: unknown[]) => myIssuesMock(...a),
    },
  },
}));

const replace = vi.fn();
let tabParam: string | null = null;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(tabParam ? `tab=${tabParam}` : ""),
}));

import MyWorkPage from "@/app/my/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MyWorkPage />
    </QueryClientProvider>,
  );
}

const ROW = {
  number: 12,
  title: "Fix bug",
  repository: "acme/api",
  html_url: "https://github.com/acme/api/pull/12",
  updated_at: "2026-07-20T00:00:00Z",
};

describe("MyWorkPage (tabbed /my)", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    myPrsMock.mockReset();
    myReviewsMock.mockReset();
    myIssuesMock.mockReset();
    replace.mockReset();
    tabParam = null;
    localStorage.clear();
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the no-account empty state when no scope is selected", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/No account selected/)).toBeInTheDocument());
    expect(myPrsMock).not.toHaveBeenCalled();
  });

  it("defaults to the PRs tab and queries my-prs", async () => {
    localStorage.setItem("default_org", "acme");
    myPrsMock.mockResolvedValue({ items: [ROW], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(screen.getByText("Fix bug")).toBeInTheDocument());
    expect(myPrsMock).toHaveBeenCalledWith("acme", 1, 25, "ghp_test");
    expect(myReviewsMock).not.toHaveBeenCalled();
  });

  it("deep-links to the reviews tab via ?tab=reviews", async () => {
    tabParam = "reviews";
    localStorage.setItem("default_org", "acme");
    myReviewsMock.mockResolvedValue({ items: [ROW], total_count: 1, page: 1, per_page: 25 });

    renderPage();

    await waitFor(() => expect(myReviewsMock).toHaveBeenCalledWith("acme", 1, 25, "ghp_test"));
    expect(screen.getByRole("tab", { name: "My Reviews" })).toHaveAttribute("aria-selected", "true");
    expect(myPrsMock).not.toHaveBeenCalled();
  });

  it("routes to ?tab=issues when the Issues tab is clicked", async () => {
    localStorage.setItem("default_org", "acme");
    myPrsMock.mockResolvedValue({ items: [], total_count: 0, page: 1, per_page: 25 });

    renderPage();
    await waitFor(() => expect(myPrsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("tab", { name: "My Issues" }));
    expect(replace).toHaveBeenCalledWith("?tab=issues", { scroll: false });
  });

  it("drops the tab param when returning to the PRs tab", async () => {
    tabParam = "issues";
    localStorage.setItem("default_org", "acme");
    myIssuesMock.mockResolvedValue({ items: [], total_count: 0, page: 1, per_page: 25 });

    renderPage();
    await waitFor(() => expect(myIssuesMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("tab", { name: "My PRs" }));
    expect(replace).toHaveBeenCalledWith("?", { scroll: false });
  });
});

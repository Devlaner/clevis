import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const reposListMock = vi.fn();
const reposPullsMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
    },
    repos: {
      list: (...args: unknown[]) => reposListMock(...args),
      pulls: (...args: unknown[]) => reposPullsMock(...args),
    },
  },
}));

import PullRequestsPage from "@/app/pulls/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PullRequestsPage />
    </QueryClientProvider>,
  );
}

function pull(overrides: Partial<{ number: number; title: string; user: string | null; created_at: string; html_url: string }> = {}) {
  return {
    number: 1,
    title: "Fix the thing",
    user: "octocat",
    created_at: "2026-07-01T00:00:00Z",
    html_url: "https://github.com/acme/demo/pull/1",
    ...overrides,
  };
}

describe("PullRequestsPage", () => {
  beforeEach(() => {
    localStorage.clear();
    tokensResolveMock.mockReset();
    reposListMock.mockReset();
    reposPullsMock.mockReset();
    tokensResolveMock.mockRejectedValue(new Error("no saved token"));
    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });
    reposPullsMock.mockResolvedValue({ repository: "acme/demo", total: 0, pulls: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows a configure prompt when no org is set", async () => {
    renderPage();
    expect(await screen.findByText(/No account selected yet/)).toBeInTheDocument();
    expect(reposListMock).not.toHaveBeenCalled();
  });

  it("shows an empty state when the org has repos but no open pull requests", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [{ name: "demo", full_name: "acme/demo", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/demo" }],
    });

    renderPage();

    await waitFor(() => expect(reposPullsMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));
    expect(await screen.findByText("No open pull requests")).toBeInTheDocument();
  });

  it("aggregates pull requests across every repo in the org, sorted newest first", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 2,
      repos: [
        { name: "api", full_name: "acme/api", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/api" },
        { name: "web", full_name: "acme/web", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/web" },
      ],
    });
    reposPullsMock.mockImplementation((_org: string, _owner: string, repo: string) => {
      if (repo === "api") {
        return Promise.resolve({
          repository: "acme/api",
          total: 1,
          pulls: [pull({ number: 10, title: "Older PR", created_at: "2026-06-01T00:00:00Z", html_url: "https://github.com/acme/api/pull/10" })],
        });
      }
      return Promise.resolve({
        repository: "acme/web",
        total: 1,
        pulls: [pull({ number: 20, title: "Newer PR", created_at: "2026-07-10T00:00:00Z", html_url: "https://github.com/acme/web/pull/20" })],
      });
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/2 total/)).toBeInTheDocument());
    const rows = screen.getAllByRole("row").slice(1).map((r) => r.textContent);
    // Newest first: "Newer PR" (07-10) before "Older PR" (06-01).
    expect(rows[0]).toContain("Newer PR");
    expect(rows[1]).toContain("Older PR");
  });

  it("still renders other repos' pull requests when one repo's fetch fails", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 2,
      repos: [
        { name: "api", full_name: "acme/api", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/api" },
        { name: "web", full_name: "acme/web", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/web" },
      ],
    });
    reposPullsMock.mockImplementation((_org: string, _owner: string, repo: string) => {
      if (repo === "api") return Promise.reject(new Error("GitHub API unreachable"));
      return Promise.resolve({ repository: "acme/web", total: 1, pulls: [pull({ title: "Still shows up" })] });
    });

    renderPage();

    expect(await screen.findByText(/Still shows up/)).toBeInTheDocument();
  });

  it("shows an error with retry when the repo list fails to load", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    expect(await screen.findByText("GitHub API unreachable")).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: /retry/i });
    expect(retryButton).toBeInTheDocument();

    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });
    fireEvent.click(retryButton);

    await waitFor(() => expect(screen.getByText("No open pull requests")).toBeInTheDocument());
  });

  it("shows a generic error message when the repo list rejects with a non-Error value", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockRejectedValue("string rejection, not an Error instance");

    renderPage();

    expect(await screen.findByText("Failed to load repositories.")).toBeInTheDocument();
  });

  it("falls back to 'unknown' when a pull request has no author", async () => {
    localStorage.setItem("default_org", "acme");
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [{ name: "demo", full_name: "acme/demo", private: false, description: null, language: null, stargazers_count: 0, forks_count: 0, watchers_count: 0, open_issues_count: 0, pushed_at: null, default_branch: "main", html_url: "https://github.com/acme/demo" }],
    });
    reposPullsMock.mockResolvedValue({ repository: "acme/demo", total: 1, pulls: [pull({ user: null })] });

    renderPage();

    expect(await screen.findByText("unknown")).toBeInTheDocument();
  });
});

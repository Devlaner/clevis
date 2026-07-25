import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const tokensUpsertMock = vi.fn();
const reposListMock = vi.fn();
const reposStatsMock = vi.fn();
const reposPullsMock = vi.fn();
const installationsListMock = vi.fn();
const installationsListForOrgMock = vi.fn();

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
      upsert: (...args: unknown[]) => tokensUpsertMock(...args),
    },
    repos: {
      list: (...args: unknown[]) => reposListMock(...args),
      stats: (...args: unknown[]) => reposStatsMock(...args),
      pulls: (...args: unknown[]) => reposPullsMock(...args),
    },
    installations: {
      list: (...args: unknown[]) => installationsListMock(...args),
      listForOrg: (...args: unknown[]) => installationsListForOrgMock(...args),
    },
  },
}));

import ReposPage from "@/app/repos/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReposPage />
    </QueryClientProvider>,
  );
}

describe("ReposPage", () => {
  beforeEach(() => {
    localStorage.clear();
    tokensResolveMock.mockReset();
    tokensUpsertMock.mockReset();
    reposListMock.mockReset();
    reposStatsMock.mockReset();
    reposPullsMock.mockReset();
    tokensResolveMock.mockRejectedValue(new Error("no saved token"));
    reposStatsMock.mockResolvedValue({
      repository: "acme/demo",
      commit_activity: [],
      participation: {},
      contributors: [],
      stargazers_count: 3,
      forks_count: 0,
      watchers_count: 3,
      open_issues_count: 1,
      default_branch: "main",
      latest_release: null,
    });
    reposPullsMock.mockResolvedValue({ repository: "acme/demo", total: 0, pulls: [] });
    installationsListMock.mockReset();
    installationsListMock.mockResolvedValue([]);
    installationsListForOrgMock.mockReset();
    installationsListForOrgMock.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps Load repositories disabled until an org is entered", () => {
    renderPage();
    expect(screen.getByRole("button", { name: /load repositories/i })).toBeDisabled();
  });

  it("hides the GitHub Token field when an installation covers the entered org", async () => {
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "acme", account_type: "Organization", installation_id: 42, created_at: "2026-07-20T00:00:00Z" },
    ]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(screen.queryByText("GitHub Token")).not.toBeInTheDocument();
    });
  });

  it("hides the GitHub Token field when an installation covers the entered org with trailing whitespace", async () => {
    // Regression test (CodeRabbit finding on PR #299): the personal-installation match
    // compared account_login against raw owner state, so "acme " (untrimmed) never
    // matched an "acme" installation and incorrectly left the token field visible.
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "acme", account_type: "Organization", installation_id: 42, created_at: "2026-07-20T00:00:00Z" },
    ]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme " } });
    await waitFor(() => {
      expect(screen.queryByText("GitHub Token")).not.toBeInTheDocument();
    });
  });

  it("hides the GitHub Token field when an org-level installation covers the entered org", async () => {
    // Regression test: api.installations.list() only ever returns the caller's *personal*
    // installations -- an org's App installation must be checked via the separate
    // org-scoped endpoint, or this would never hide the field for the primary (org) case.
    installationsListForOrgMock.mockResolvedValue([
      { id: 2, account_login: "acme", account_type: "Organization", installation_id: 99, created_at: "2026-07-20T00:00:00Z" },
    ]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(installationsListForOrgMock).toHaveBeenCalledWith("acme");
      expect(screen.queryByText("GitHub Token")).not.toBeInTheDocument();
    });
  });

  it("still shows the GitHub Token field when the org-installation lookup errors (e.g. not a recognized org member)", async () => {
    installationsListForOrgMock.mockRejectedValue(new Error("403"));
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(screen.getByText("GitHub Token")).toBeInTheDocument();
    });
  });

  it("keeps Load repositories disabled for a whitespace-only organization", () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: /load repositories/i })).toBeDisabled();
  });

  it("loads and renders repos for the entered org", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: null,
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const loadButton = screen.getByRole("button", { name: /load repositories/i });
    await waitFor(() => expect(loadButton).not.toBeDisabled());
    fireEvent.click(loadButton);

    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());
    expect(reposListMock).toHaveBeenCalledWith("acme", "");

    // Per-visible-row lazy fetches fire once the list is loaded.
    await waitFor(() => expect(reposStatsMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));
    await waitFor(() => expect(reposPullsMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));
  });

  it("shows an empty state when no repos match the name filter", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: null,
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));
    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Filter by name…"), { target: { value: "nonexistent" } });

    await waitFor(() => expect(screen.getByText(/no repositories match your filter/i)).toBeInTheDocument());
  });

  it("filters against the full org repo list, not just a first page of results (issue #220 regression)", async () => {
    // The backend's /orgs/{org}/repos endpoint already exhausts every GitHub API page
    // (see apps/api/src/services/github_client.py request_paginated) before responding, so
    // listMutation.data.repos here represents the org's *complete* repo list in one shot --
    // there's no separate "load more" page for the client-side filter to miss. Simulate an
    // org large enough to have spanned multiple 100-per-page GitHub responses by including a
    // repo past index 100, and assert the name filter still finds it.
    const repos = Array.from({ length: 150 }, (_, i) => ({
      name: `repo-${i}`,
      full_name: `acme/repo-${i}`,
      private: false,
      description: null,
      language: null,
      stargazers_count: 0,
      forks_count: 0,
      watchers_count: 0,
      open_issues_count: 0,
      pushed_at: "2026-07-01T00:00:00Z",
      default_branch: "main",
      html_url: `https://github.com/acme/repo-${i}`,
    }));
    reposListMock.mockResolvedValue({ org: "acme", total: 150, repos });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));
    await waitFor(() => expect(screen.getByText("repo-0")).toBeInTheDocument());

    // repo-149 would only exist on GitHub's second 100-per-page response -- confirms the
    // filter searches the entire fetched list, not a truncated first page of it.
    fireEvent.change(screen.getByPlaceholderText("Filter by name…"), { target: { value: "repo-149" } });

    await waitFor(() => expect(screen.getByText("repo-149")).toBeInTheDocument());
    expect(screen.queryByText("repo-0")).not.toBeInTheDocument();
  });

  it("shows an error message when loading repos fails", async () => {
    reposListMock.mockRejectedValue(new Error("No GitHub App installation found"));

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    await waitFor(() =>
      expect(screen.getByText(/no github app installation found/i)).toBeInTheDocument(),
    );
  });

  it("seeds the org field from the saved default_org", () => {
    localStorage.setItem("default_org", "acme");
    renderPage();
    expect(screen.getByPlaceholderText("e.g. octocat")).toHaveValue("acme");
  });

  it("shows the loading skeleton while the list request is in flight", async () => {
    let resolveList: (v: unknown) => void = () => {};
    reposListMock.mockReturnValue(new Promise((resolve) => { resolveList = resolve; }));

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    expect(await screen.findByText(/loading…/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /loading…/i })).toBeDisabled();

    resolveList({ org: "acme", total: 0, repos: [] });
    await waitFor(() => expect(screen.queryByText(/loading…/i)).not.toBeInTheDocument());
  });

  it("triggers a load on Enter in the organization field", async () => {
    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });

    renderPage();

    const orgInput = screen.getByPlaceholderText("e.g. octocat");
    fireEvent.change(orgInput, { target: { value: "acme" } });
    fireEvent.keyDown(orgInput, { key: "Enter" });

    await waitFor(() => expect(reposListMock).toHaveBeenCalledWith("acme", ""));
  });

  it("shows a plain empty state (no filter hint) when the org has zero repos", async () => {
    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    await waitFor(() => expect(screen.getByText("No repositories match")).toBeInTheDocument());
  });

  it("auto-applies a resolved saved token and hides the save-token button", async () => {
    tokensResolveMock.mockReset();
    tokensResolveMock.mockResolvedValue({ token: "ghp_saved_token_1234567890123456" });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    await waitFor(() => expect(screen.getByPlaceholderText(/leave blank/i)).toHaveValue("ghp_saved_token_1234567890123456"));
    expect(screen.getByText(/saved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save token for this org/i })).not.toBeInTheDocument();
  });

  it("offers to save a manually-entered token and calls upsert on click", async () => {
    tokensUpsertMock.mockResolvedValue({ org: "acme", label: null, created_at: "", updated_at: "" });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => expect(tokensResolveMock).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/leave blank/i), {
      target: { value: "ghp_manual_token_1234567890123456" },
    });

    const saveButton = await screen.findByRole("button", { name: /save token for this org/i });
    fireEvent.click(saveButton);

    await waitFor(() =>
      expect(tokensUpsertMock).toHaveBeenCalledWith("acme", "ghp_manual_token_1234567890123456"),
    );
  });

  it("renders the private-repo lock indicator, star count, and resolved PR count", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "secret-repo",
          full_name: "acme/secret-repo",
          private: true,
          description: null,
          language: null,
          stargazers_count: 42,
          forks_count: 2,
          watchers_count: 42,
          open_issues_count: 5,
          pushed_at: null,
          default_branch: "main",
          html_url: "https://github.com/acme/secret-repo",
        },
      ],
    });
    reposPullsMock.mockResolvedValue({ repository: "acme/secret-repo", total: 3, pulls: [] });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    await waitFor(() => expect(screen.getByText("secret-repo")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
    expect(screen.getByText("42")).toBeInTheDocument();
    // No language and no pushed_at both fall back to the em-dash placeholder.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("renders a sparkline instead of the no-activity message when a repo has recent commits", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: null,
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });
    reposStatsMock.mockResolvedValue({
      repository: "acme/demo",
      commit_activity: Array.from({ length: 8 }, (_, i) => ({ week: i, total: i + 1, days: [] })),
      participation: {},
      contributors: [],
      stargazers_count: 3,
      forks_count: 0,
      watchers_count: 3,
      open_issues_count: 1,
      default_branch: "main",
      latest_release: null,
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText(/no recent activity/i)).not.toBeInTheDocument());
  });

  it("keeps rendered rows scoped to the org they were loaded for, even if the org field is edited afterward without reloading", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: null,
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));
    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());
    await waitFor(() => expect(reposStatsMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));

    reposStatsMock.mockClear();
    reposPullsMock.mockClear();

    // Edit the org field without clicking "Load repositories" again — the still-rendered
    // "acme" row must keep using "acme" for its lazy fetches and cache link, not "other-org".
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "other-org" } });

    expect(screen.getByRole("link", { name: /cache/i })).toHaveAttribute("href", "/repos/acme~demo/cache");
    expect(reposStatsMock).not.toHaveBeenCalledWith("other-org", "other-org", "demo", "");
    expect(reposPullsMock).not.toHaveBeenCalledWith("other-org", "other-org", "demo", "");
  });

  it("triggers a load on Enter in the token field", async () => {
    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const tokenInput = screen.getByPlaceholderText(/leave blank/i);
    fireEvent.keyDown(tokenInput, { key: "Enter" });

    await waitFor(() => expect(reposListMock).toHaveBeenCalledWith("acme", ""));
  });

  it("renders a repo's description, truncated, under its name", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: "A demo repository for testing",
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });

    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    expect(await screen.findByText("A demo repository for testing")).toBeInTheDocument();
  });

  it("reorders rows when the Sort control is changed", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 2,
      repos: [
        {
          name: "zebra",
          full_name: "acme/zebra",
          private: false,
          description: null,
          language: "Go",
          stargazers_count: 1,
          forks_count: 0,
          watchers_count: 1,
          open_issues_count: 0,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/zebra",
        },
        {
          name: "alpha",
          full_name: "acme/alpha",
          private: false,
          description: null,
          language: "Go",
          stargazers_count: 99,
          forks_count: 0,
          watchers_count: 99,
          open_issues_count: 0,
          pushed_at: "2026-06-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/alpha",
        },
      ],
    });

    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));
    await waitFor(() => expect(screen.getByText("zebra")).toBeInTheDocument());

    const rowNames = () => screen.getAllByRole("row").slice(1).map((r) => r.textContent);
    // Default sort is "pushed" (most recent first) — zebra (07-01) before alpha (06-01).
    expect(rowNames()[0]).toContain("zebra");

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "stars" } });
    // Sorted by stars descending — alpha (99) now first.
    await waitFor(() => expect(rowNames()[0]).toContain("alpha"));

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "name" } });
    // Alphabetical — alpha before zebra.
    await waitFor(() => expect(rowNames()[0]).toContain("alpha"));
  });

  it("percent-encodes the org and repo name in navigation links (CodeQL js/xss-through-dom fix)", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "weird name",
          full_name: "acme/weird name",
          private: false,
          description: null,
          language: null,
          stargazers_count: 0,
          forks_count: 0,
          watchers_count: 0,
          open_issues_count: 0,
          pushed_at: null,
          default_branch: "main",
          html_url: "https://github.com/acme/weird%20name",
        },
      ],
    });

    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));

    const repoLink = await screen.findByRole("link", { name: "weird name" });
    expect(repoLink).toHaveAttribute("href", "/repos/acme~weird%20name");

    const cacheLink = screen.getByRole("link", { name: /cache/i });
    expect(cacheLink).toHaveAttribute("href", "/repos/acme~weird%20name/cache");
  });

  it("shows a distinct error indicator, not an empty state, when a row's stats/pulls fetch fails", async () => {
    reposListMock.mockResolvedValue({
      org: "acme",
      total: 1,
      repos: [
        {
          name: "demo",
          full_name: "acme/demo",
          private: false,
          description: null,
          language: "Python",
          stargazers_count: 3,
          forks_count: 0,
          watchers_count: 3,
          open_issues_count: 1,
          pushed_at: "2026-07-01T00:00:00Z",
          default_branch: "main",
          html_url: "https://github.com/acme/demo",
        },
      ],
    });
    reposStatsMock.mockRejectedValue(new Error("GitHub API unreachable"));
    reposPullsMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /load repositories/i }));
    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());

    // Regression: a failed fetch must not render identically to "no activity"/"0 PRs".
    await waitFor(() => expect(screen.getAllByText(/failed to load/i).length).toBeGreaterThan(0));
    expect(screen.queryByText(/no recent activity/i)).not.toBeInTheDocument();
  });
});

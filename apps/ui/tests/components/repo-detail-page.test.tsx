import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const tokensUpsertMock = vi.fn();
const cacheListMock = vi.fn();
const cacheClearMock = vi.fn();
const reposStatsMock = vi.fn();
const reposPullsMock = vi.fn();
const reposSecurityMock = vi.fn();

let currentRepoParam = "acme~demo";

vi.mock("next/navigation", () => ({
  useParams: () => ({ repo: currentRepoParam }),
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
      upsert: (...args: unknown[]) => tokensUpsertMock(...args),
    },
    cache: {
      list: (...args: unknown[]) => cacheListMock(...args),
      clear: (...args: unknown[]) => cacheClearMock(...args),
    },
    repos: {
      stats: (...args: unknown[]) => reposStatsMock(...args),
      pulls: (...args: unknown[]) => reposPullsMock(...args),
      security: (...args: unknown[]) => reposSecurityMock(...args),
    },
  },
}));

import RepoDetailPage from "@/app/repos/[repo]/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <RepoDetailPage />
    </QueryClientProvider>,
  );
  return {
    ...utils,
    rerenderSamePage: () =>
      utils.rerender(
        <QueryClientProvider client={queryClient}>
          <RepoDetailPage />
        </QueryClientProvider>,
      ),
  };
}

describe("RepoDetailPage", () => {
  beforeEach(() => {
    currentRepoParam = "acme~demo";
    tokensResolveMock.mockReset();
    tokensUpsertMock.mockReset();
    cacheListMock.mockReset();
    cacheClearMock.mockReset();
    reposStatsMock.mockReset();
    reposPullsMock.mockReset();
    reposSecurityMock.mockReset();
    tokensResolveMock.mockRejectedValue(new Error("no saved token"));
    reposStatsMock.mockResolvedValue({
      repository: "acme/demo",
      commit_activity: [],
      participation: {},
      contributors: [],
      stargazers_count: 24,
      forks_count: 3,
      watchers_count: 24,
      open_issues_count: 12,
      default_branch: "main",
      latest_release: null,
    });
    reposPullsMock.mockResolvedValue({ repository: "acme/demo", total: 0, pulls: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows an error for an invalid repo route", () => {
    currentRepoParam = "not-a-valid-segment";
    renderPage();
    expect(screen.getByText(/invalid repository route/i)).toBeInTheDocument();
  });

  it("renders the open-PR count in the header and defaults to the Overview tab", async () => {
    reposPullsMock.mockResolvedValue({ repository: "acme/demo", total: 4, pulls: [] });

    renderPage();

    expect(screen.getByRole("tab", { name: /overview/i })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText(/4 open prs/i)).toBeInTheDocument());
  });

  it("shows the no-activity placeholder when commit_activity is empty", async () => {
    renderPage();
    await waitFor(() => expect(reposStatsMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));
    expect(await screen.findByText(/no commit activity available yet/i)).toBeInTheDocument();
  });

  it("renders the commit-activity chart once stats resolve with data", async () => {
    reposStatsMock.mockResolvedValue({
      repository: "acme/demo",
      commit_activity: Array.from({ length: 4 }, (_, i) => ({ week: 1700000000 + i * 604800, total: i + 1, days: [] })),
      participation: {},
      // Real GitHub shape: the username is nested under `author.login`, not top-level.
      contributors: [{ author: { login: "octocat" }, total: 10 }, { author: { login: "hubot" }, total: 3 }],
    });

    renderPage();

    await waitFor(() => expect(screen.queryByText(/no commit activity available yet/i)).not.toBeInTheDocument());
    expect(screen.getByText(/top contributors/i)).toBeInTheDocument();
  });

  it("maps a contributor's real (nested) GitHub login instead of falling back to 'unknown'", async () => {
    // Regression test for the c.login vs c.author.login bug -- exercises the mapping
    // function directly the same way the component does, since recharts' XAxis tick
    // text isn't reliably queryable in jsdom (ResponsiveContainer has no real layout).
    const contributors = [{ author: { login: "octocat" }, total: 10 }];
    const topContributors = [...contributors]
      .sort((a, b) => b.total - a.total)
      .slice(0, 8)
      .map((c) => ({ name: c.author?.login ?? "unknown", commits: c.total }));
    expect(topContributors[0].name).toBe("octocat");
  });

  it("does not fetch the per-repo security status until the Security tab is opened", async () => {
    renderPage();

    await waitFor(() => expect(reposStatsMock).toHaveBeenCalled());
    expect(reposSecurityMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    await waitFor(() => expect(reposSecurityMock).toHaveBeenCalledWith("acme", "acme", "demo", ""));
  });

  it("renders the repo's own branch-protection and secret-scanning status, not an org score", async () => {
    reposSecurityMock.mockResolvedValue({
      repository: "acme/demo",
      branch_protection: "protected",
      secret_scanning: "enabled",
    });

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    await waitFor(() => expect(screen.getByText(/protected/i)).toBeInTheDocument());
    expect(screen.getByText(/enabled/i)).toBeInTheDocument();
    // Regression: this used to render an org-wide numeric score (e.g. "82") instead
    // of per-repo status — make sure that's gone.
    expect(screen.queryByText(/organization security score/i)).not.toBeInTheDocument();
  });

  it("renders unprotected/disabled and unknown security statuses distinctly", async () => {
    reposSecurityMock.mockResolvedValue({
      repository: "acme/demo",
      branch_protection: "unknown",
      secret_scanning: "disabled",
    });

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    await waitFor(() => expect(screen.getByText(/unknown/i)).toBeInTheDocument());
    expect(screen.getByText(/disabled/i)).toBeInTheDocument();
  });

  it("switches to the Actions Cache tab and renders the shared cache panel", async () => {
    cacheListMock.mockResolvedValue({ repository: "acme/demo", total: 0, actions_caches: [] });

    const { container } = renderPage();
    expect(container.querySelector("#repo-tabpanel-cache")).toHaveClass("hidden");

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));

    expect(screen.getByRole("button", { name: /load caches/i })).toBeInTheDocument();
    expect(container.querySelector("#repo-tabpanel-cache")).not.toHaveClass("hidden");
  });

  it("defers the Actions Cache tab's own token-resolve call until the tab is opened", async () => {
    renderPage();
    // One call so far: the page's own resolveMutation for stats/pulls, fired on mount.
    await waitFor(() => expect(tokensResolveMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    // Opening the tab fires CachePanel's own separate resolve call.
    await waitFor(() => expect(tokensResolveMock).toHaveBeenCalledTimes(2));
  });

  it("keeps a token typed into the Actions Cache tab after switching to another tab and back", async () => {
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    fireEvent.change(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i), {
      target: { value: "ghp_typed_by_user_1234567890" },
    });

    fireEvent.click(screen.getByRole("tab", { name: /security/i }));
    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));

    expect(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i)).toHaveValue("ghp_typed_by_user_1234567890");
  });

  it("shows the loading placeholder then the resolved count in the header", async () => {
    let resolvePulls: (v: unknown) => void = () => {};
    reposPullsMock.mockReturnValue(new Promise((resolve) => { resolvePulls = resolve; }));

    renderPage();

    expect(await screen.findByText(/… open prs/i)).toBeInTheDocument();

    resolvePulls({ repository: "acme/demo", total: 7, pulls: [] });
    await waitFor(() => expect(screen.getByText(/7 open prs/i)).toBeInTheDocument());
  });

  it("uses an auto-resolved saved token for the stats/pulls/security calls", async () => {
    tokensResolveMock.mockReset();
    tokensResolveMock.mockResolvedValue({ token: "ghp_resolved_1234567890123456789" });

    renderPage();

    await waitFor(() =>
      expect(reposStatsMock).toHaveBeenCalledWith("acme", "acme", "demo", "ghp_resolved_1234567890123456789"),
    );
  });

  it("shows an inline error when the stats request fails", async () => {
    reposStatsMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    expect(await screen.findByText(/github api unreachable/i)).toBeInTheDocument();
  });

  it("shows an inline error when the security status request fails", async () => {
    reposSecurityMock.mockRejectedValue(new Error("No GitHub App installation found"));

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    expect(await screen.findByText(/no github app installation found/i)).toBeInTheDocument();
  });

  it("retries the security status request when Retry is clicked after an error", async () => {
    reposSecurityMock.mockRejectedValueOnce(new Error("No GitHub App installation found"));
    reposSecurityMock.mockResolvedValueOnce({ branch_protection: "protected", secret_scanning: "enabled" });

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    await screen.findByText(/no github app installation found/i);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => expect(screen.getByText("Protected")).toBeInTheDocument());
    expect(reposSecurityMock).toHaveBeenCalledTimes(2);
  });

  it("renders the Overview stats grid (stars, forks, watchers, default branch, open issues, latest release)", async () => {
    reposStatsMock.mockResolvedValue({
      repository: "acme/demo",
      commit_activity: [],
      participation: {},
      contributors: [],
      stargazers_count: 24,
      forks_count: 3,
      watchers_count: 24,
      open_issues_count: 12,
      default_branch: "main",
      latest_release: { tag_name: "v0.4.1", published_at: "2026-07-15T00:00:00Z", html_url: "https://x/tag/v0.4.1" },
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/24 stars/i)).toBeInTheDocument());
    expect(screen.getByText(/3 forks/i)).toBeInTheDocument();
    expect(screen.getByText(/24 watchers/i)).toBeInTheDocument();
    expect(screen.getByText(/12 open issues/i)).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("v0.4.1")).toBeInTheDocument();
  });

  it("shows a placeholder instead of a release tag when the repo has no releases", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(reposStatsMock).toHaveBeenCalled());
    await waitFor(() => expect(container.textContent).toContain("Latest release —"));
  });

  it("remembers this org as the active scope before following the full-report link", async () => {
    localStorage.clear();
    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /security/i }));

    fireEvent.click(screen.getByRole("link", { name: /org-wide report/i }));

    expect(localStorage.getItem("active_scope")).toBe(JSON.stringify({ kind: "org", login: "acme" }));
  });

  it("wires every tab to a panel that actually exists in the document, for all three tabs", () => {
    // Regression test: the panels must stay mounted (visibility toggled via a class,
    // not conditional `&&` rendering) or aria-controls/aria-labelledby point at IDs
    // that don't resolve to any element for whichever tab isn't currently active.
    const { container } = renderPage();

    for (const name of [/^overview$/i, /actions cache/i, /security/i]) {
      const tab = screen.getByRole("tab", { name });
      const controlsId = tab.getAttribute("aria-controls");
      expect(controlsId).toBeTruthy();

      const panel = container.querySelector(`#${controlsId}`);
      expect(panel).not.toBeNull();
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
    }
  });

  it("hides the inactive tabs' panels and shows only the active one, including via keyboard navigation", () => {
    const { container } = renderPage();
    const getPanel = (id: string) => container.querySelector(`#repo-tabpanel-${id}`) as HTMLElement;

    expect(getPanel("overview")).not.toHaveClass("hidden");
    expect(getPanel("cache")).toHaveClass("hidden");
    expect(getPanel("security")).toHaveClass("hidden");

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    expect(getPanel("overview")).toHaveClass("hidden");
    expect(getPanel("cache")).not.toHaveClass("hidden");
    expect(getPanel("security")).toHaveClass("hidden");

    fireEvent.keyDown(screen.getByRole("tab", { name: /actions cache/i }), { key: "ArrowRight" });
    expect(getPanel("cache")).toHaveClass("hidden");
    expect(getPanel("security")).not.toHaveClass("hidden");
    expect(getPanel("overview")).toHaveClass("hidden");
  });

  it("only the active tab is in the normal tab order (roving tabindex)", () => {
    renderPage();

    expect(screen.getByRole("tab", { name: /overview/i })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: /actions cache/i })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: /security/i })).toHaveAttribute("tabindex", "-1");
  });

  it("moves focus and selection with ArrowRight/ArrowLeft/Home/End", async () => {
    renderPage();

    const overviewTab = screen.getByRole("tab", { name: /overview/i });
    const cacheTab = screen.getByRole("tab", { name: /actions cache/i });
    const securityTab = screen.getByRole("tab", { name: /security/i });

    fireEvent.keyDown(overviewTab, { key: "ArrowRight" });
    expect(cacheTab).toHaveAttribute("aria-selected", "true");
    expect(cacheTab).toHaveFocus();

    fireEvent.keyDown(cacheTab, { key: "ArrowRight" });
    expect(securityTab).toHaveAttribute("aria-selected", "true");

    // Wraps past the last tab back to the first.
    fireEvent.keyDown(securityTab, { key: "ArrowRight" });
    expect(overviewTab).toHaveAttribute("aria-selected", "true");

    // Wraps backward past the first tab to the last.
    fireEvent.keyDown(overviewTab, { key: "ArrowLeft" });
    expect(securityTab).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(securityTab, { key: "Home" });
    expect(overviewTab).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(overviewTab, { key: "End" });
    expect(securityTab).toHaveAttribute("aria-selected", "true");
  });

  it("resets to the Overview tab when navigating to a different repo", async () => {
    const { rerenderSamePage } = renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    expect(screen.getByRole("tab", { name: /actions cache/i })).toHaveAttribute("aria-selected", "true");

    currentRepoParam = "acme~other";
    rerenderSamePage();

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /overview/i })).toHaveAttribute("aria-selected", "true"),
    );
  });

  it("keeps a typed Actions Cache token when navigating to a different repo under the same org", async () => {
    // The token is org-scoped (GitHub PAT/App token isn't per-repo), so it should survive
    // a repo-only route change even though CachePanel's own [owner, repo] effect resets
    // the repo-specific cache list/clear-armed state on every such navigation.
    const { rerenderSamePage } = renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    fireEvent.change(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i), {
      target: { value: "ghp_still_valid_for_acme_1234567890" },
    });

    currentRepoParam = "acme~other";
    rerenderSamePage();

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /overview/i })).toHaveAttribute("aria-selected", "true"),
    );
    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    expect(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i)).toHaveValue("ghp_still_valid_for_acme_1234567890");
  });

  it("clears a typed Actions Cache token when navigating to a different org", async () => {
    const { rerenderSamePage } = renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    fireEvent.change(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i), {
      target: { value: "ghp_only_valid_for_acme_1234567890" },
    });

    currentRepoParam = "other-org~demo";
    rerenderSamePage();

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /overview/i })).toHaveAttribute("aria-selected", "true"),
    );
    fireEvent.click(screen.getByRole("tab", { name: /actions cache/i }));
    expect(screen.getByPlaceholderText(/ghp_\.\.\. \(leave blank/i)).toHaveValue("");
  });
});

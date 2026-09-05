import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const cockpitMock = vi.fn();
const myViewMock = vi.fn();
const actionsUsageMock = vi.fn();

let authUser: { id: number } | null = { id: 1 };
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ user: authUser }),
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
    },
    analytics: {
      cockpit: (...args: unknown[]) => cockpitMock(...args),
      myView: (...args: unknown[]) => myViewMock(...args),
      actionsUsage: (...args: unknown[]) => actionsUsageMock(...args),
    },
  },
}));

import OverviewPage from "@/app/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <OverviewPage />
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

const EMPTY_COCKPIT = {
  repo_count: 0,
  member_count: 0,
  latest_score: null,
  score_trend: [],
  recent_events: [],
  open_pr_count: 0,
  pr_merge_rate_4w: [],
  commit_activity_4w: [],
  total_cache_size_bytes: 0,
  cache_job_success_rate: 0,
  at_risk_repos: [],
  milestones: [],
  pr_cycle_time_8w: [],
  release_cadence_4w: [],
  commit_activity_source: "github" as const,
  recent_events_source: "github" as const,
  recent_events_stale: false,
  degraded: false,
};

const EMPTY_MY_VIEW = {
  my_open_prs: [],
  review_requests: [],
  assigned_issues: [],
  my_recent_runs: [],
  identity_unresolved: false,
};

describe("OverviewPage cockpit", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    cockpitMock.mockReset();
    myViewMock.mockReset();
    actionsUsageMock.mockReset();
    myViewMock.mockResolvedValue(EMPTY_MY_VIEW);
    // Default: the Actions-usage query fails (App lacks the billing permission) so the
    // card is absent -- matching the common self-hosted setup.
    actionsUsageMock.mockRejectedValue(new Error("GitHub API error: 400"));
    authUser = { id: 1 };
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows Configure links for all 4 stat cards when no org is configured", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getAllByText("Configure →")).toHaveLength(4);
    });

    expect(tokensResolveMock).not.toHaveBeenCalled();
    expect(cockpitMock).not.toHaveBeenCalled();

    // Each card's "Configure →" must route to the page it actually represents, not all
    // four piling onto Health & Security (a prior bug: LiveStatCard hardcoded /security).
    const expectedHrefs: Record<string, string> = {
      Repositories: "/repos",
      "Open PRs": "/pulls",
      "Security Score": "/security",
      "Team Members": "/collaborators",
    };
    for (const [label, href] of Object.entries(expectedHrefs)) {
      // getByRole (not getByText) since "Security Score" also appears as a plain
      // <span> chart heading further down the page -- the stat card's <a> is the only
      // element whose accessible name (label + "Configure →") matches this regex.
      const card = screen.getByRole("link", { name: new RegExp(label) });
      expect(card).toHaveAttribute("href", href);
    }
  });

  it("fires a single cockpit call (no waterfall) and renders real values for all 4 cards", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      repo_count: 12,
      open_pr_count: 5,
      latest_score: 87,
      score_trend: [70, 80, 87],
      member_count: 9,
    });

    renderPage();

    await waitFor(() => {
      expect(cockpitMock).toHaveBeenCalledWith("acme", "ghp_test");
    });

    await waitFor(() => {
      expect(screen.getByText("12")).toBeInTheDocument();
      expect(screen.getByText("5")).toBeInTheDocument();
      expect(screen.getByText("87")).toBeInTheDocument();
      expect(screen.getByText("9")).toBeInTheDocument();
    });

    expect(screen.queryAllByText("Configure →")).toHaveLength(0);
    // A single cockpit call once the token-resolve fetch settles -- not a second
    // waterfalled fetch for jobs/overview like the pre-cockpit page used to do.
    expect(cockpitMock).toHaveBeenCalledTimes(1);
  });

  it("labels commit activity as estimated when the cockpit source is an aggregate", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      commit_activity_4w: [1, 2, 3, 4],
      commit_activity_source: "aggregate" as const,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("(estimated)")).toBeInTheDocument();
    });
  });

  it("does not show the estimated caption when the cockpit source is github", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      commit_activity_4w: [1, 2, 3, 4],
    });

    const { queryClient } = renderPage();

    await waitFor(() => {
      expect(queryClient.getQueryState(["analytics.cockpit", "acme"])?.status).toBe("success");
    });
    expect(screen.queryByText("(estimated)")).not.toBeInTheDocument();
  });

  it("shows a data-may-be-incomplete banner when the cockpit response is degraded", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT, degraded: true });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Some data below may be incomplete/i)).toBeInTheDocument();
    });
  });

  it("does not show the degraded banner when the cockpit response is not degraded", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue(EMPTY_COCKPIT);

    const { queryClient } = renderPage();

    await waitFor(() => {
      expect(queryClient.getQueryState(["analytics.cockpit", "acme"])?.status).toBe("success");
    });
    expect(screen.queryByText(/Some data below may be incomplete/i)).not.toBeInTheDocument();
  });

  it("labels recent activity as stale when the cockpit says the ingestion cursor is stale", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      recent_events_source: "aggregate" as const,
      recent_events_stale: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("(stale)")).toBeInTheDocument();
    });
  });

  it("does not label recent activity as stale when the cockpit says it's current", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      recent_events_source: "aggregate" as const,
      recent_events_stale: false,
    });

    const { queryClient } = renderPage();

    await waitFor(() => {
      expect(queryClient.getQueryState(["analytics.cockpit", "acme"])?.status).toBe("success");
    });
    expect(screen.queryByText("(stale)")).not.toBeInTheDocument();
  });

  it("renders empty states for charts and activity when the org has no data yet", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue(EMPTY_COCKPIT);

    renderPage();

    await waitFor(() => {
      expect(cockpitMock).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.getByText("— no recent activity")).toBeInTheDocument();
    });
    expect(screen.getByText("No commit activity yet")).toBeInTheDocument();
    expect(screen.getByText("Run more scans to see a trend")).toBeInTheDocument();
    expect(screen.getByText("No pull request activity yet")).toBeInTheDocument();
  });

  it("renders recent events in the activity panel", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      recent_events: [
        {
          id: "1",
          type: "PushEvent",
          actor: "alice",
          actor_avatar: "",
          repo: "acme/api",
          summary: "pushed 3 commits to main",
          created_at: "2026-07-18T00:00:00Z",
        },
      ],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    expect(screen.getByText("pushed 3 commits to main")).toBeInTheDocument();
  });

  it("renders the Actions Usage card from the billing usage summary (issue #294)", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });
    actionsUsageMock.mockResolvedValue({
      total_minutes_used: 1250,
      included_minutes_used: 900,
      paid_minutes_used: 350,
      minutes_used_breakdown: { actions_linux: 1000, actions_macos: 250 },
    });

    renderPage();

    expect(await screen.findByText("Actions Usage (this month)")).toBeInTheDocument();
    expect(screen.getByText(/1,250 min/)).toBeInTheDocument();
    expect(screen.getByText(/900 included · 350 billable/)).toBeInTheDocument();
    expect(screen.getByText("actions_linux")).toBeInTheDocument();
  });

  it("omits the billable clause when nothing was billed on top of the allowance", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });
    actionsUsageMock.mockResolvedValue({
      total_minutes_used: 42,
      included_minutes_used: 42,
      paid_minutes_used: 0,
      minutes_used_breakdown: {},
    });

    renderPage();

    expect(await screen.findByText(/42 min/)).toBeInTheDocument();
    expect(screen.getByText("42 included")).toBeInTheDocument();
    expect(screen.queryByText(/billable/)).not.toBeInTheDocument();
  });

  it("omits the Actions Usage card when no Actions minutes were used this month", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });
    actionsUsageMock.mockResolvedValue({
      total_minutes_used: 0,
      included_minutes_used: 0,
      paid_minutes_used: 0,
      minutes_used_breakdown: {},
    });

    renderPage();

    await waitFor(() => expect(actionsUsageMock).toHaveBeenCalled());
    expect(screen.queryByText("Actions Usage (this month)")).not.toBeInTheDocument();
  });

  it("omits the Actions Usage card when the billing query fails (missing permission)", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });
    // actionsUsageMock rejects by default (see beforeEach)

    const { queryClient } = renderPage();

    await waitFor(() =>
      expect(queryClient.getQueryState(["analytics.actions-usage", "acme", 1])?.status).toBe("error"),
    );
    expect(screen.queryByText("Actions Usage (this month)")).not.toBeInTheDocument();
  });

  it("does not serve one user's cached Actions usage to a different session (CWE-200)", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });
    actionsUsageMock.mockResolvedValue({
      total_minutes_used: 999,
      included_minutes_used: 999,
      paid_minutes_used: 0,
      minutes_used_breakdown: {},
    });

    // Admin (id 1) loads the page and the usage lands in the shared QueryClient.
    const { queryClient, unmount } = renderPage();
    expect(await screen.findByText(/999 min/)).toBeInTheDocument();
    unmount();

    // A member (id 2) signs in on the same tab. Their query key differs, so the
    // admin's cached row can't satisfy it; with billing now 403-ing the card is gone.
    authUser = { id: 2 };
    actionsUsageMock.mockRejectedValue(new Error("GitHub API error: 400"));
    render(
      <QueryClientProvider client={queryClient}>
        <OverviewPage />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(queryClient.getQueryState(["analytics.actions-usage", "acme", 2])?.status).toBe("error"),
    );
    expect(screen.queryByText(/999 min/)).not.toBeInTheDocument();
  });

  it("titles the card 'Recent Highlights' and links to the full activity feed (issue #285)", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({ ...EMPTY_COCKPIT });

    renderPage();

    expect(await screen.findByText("Recent Highlights")).toBeInTheDocument();
    expect(screen.queryByText("Recent Activity")).not.toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view full activity feed/i });
    expect(link).toHaveAttribute("href", "/activity");
  });

  it("hides the Needs Attention card when there are no at-risk repos", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue(EMPTY_COCKPIT);

    renderPage();

    await waitFor(() => expect(cockpitMock).toHaveBeenCalled());
    expect(screen.queryByText("Needs Attention")).not.toBeInTheDocument();
  });

  it("renders at-risk repos with their reasons and severity", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      at_risk_repos: [{ repo: "acme/api", reasons: ["Milestone 'v2' overdue"], severity: "critical" }],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Needs Attention")).toBeInTheDocument();
    });
    expect(screen.getByText("acme/api")).toBeInTheDocument();
    expect(screen.getByText("Milestone 'v2' overdue")).toBeInTheDocument();
  });

  it("renders at-risk repos with warning severity styling (not just critical)", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      at_risk_repos: [{ repo: "acme/worker", reasons: ["Milestone 'v3' due soon at 40% complete"], severity: "warning" }],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("acme/worker")).toBeInTheDocument();
    });
    expect(screen.getByText("Milestone 'v3' due soon at 40% complete")).toBeInTheDocument();
  });

  it("renders milestone burndown rows", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      milestones: [
        {
          repo: "acme/api",
          title: "v2",
          // relativeTime() renders anything within 60s (or in the future) as "just now" --
          // computed relative to test execution time so this doesn't go stale as real time
          // passes (a hardcoded near-future date drifted into "N days ago" here before).
          due_on: new Date(Date.now() + 60_000).toISOString(),
          open_issues: 2,
          closed_issues: 8,
          progress_pct: 80,
          state: "on_track",
        },
      ],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/v2/)).toBeInTheDocument();
    });
    expect(screen.getByText((_, el) => el?.textContent === "8/10 closed · due just now")).toBeInTheDocument();
  });

  it("renders overdue and at-risk milestone chips with their own styling", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue({
      ...EMPTY_COCKPIT,
      milestones: [
        {
          repo: "acme/api",
          title: "v1",
          due_on: "2020-01-01T00:00:00Z",
          open_issues: 3,
          closed_issues: 1,
          progress_pct: 25,
          state: "overdue",
        },
        {
          repo: "acme/worker",
          title: "v3",
          due_on: "2026-08-01T00:00:00Z",
          open_issues: 6,
          closed_issues: 4,
          progress_pct: 40,
          state: "at_risk",
        },
      ],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("overdue")).toBeInTheDocument();
    });
    expect(screen.getByText("at risk")).toBeInTheDocument();
  });

  it("fetches My View data and switches tabs", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue(EMPTY_COCKPIT);
    myViewMock.mockResolvedValue({
      my_open_prs: [
        { number: 1, title: "Fix bug", repository: "acme/api", html_url: "https://github.com/acme/api/pull/1", updated_at: "2026-07-20T00:00:00Z" },
      ],
      review_requests: [],
      assigned_issues: [
        { number: 2, title: "Investigate flake", repository: "acme/worker", html_url: "https://github.com/acme/worker/issues/2", updated_at: "2026-07-19T00:00:00Z" },
      ],
      my_recent_runs: [],
    });

    renderPage();

    await waitFor(() => {
      expect(myViewMock).toHaveBeenCalledWith("acme", "ghp_test");
    });
    await waitFor(() => {
      expect(screen.getByText("Fix bug")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Assigned Issues"));
    expect(screen.getByText("Investigate flake")).toBeInTheDocument();
    expect(screen.queryByText("Fix bug")).not.toBeInTheDocument();
  });

  it("shows empty states for release cadence and PR cycle time when all values are zero", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockResolvedValue(EMPTY_COCKPIT);

    renderPage();

    await waitFor(() => expect(cockpitMock).toHaveBeenCalled());
    expect(screen.getByText("No releases in the last 4 weeks")).toBeInTheDocument();
    expect(screen.getByText("No merged pull requests yet")).toBeInTheDocument();
  });

  it("shows a distinct 'no default org' banner (not a blank empty state) when nothing is configured", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/No account selected yet/)).toBeInTheDocument();
    });
    const settingsLink = screen.getByRole("link", { name: "Connect a GitHub account" });
    expect(settingsLink).toHaveAttribute("href", "/settings");
    expect(cockpitMock).not.toHaveBeenCalled();
  });

  it("shows a retryable error banner (not a blank empty state) when the cockpit query fails", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    cockpitMock.mockRejectedValue(new Error("org access required"));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("org access required")).toBeInTheDocument();
    });
    const retryButton = screen.getByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);
    // Retry re-fires both queries that feed the shared error banner.
    await waitFor(() => expect(cockpitMock).toHaveBeenCalledTimes(2));
    expect(myViewMock).toHaveBeenCalledTimes(2);
  });
});

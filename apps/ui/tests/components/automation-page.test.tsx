import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const workflowsMock = vi.fn();
const runsMock = vi.fn();
const dispatchMock = vi.fn();
const dispatchAllMock = vi.fn();
const reposListMock = vi.fn();
const installationsListMock = vi.fn();
const installationsListForOrgMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
      upsert: vi.fn(),
    },
    automation: {
      workflows: (...args: unknown[]) => workflowsMock(...args),
      runs: (...args: unknown[]) => runsMock(...args),
      dispatch: (...args: unknown[]) => dispatchMock(...args),
      dispatchAll: (...args: unknown[]) => dispatchAllMock(...args),
    },
    repos: {
      list: (...args: unknown[]) => reposListMock(...args),
    },
    installations: {
      list: (...args: unknown[]) => installationsListMock(...args),
      listForOrg: (...args: unknown[]) => installationsListForOrgMock(...args),
    },
  },
}));

import AutomationPage from "@/app/automation/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AutomationPage />
    </QueryClientProvider>,
  );
}

const DEMO_REPO = {
  name: "demo",
  full_name: "acme/demo",
  private: false,
  description: null,
  language: null,
  stargazers_count: 0,
  forks_count: 0,
  watchers_count: 0,
  open_issues_count: 0,
  pushed_at: null,
  default_branch: "main",
  html_url: "https://github.com/acme/demo",
};

/** Types the owner, waits for the repo dropdown to populate, then selects `name`. */
async function enterOwnerAndSelectRepo(owner: string, name: string) {
  fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: owner } });
  await waitFor(() => expect(screen.getByRole("option", { name })).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("Repository"), { target: { value: name } });
}

describe("AutomationPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    workflowsMock.mockReset();
    runsMock.mockReset();
    dispatchMock.mockReset();
    dispatchAllMock.mockReset();
    reposListMock.mockReset();
    reposListMock.mockResolvedValue({ org: "acme", total: 1, repos: [DEMO_REPO] });
    installationsListMock.mockReset();
    installationsListMock.mockResolvedValue([]);
    installationsListForOrgMock.mockReset();
    installationsListForOrgMock.mockResolvedValue([]);
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders no results panel before any repository is loaded", () => {
    renderPage();
    expect(screen.queryByText("Workflows")).not.toBeInTheDocument();
    expect(workflowsMock).not.toHaveBeenCalled();
  });

  it("disables the repository dropdown until an owner is entered", () => {
    renderPage();
    expect(screen.getByLabelText("Repository")).toBeDisabled();
    expect(reposListMock).not.toHaveBeenCalled();
  });

  it("populates the repository dropdown from the entered owner's repo list", async () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    await waitFor(() => expect(reposListMock).toHaveBeenCalledWith("acme", ""));
    await waitFor(() => expect(screen.getByRole("option", { name: "demo" })).toBeInTheDocument());
    expect(screen.getByLabelText("Repository")).not.toBeDisabled();
  });

  it("clears a selected repository when the owner changes, disabling Load workflows until a new one is picked", async () => {
    // Regression test (CodeRabbit finding on PR #300): a stale repo name from the old
    // owner must not be submittable against the new owner's dropdown options.
    renderPage();
    await enterOwnerAndSelectRepo("acme", "demo");
    expect(screen.getByLabelText("Repository")).toHaveValue("demo");

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "other-org" } });

    await waitFor(() => expect(screen.getByLabelText("Repository")).toHaveValue(""));
    expect(screen.getByRole("button", { name: "Load workflows" })).toBeDisabled();
  });

  it("shows a 'Failed to load repositories' placeholder when the repo list fetch errors", async () => {
    reposListMock.mockRejectedValue(new Error("GitHub API unreachable"));
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Failed to load repositories" })).toBeInTheDocument();
    });
  });

  it("shows a 'No repositories found' placeholder when the owner has zero repos", async () => {
    reposListMock.mockResolvedValue({ org: "acme", total: 0, repos: [] });
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "No repositories found" })).toBeInTheDocument();
    });
  });

  it("shows the GitHub Token field when no installation covers the entered owner", async () => {
    installationsListMock.mockResolvedValue([]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(screen.getByText("GitHub Token")).toBeInTheDocument();
    });
  });

  it("hides the GitHub Token field when a personal installation covers the entered owner", async () => {
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "acme", account_type: "Organization", installation_id: 42, created_at: "2026-07-20T00:00:00Z" },
    ]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(screen.queryByText("GitHub Token")).not.toBeInTheDocument();
    });
  });

  it("hides the GitHub Token field when a personal installation covers the entered owner with trailing whitespace", async () => {
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

  it("hides the GitHub Token field when an org-level installation covers the entered owner", async () => {
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

  it("shows a permission-drift notice when the org installation is missing scopes", async () => {
    installationsListForOrgMock.mockResolvedValue([
      {
        id: 2,
        account_login: "acme",
        account_type: "Organization",
        installation_id: 99,
        created_at: "2026-07-20T00:00:00Z",
        permissions_synced_at: "2026-09-01T00:00:00Z",
        blocked_features: [
          { feature: "bulk_branch_protection", label: "Bulk branch-protection apply", missing: { administration: "write" } },
        ],
      },
    ]);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    await waitFor(() => {
      expect(screen.getByText(/needs extra GitHub access/i)).toBeInTheDocument();
      expect(screen.getByText("Bulk branch-protection apply")).toBeInTheDocument();
    });
  });

  it("loads workflows and run history for the selected owner/repo", async () => {
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [
        { id: 1, name: "CI", path: ".github/workflows/ci.yml", state: "active", last_run_status: "completed", last_run_conclusion: "success", last_run_at: "2026-07-20T00:00:00Z" },
      ],
    });
    runsMock.mockResolvedValue({
      repository: "acme/demo",
      runs: [
        { id: 100, name: "CI", status: "completed", conclusion: "success", head_branch: "main", created_at: "2026-07-20T00:00:00Z", duration_ms: 60000 },
      ],
    });

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    // Waiting for the repo dropdown to populate above gives the token-resolve mock time
    // to settle too, so by the time "Load workflows" is clicked the resolved token is
    // already applied -- unlike the free-text-field version of this test, which could
    // click through before that resolution landed.
    await waitFor(() => expect(screen.getByText("saved")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => {
      expect(workflowsMock).toHaveBeenCalledWith("acme", "demo", "ghp_test");
      expect(runsMock).toHaveBeenCalledWith("acme", "demo", "ghp_test");
    });

    await waitFor(() => {
      expect(screen.getAllByText("CI").length).toBeGreaterThan(0);
    });
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("arms then confirms a workflow dispatch, calling the API only after the second click", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [{ id: 1, name: "CI", path: ".github/workflows/ci.yml", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null }],
    });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });
    dispatchMock.mockResolvedValue({ dispatched: true, message: "Workflow dispatched." });

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("CI")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Dispatch/i }));

    await waitFor(() => {
      expect(screen.getByText("Dispatch workflow")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Dispatch workflow"));
    expect(dispatchMock).not.toHaveBeenCalled();
    expect(screen.getByText("Confirm dispatch")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Confirm dispatch"));

    await waitFor(() => {
      expect(dispatchMock).toHaveBeenCalledWith("acme", "demo", 1, { token: "", ref: "main" });
    });
  });

  it("hides the 'Dispatch all' toolbar action when a repo has one workflow", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [{ id: 1, name: "CI", path: "p", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null }],
    });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();
    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("CI")).toBeInTheDocument());
    expect(screen.queryByText("Dispatch all")).not.toBeInTheDocument();
  });

  it("arms then confirms 'Dispatch all', showing the result summary", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [
        { id: 1, name: "CI", path: "p", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null },
        { id: 2, name: "Release", path: "p", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null },
      ],
    });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });
    dispatchAllMock.mockResolvedValue({
      ref: "main",
      results: [
        { workflow_id: 1, name: "CI", status: "dispatched", message: null },
        { workflow_id: 2, name: "Release", status: "failed", message: "Resource not accessible by integration" },
      ],
      dispatched_count: 1,
      skipped_count: 0,
      failed_count: 1,
    });

    renderPage();
    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));
    await waitFor(() => expect(screen.getByText("CI")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Dispatch all"));
    expect(dispatchAllMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Confirm — dispatch all"));

    await waitFor(() => expect(dispatchAllMock).toHaveBeenCalledWith("acme", "demo", { token: "", ref: "main" }));
    await waitFor(() => expect(screen.getByText("1 dispatched")).toBeInTheDocument());
    expect(screen.getByText("Release: Resource not accessible by integration")).toBeInTheDocument();

    // Reloading workflows clears the previous bulk-dispatch summary.
    fireEvent.click(screen.getByText("Load workflows"));
    await waitFor(() => expect(screen.queryByText("1 dispatched")).not.toBeInTheDocument());
  });

  it("clears an armed 'Dispatch all' confirmation when the ref is edited", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [
        { id: 1, name: "CI", path: "p", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null },
        { id: 2, name: "Release", path: "p", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null },
      ],
    });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();
    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));
    await waitFor(() => expect(screen.getByText("CI")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Dispatch all"));
    expect(screen.getByText("Confirm — dispatch all")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Ref for bulk dispatch"), { target: { value: "release" } });
    expect(screen.getByText("Dispatch all")).toBeInTheDocument();
    expect(screen.queryByText("Confirm — dispatch all")).not.toBeInTheDocument();
  });

  it("surfaces an error message when loading workflows fails", async () => {
    workflowsMock.mockRejectedValue(new Error("GitHub API unreachable"));
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => {
      expect(screen.getByText("GitHub API unreachable")).toBeInTheDocument();
    });
  });

  it("shows a loading skeleton, then the empty state, when a repo has no workflows", async () => {
    let resolveWorkflows: (v: unknown) => void = () => {};
    workflowsMock.mockImplementation(
      () => new Promise((res) => { resolveWorkflows = res; }),
    );
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("Loading…")).toBeInTheDocument());

    resolveWorkflows({ repository: "acme/demo", workflows: [] });

    await waitFor(() => {
      expect(screen.getByText("No workflows found in this repository.")).toBeInTheDocument();
    });
  });

  it("saves a manually entered token and renders failure/pending status icons with fallback labels", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [
        { id: 1, name: "CI", path: ".github/workflows/ci.yml", state: "active", last_run_status: "in_progress", last_run_conclusion: null, last_run_at: null },
      ],
    });
    runsMock.mockResolvedValue({
      repository: "acme/demo",
      runs: [
        { id: 200, name: null, status: "completed", conclusion: "failure", head_branch: "main", created_at: "2026-07-20T00:00:00Z", duration_ms: null },
        { id: 201, name: "Build", status: "completed", conclusion: "cancelled", head_branch: "dev", created_at: "2026-07-20T00:00:00Z", duration_ms: null },
      ],
    });

    const { api } = await import("@/lib/api/client");
    const upsertMock = api.tokens.upsert as unknown as ReturnType<typeof vi.fn>;
    upsertMock.mockReset();
    upsertMock.mockResolvedValue({});

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.change(screen.getByPlaceholderText(/ghp_/), { target: { value: "ghp_manual123456789012345678901234" } });

    await waitFor(() => expect(screen.getByText("Save token for this org")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Save token for this org"));
    await waitFor(() => expect(upsertMock).toHaveBeenCalledWith("acme", "ghp_manual123456789012345678901234"));

    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("#200")).toBeInTheDocument());
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("in_progress")).toBeInTheDocument();
  });

  it("surfaces a dispatch error and lets the user edit the ref before retrying", async () => {
    workflowsMock.mockResolvedValue({
      repository: "acme/demo",
      workflows: [{ id: 1, name: "CI", path: ".github/workflows/ci.yml", state: "active", last_run_status: null, last_run_conclusion: null, last_run_at: null }],
    });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });
    dispatchMock.mockRejectedValue(new Error("GitHub API error: 422"));

    renderPage();

    await enterOwnerAndSelectRepo("acme", "demo");
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("CI")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Dispatch/i }));

    const refInput = screen.getByDisplayValue("main");
    fireEvent.change(refInput, { target: { value: "release" } });

    fireEvent.click(screen.getByText("Dispatch workflow"));
    fireEvent.click(screen.getByText("Confirm dispatch"));

    await waitFor(() => {
      expect(screen.getByText("GitHub API error: 422")).toBeInTheDocument();
    });
  });
});

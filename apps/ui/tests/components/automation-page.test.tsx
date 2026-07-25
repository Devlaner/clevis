import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const workflowsMock = vi.fn();
const runsMock = vi.fn();
const dispatchMock = vi.fn();
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

describe("AutomationPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    workflowsMock.mockReset();
    runsMock.mockReset();
    dispatchMock.mockReset();
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

  it("loads workflows and run history for the entered owner/repo", async () => {
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

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => {
      expect(workflowsMock).toHaveBeenCalledWith("acme", "demo", "");
      expect(runsMock).toHaveBeenCalledWith("acme", "demo", "");
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

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
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

  it("surfaces an error message when loading workflows fails", async () => {
    workflowsMock.mockRejectedValue(new Error("GitHub API unreachable"));
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
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

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
    fireEvent.click(screen.getByText("Load workflows"));

    await waitFor(() => expect(screen.getByText("Loading…")).toBeInTheDocument());

    resolveWorkflows({ repository: "acme/demo", workflows: [] });

    await waitFor(() => {
      expect(screen.getByText("No workflows found in this repository.")).toBeInTheDocument();
    });
  });

  it("submits the load request when Enter is pressed in the repository field", async () => {
    workflowsMock.mockResolvedValue({ repository: "acme/demo", workflows: [] });
    runsMock.mockResolvedValue({ repository: "acme/demo", runs: [] });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const repoInput = screen.getByPlaceholderText("e.g. hello-world");
    fireEvent.change(repoInput, { target: { value: "demo" } });
    fireEvent.keyDown(repoInput, { key: "Enter" });

    await waitFor(() => {
      expect(workflowsMock).toHaveBeenCalledWith("acme", "demo", "");
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

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
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

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. hello-world"), { target: { value: "demo" } });
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

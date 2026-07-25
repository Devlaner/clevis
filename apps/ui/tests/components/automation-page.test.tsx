import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const workflowsMock = vi.fn();
const runsMock = vi.fn();
const dispatchMock = vi.fn();
const reposListMock = vi.fn();

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
    repos: {
      list: (...args: unknown[]) => reposListMock(...args),
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
  fireEvent.change(screen.getByRole("combobox"), { target: { value: name } });
}

describe("AutomationPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    workflowsMock.mockReset();
    runsMock.mockReset();
    dispatchMock.mockReset();
    reposListMock.mockReset();
    reposListMock.mockResolvedValue({ org: "acme", total: 1, repos: [DEMO_REPO] });
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
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(reposListMock).not.toHaveBeenCalled();
  });

  it("populates the repository dropdown from the entered owner's repo list", async () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    await waitFor(() => expect(reposListMock).toHaveBeenCalledWith("acme", ""));
    await waitFor(() => expect(screen.getByRole("option", { name: "demo" })).toBeInTheDocument());
    expect(screen.getByRole("combobox")).not.toBeDisabled();
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

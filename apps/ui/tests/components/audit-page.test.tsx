import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auditListMock = vi.fn();
const jobsListMock = vi.fn();

let mockSearchParams = new URLSearchParams();

// jsdom doesn't implement scrollIntoView -- needed for the ?job_id= highlight-and-scroll effect.
Element.prototype.scrollIntoView = vi.fn();

vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    audit: { list: (...args: unknown[]) => auditListMock(...args) },
    jobs: { list: (...args: unknown[]) => jobsListMock(...args) },
  },
}));

import AuditPage from "@/app/audit/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuditPage />
    </QueryClientProvider>,
  );
}

describe("AuditPage", () => {
  beforeEach(() => {
    auditListMock.mockReset();
    jobsListMock.mockReset();
    jobsListMock.mockResolvedValue([]);
    mockSearchParams = new URLSearchParams();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders audit log entries", async () => {
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "installation.connected", target: "acme", payload: "{}", created_at: "2026-01-01T00:00:00Z" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("installation.connected")).toBeInTheDocument());
  });

  it("shows a retry option instead of a fake empty state when the query fails", async () => {
    // Regression test: this page used to default logs to [] on any query error and never
    // checked isError, so a real 403/500 rendered identically to "genuinely zero rows".
    auditListMock.mockRejectedValue(new Error("Workspace admin access required"));
    renderPage();
    await waitFor(() => expect(screen.getByText("Workspace admin access required")).toBeInTheDocument());
    expect(screen.queryByText(/No audit events/)).not.toBeInTheDocument();
    // Regression test (CodeRabbit finding): a misleading "0 entries" chip must not
    // render alongside the error message.
    expect(screen.queryByText(/entries$/)).not.toBeInTheDocument();

    auditListMock.mockResolvedValueOnce([]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText(/No audit events/)).toBeInTheDocument());
    expect(auditListMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to a generic message when the rejection isn't an Error instance", async () => {
    auditListMock.mockRejectedValue("boom");
    renderPage();
    await waitFor(() => expect(screen.getByText("Failed to load audit events.")).toBeInTheDocument());
  });

  it("shows the empty state only when the query genuinely succeeds with no rows", async () => {
    auditListMock.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/No audit events/)).toBeInTheDocument());
  });

  it("shows a row's live job status when its payload embeds a matching job_id", async () => {
    auditListMock.mockResolvedValue([
      {
        id: 1,
        actor: "u@e.com",
        action: "cache.clear.queued",
        target: "acme/demo",
        payload: JSON.stringify({ job_id: 42, key: null, ref: null, dry_run: false }),
        created_at: "2026-01-01T00:00:00Z",
      },
    ]);
    jobsListMock.mockResolvedValue([
      { id: 42, job_type: "github.clear_actions_cache", status: "processing", result: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("processing")).toBeInTheDocument());
  });

  it("shows a dash for rows with no job_id, or whose job_id isn't in the jobs list", async () => {
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "installation.connected", target: "acme", payload: "{}", created_at: "2026-01-01T00:00:00Z" },
      { id: 2, actor: "u@e.com", action: "cache.clear.queued", target: "acme/demo", payload: JSON.stringify({ job_id: 99 }), created_at: "2026-01-01T00:00:00Z" },
    ]);
    jobsListMock.mockResolvedValue([]);

    renderPage();

    // "cache.clear.queued" also appears as a static <option> in the action filter
    // dropdown, so waiting on it directly would false-positive before the rows load.
    await waitFor(() => expect(screen.getByText("2 entries")).toBeInTheDocument());
    expect(screen.getAllByText("—").length).toBe(2);
  });

  it("shows a distinct 'failed to load' indicator, not a plain dash, when the jobs query fails for a row with a job_id", async () => {
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "cache.clear.queued", target: "acme/demo", payload: JSON.stringify({ job_id: 42 }), created_at: "2026-01-01T00:00:00Z" },
    ]);
    jobsListMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    await waitFor(() => expect(screen.getByText("1 entries")).toBeInTheDocument());
    expect(screen.getByText("failed to load")).toBeInTheDocument();
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });

  it("still shows a plain dash for rows with no job_id even when the jobs query fails", async () => {
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "installation.connected", target: "acme", payload: "{}", created_at: "2026-01-01T00:00:00Z" },
    ]);
    jobsListMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    await waitFor(() => expect(screen.getByText("1 entries")).toBeInTheDocument());
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("does not crash on a malformed (non-JSON) payload", async () => {
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "cache.clear.queued", target: "acme/demo", payload: "not json", created_at: "2026-01-01T00:00:00Z" },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("1 entries")).toBeInTheDocument());
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("highlights the row matching a ?job_id= deep link, e.g. from the cache panel's 'View in Audit Log' link", async () => {
    mockSearchParams = new URLSearchParams("job_id=42");
    auditListMock.mockResolvedValue([
      { id: 1, actor: "u@e.com", action: "cache.clear.queued", target: "acme/demo", payload: JSON.stringify({ job_id: 42 }), created_at: "2026-01-01T00:00:00Z" },
      { id: 2, actor: "u@e.com", action: "installation.connected", target: "acme", payload: "{}", created_at: "2026-01-01T00:00:00Z" },
    ]);
    jobsListMock.mockResolvedValue([
      { id: 42, job_type: "github.clear_actions_cache", status: "done", result: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("done")).toBeInTheDocument());
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveClass("ring-primary/40");
    expect(rows[1]).not.toHaveClass("ring-primary/40");
  });
});

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const tokensUpsertMock = vi.fn();
const cacheListMock = vi.fn();
const cacheClearMock = vi.fn();
const jobsGetMock = vi.fn();

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
    jobs: {
      get: (...args: unknown[]) => jobsGetMock(...args),
    },
  },
}));

import CachePage from "@/app/repos/[repo]/cache/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <CachePage />
    </QueryClientProvider>,
  );
  return {
    ...utils,
    rerenderSamePage: () =>
      utils.rerender(
        <QueryClientProvider client={queryClient}>
          <CachePage />
        </QueryClientProvider>,
      ),
  };
}

describe("CachePage", () => {
  beforeEach(() => {
    currentRepoParam = "acme~demo";
    tokensResolveMock.mockReset();
    tokensUpsertMock.mockReset();
    cacheListMock.mockReset();
    cacheClearMock.mockReset();
    jobsGetMock.mockReset();
    tokensResolveMock.mockRejectedValue(new Error("no saved token"));
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "done",
      result: JSON.stringify({ ok: true, deleted: 3 }),
      created_at: "",
      updated_at: "",
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("allows a dry-run clear without a token or an actor field", async () => {
    cacheClearMock.mockResolvedValue({ queued: false, dry_run: true });

    renderPage();

    // The Actor field is gone — the API attributes the action to the signed-in user.
    expect(screen.queryByPlaceholderText("actor")).not.toBeInTheDocument();

    const dryRunButton = screen.getByRole("button", { name: /dry run/i });
    await waitFor(() => expect(dryRunButton).not.toBeDisabled());

    fireEvent.click(dryRunButton);

    await waitFor(() =>
      expect(cacheClearMock).toHaveBeenCalledWith("acme", "demo", {
        token: "",
        dry_run: true,
      }),
    );
  });

  it("enables the Clear button without needing an actor entered", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: /^clear$/i })).not.toBeDisabled());
  });

  it("requires confirming in a dialog before actually clearing caches", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 7 });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());

    fireEvent.click(clearButton);

    // Clicking Clear only opens the confirm dialog — no request fired yet.
    expect(cacheClearMock).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(screen.getByText(/permanently deletes every Actions cache entry/i)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() =>
      expect(cacheClearMock).toHaveBeenCalledWith("acme", "demo", {
        token: "",
        dry_run: false,
      }),
    );
    // The dialog closes itself once the clear mutation succeeds.
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("polls the enqueued job and reports the real terminal outcome", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() => expect(jobsGetMock).toHaveBeenCalledWith(42));
    expect(await screen.findByText(/cache cleared — 3 entries deleted/i)).toBeInTheDocument();
  });

  it("surfaces a job that fails on GitHub's side", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "failed",
      result: "GitHub API error: 403 - Resource not accessible by integration",
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/cache clear failed — .*not accessible/i)).toBeInTheDocument();
  });

  it("shows a generic 'Cache cleared.' when the job result has no deleted count", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    // A key-scoped clear's result is {ok, status}, with no `deleted` field.
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "done",
      result: JSON.stringify({ ok: true, status: 204 }),
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText("Cache cleared.")).toBeInTheDocument();
  });

  it("tolerates a non-JSON job result", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "done",
      result: "not json at all",
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText("Cache cleared.")).toBeInTheDocument();
  });

  it("shows a progress indicator while the job is still running", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "processing",
      result: null,
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/clearing caches…/i)).toBeInTheDocument();
  });

  it("shows a queued indicator before the worker picks the job up", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "queued",
      result: null,
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/queued…/i)).toBeInTheDocument();
  });

  it("shows a retryable error when polling the job status fails", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockRejectedValue(new Error("Unknown job"));

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/couldn't check job status — unknown job/i)).toBeInTheDocument();

    // Recover on the next attempt when Retry is clicked.
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "done",
      result: JSON.stringify({ ok: true, deleted: 1 }),
      created_at: "",
      updated_at: "",
    });
    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));

    expect(await screen.findByText("Cache cleared — 1 entry deleted.")).toBeInTheDocument();
  });

  it("surfaces a job whose failure result is empty", async () => {
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });
    jobsGetMock.mockResolvedValue({
      id: 42,
      job_type: "github.clear_actions_cache",
      status: "failed",
      result: null,
      created_at: "",
      updated_at: "",
    });

    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());
    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/cache clear failed — unknown error/i)).toBeInTheDocument();
  });

  it("closes the confirm dialog without clearing when Cancel is clicked", async () => {
    renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());

    fireEvent.click(clearButton);
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.click(within(dialog).getByRole("button", { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(cacheClearMock).not.toHaveBeenCalled();
  });

  it("closes the confirm dialog when the route's repo param changes", async () => {
    const { rerenderSamePage } = renderPage();

    const clearButton = screen.getByRole("button", { name: /^clear$/i });
    await waitFor(() => expect(clearButton).not.toBeDisabled());

    fireEvent.click(clearButton);
    await screen.findByRole("alertdialog");

    currentRepoParam = "acme~other";
    rerenderSamePage();

    await waitFor(() => expect(screen.getByText(/acme\/other/i)).toBeInTheDocument());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(cacheClearMock).not.toHaveBeenCalled();
  });

  it("auto-applies a resolved saved token and shows the saved badge", async () => {
    tokensResolveMock.mockReset();
    tokensResolveMock.mockResolvedValue({ token: "ghp_saved_1234567890123456789012" });

    renderPage();

    await waitFor(() => expect(screen.getByText(/saved/i)).toBeInTheDocument());
  });

  it("lets the user type and save a token, then shows the saved badge", async () => {
    tokensUpsertMock.mockResolvedValue({ org: "acme", label: null, created_at: "", updated_at: "" });

    renderPage();
    await waitFor(() => expect(tokensResolveMock).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/leave blank/i), {
      target: { value: "ghp_manual_1234567890123456789012" },
    });

    const saveButton = await screen.findByRole("button", { name: /save token for this org/i });
    fireEvent.click(saveButton);

    await waitFor(() => expect(tokensUpsertMock).toHaveBeenCalledWith("acme", "ghp_manual_1234567890123456789012"));
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("shows an error message if saving the token fails", async () => {
    tokensUpsertMock.mockRejectedValue(new Error("Could not save token"));

    renderPage();
    await waitFor(() => expect(tokensResolveMock).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/leave blank/i), {
      target: { value: "ghp_manual_1234567890123456789012" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /save token for this org/i }));

    expect(await screen.findByText(/could not save token/i)).toBeInTheDocument();
  });

  it("shows an error message when loading caches fails", async () => {
    cacheListMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /load caches/i }));

    expect(await screen.findByText(/github api unreachable/i)).toBeInTheDocument();
  });

  it("shows an error message when a clear request fails", async () => {
    cacheClearMock.mockRejectedValue(new Error("Could not clear caches"));

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /^clear$/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/could not clear caches/i)).toBeInTheDocument();
  });

  it("clears a single row's cache via its own key-scoped Clear key action, without affecting others", async () => {
    cacheListMock.mockResolvedValue({
      actions_caches: [
        {
          id: 1,
          key: "api-cache-key",
          ref: "refs/heads/main",
          size_in_bytes: 1024,
          created_at: "2026-01-01T00:00:00Z",
          last_accessed_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 9 });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /load caches/i }));
    await waitFor(() => expect(screen.getByText("api-cache-key")).toBeInTheDocument());

    const clearKeyButton = screen.getByRole("button", { name: /^clear key$/i });
    await waitFor(() => expect(clearKeyButton).not.toBeDisabled());
    fireEvent.click(clearKeyButton);

    expect(cacheClearMock).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/api-cache-key/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() =>
      expect(cacheClearMock).toHaveBeenCalledWith("acme", "demo", {
        token: "",
        dry_run: false,
        key: "api-cache-key",
        ref: "refs/heads/main",
      }),
    );

    // The global "Clear" button, not this row's, must remain a plain trigger.
    expect(screen.getByRole("button", { name: /^clear$/i })).toBeInTheDocument();
  });

  it("clears the stale cache table and clear result when navigating to a different repo under the same owner", async () => {
    cacheListMock.mockResolvedValue({
      actions_caches: [
        {
          id: 1,
          key: "api-cache-key",
          ref: "refs/heads/main",
          size_in_bytes: 1024,
          created_at: "2026-01-01T00:00:00Z",
          last_accessed_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    cacheClearMock.mockResolvedValue({ queued: true, dry_run: false, job_id: 42 });

    const { rerenderSamePage } = renderPage();

    fireEvent.click(screen.getByRole("button", { name: /load caches/i }));
    await waitFor(() => expect(screen.getByText("api-cache-key")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^clear$/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(screen.getByText(/cache cleared/i)).toBeInTheDocument());

    currentRepoParam = "acme~other";
    rerenderSamePage();

    await waitFor(() => expect(screen.getByText(/acme\/other/i)).toBeInTheDocument());
    expect(screen.queryByText("api-cache-key")).not.toBeInTheDocument();
    expect(screen.queryByText(/cache cleared/i)).not.toBeInTheDocument();
  });
});

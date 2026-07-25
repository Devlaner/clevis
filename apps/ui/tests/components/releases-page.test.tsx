import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tokensResolveMock = vi.fn();
const releaseTimelineMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: { resolve: (...args: unknown[]) => tokensResolveMock(...args) },
    github: { releaseTimeline: (...args: unknown[]) => releaseTimelineMock(...args) },
  },
}));

import ReleasesPage from "@/app/releases/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReleasesPage />
    </QueryClientProvider>,
  );
}

const RELEASE = {
  repo: "acme/api",
  tag_name: "v1.2.0",
  name: "v1.2.0",
  published_at: "2026-07-20T00:00:00Z",
  is_prerelease: false,
  body_preview: "Bug fixes",
  url: "https://github.com/acme/api/releases/tag/v1.2.0",
};

describe("ReleasesPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    releaseTimelineMock.mockReset();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows a no-default-org message when nothing is configured", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/No account selected/)).toBeInTheDocument();
    });
    expect(releaseTimelineMock).not.toHaveBeenCalled();
  });

  it("renders releases for the default org at the default 90-day window", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockResolvedValue({ org: "acme", releases: [RELEASE] });

    renderPage();

    await waitFor(() => expect(screen.getAllByText(/v1\.2\.0/).length).toBeGreaterThan(0));
    expect(releaseTimelineMock).toHaveBeenCalledWith("acme", "ghp_test", 90);
  });

  it("shows a pre-release badge for pre-release entries", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockResolvedValue({ org: "acme", releases: [{ ...RELEASE, is_prerelease: true }] });

    renderPage();

    await waitFor(() => expect(screen.getByText("pre-release")).toBeInTheDocument());
  });

  it("re-queries with the new day window when the selector changes", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockResolvedValue({ org: "acme", releases: [] });

    renderPage();

    await waitFor(() => expect(releaseTimelineMock).toHaveBeenCalledWith("acme", "ghp_test", 90));

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "30" } });

    await waitFor(() => expect(releaseTimelineMock).toHaveBeenCalledWith("acme", "ghp_test", 30));
  });

  it("shows a retry option instead of a fake empty state when the query fails", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockRejectedValue(new Error("Workspace admin access required"));

    renderPage();

    await waitFor(() => expect(screen.getByText("Workspace admin access required")).toBeInTheDocument());
    expect(screen.queryByText(/No releases/)).not.toBeInTheDocument();

    releaseTimelineMock.mockResolvedValueOnce({ org: "acme", releases: [] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText(/No releases/)).toBeInTheDocument());
    expect(releaseTimelineMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to a generic message when the rejection isn't an Error instance", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockRejectedValue("boom");

    renderPage();

    await waitFor(() => expect(screen.getByText("Failed to load releases.")).toBeInTheDocument());
  });

  it("still queries via a connected GitHub App installation when there's no saved PAT to resolve", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    releaseTimelineMock.mockResolvedValue({ org: "acme", releases: [RELEASE] });

    renderPage();

    await waitFor(() => expect(screen.getAllByText(/v1\.2\.0/).length).toBeGreaterThan(0));
    expect(releaseTimelineMock).toHaveBeenCalledWith("acme", "", 90);
  });

  it("shows the release-timeline endpoint's own error when neither a saved token nor an installation is available", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockRejectedValue(new Error("No saved token for this org"));
    releaseTimelineMock.mockRejectedValue(
      new Error("No GitHub App installation found for 'acme' and no token was provided."),
    );

    renderPage();

    await waitFor(() =>
      expect(
        screen.getByText("No GitHub App installation found for 'acme' and no token was provided."),
      ).toBeInTheDocument(),
    );
  });

  it("shows the empty state only when the query genuinely succeeds with no rows", async () => {
    localStorage.setItem("default_org", "acme");
    tokensResolveMock.mockResolvedValue({ token: "ghp_test" });
    releaseTimelineMock.mockResolvedValue({ org: "acme", releases: [] });

    renderPage();

    await waitFor(() => expect(screen.getByText(/No releases in the last 90 days/)).toBeInTheDocument());
  });
});

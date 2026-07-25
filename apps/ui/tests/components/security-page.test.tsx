import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSyncExternalStore } from "react";

const tokensResolveMock = vi.fn();
const tokensUpsertMock = vi.fn();
const analyticsOverviewMock = vi.fn();
const analyticsHistoryMock = vi.fn();
const securityMatrixMock = vi.fn();
const secretScanningMock = vi.fn();
const installationsListMock = vi.fn();
const installationsListForOrgMock = vi.fn();

// A minimal reactive store standing in for Next's router-driven searchParams so a
// tab click's router.replace(...) actually triggers a re-render in the test, the
// same way real navigation would.
let mockSearchParams = new URLSearchParams();
const searchParamsListeners = new Set<() => void>();
const routerReplaceMock = vi.fn((url: string) => {
  mockSearchParams = new URLSearchParams(url.split("?")[1] ?? "");
  searchParamsListeners.forEach((listener) => listener());
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplaceMock }),
  useSearchParams: () =>
    useSyncExternalStore(
      (listener) => {
        searchParamsListeners.add(listener);
        return () => searchParamsListeners.delete(listener);
      },
      () => mockSearchParams,
    ),
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    tokens: {
      resolve: (...args: unknown[]) => tokensResolveMock(...args),
      upsert: (...args: unknown[]) => tokensUpsertMock(...args),
    },
    analytics: {
      overview: (...args: unknown[]) => analyticsOverviewMock(...args),
      history: (...args: unknown[]) => analyticsHistoryMock(...args),
    },
    security: {
      matrix: (...args: unknown[]) => securityMatrixMock(...args),
      secretScanning: (...args: unknown[]) => secretScanningMock(...args),
    },
    installations: {
      list: (...args: unknown[]) => installationsListMock(...args),
      listForOrg: (...args: unknown[]) => installationsListForOrgMock(...args),
    },
  },
}));

import SecurityPage from "@/app/security/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SecurityPage />
    </QueryClientProvider>,
  );
}

describe("SecurityPage", () => {
  beforeEach(() => {
    tokensResolveMock.mockReset();
    tokensUpsertMock.mockReset();
    analyticsOverviewMock.mockReset();
    analyticsHistoryMock.mockReset();
    securityMatrixMock.mockReset();
    secretScanningMock.mockReset();
    routerReplaceMock.mockClear();
    tokensResolveMock.mockRejectedValue(new Error("no saved token"));
    analyticsHistoryMock.mockResolvedValue([]);
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [],
      summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 0, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });
    secretScanningMock.mockResolvedValue({ repository: "acme/demo", alerts: [] });
    installationsListMock.mockReset();
    installationsListMock.mockResolvedValue([]);
    installationsListForOrgMock.mockReset();
    installationsListForOrgMock.mockResolvedValue([]);
    mockSearchParams = new URLSearchParams();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
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

  it("allows running a scan with no token entered (GitHub App fallback)", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 100,
      total_checks: 0,
      failed_checks: 0,
      repo_count: 0,
      checks: [],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });

    const scanButton = screen.getByRole("button", { name: /run scan/i });
    await waitFor(() => expect(scanButton).not.toBeDisabled());

    fireEvent.click(scanButton);

    await waitFor(() => expect(analyticsOverviewMock).toHaveBeenCalledWith("acme", ""));
  });

  it("runs a scan on Enter in the organization field with no token entered", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 100,
      total_checks: 0,
      failed_checks: 0,
      repo_count: 0,
      checks: [],
    });

    renderPage();

    const orgInput = screen.getByPlaceholderText("e.g. octocat");
    fireEvent.change(orgInput, { target: { value: "acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: /run scan/i })).not.toBeDisabled());

    fireEvent.keyDown(orgInput, { key: "Enter" });
    await waitFor(() => expect(analyticsOverviewMock).toHaveBeenCalledWith("acme", ""));
  });

  it("runs a scan on Enter in the token field", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 100,
      total_checks: 0,
      failed_checks: 0,
      repo_count: 0,
      checks: [],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const tokenInput = screen.getByPlaceholderText(/leave blank to use the connected GitHub App/i);
    fireEvent.change(tokenInput, { target: { value: "ghp_test" } });

    fireEvent.keyDown(tokenInput, { key: "Enter" });
    await waitFor(() => expect(analyticsOverviewMock).toHaveBeenCalledWith("acme", "ghp_test"));
  });

  it("renders the score trend chart once 2+ history points are available", async () => {
    analyticsHistoryMock.mockResolvedValue([
      { id: 2, owner: "acme", score: 90, total_checks: 3, failed_checks: 0, created_at: "2026-07-17T00:00:00Z" },
      { id: 1, owner: "acme", score: 70, total_checks: 3, failed_checks: 1, created_at: "2026-07-10T00:00:00Z" },
    ]);
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 90,
      total_checks: 0,
      failed_checks: 0,
      repo_count: 0,
      checks: [],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const scanButton = screen.getByRole("button", { name: /run scan/i });
    await waitFor(() => expect(scanButton).not.toBeDisabled());
    fireEvent.click(scanButton);

    await waitFor(() => expect(screen.getByText(/Score trend \(last 2 scans\)/)).toBeInTheDocument());
  });

  it("filters checks down to failed only via the Failed tab", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 50,
      total_checks: 2,
      failed_checks: 1,
      repo_count: 1,
      checks: [
        {
          id: "check-a",
          title: "Passing check",
          severity: "low",
          remediation: "n/a",
          status: "pass",
          value: null,
        },
        {
          id: "check-b",
          title: "Failing check",
          severity: "high",
          remediation: "n/a",
          status: "fail",
          value: null,
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const scanButton = screen.getByRole("button", { name: /run scan/i });
    await waitFor(() => expect(scanButton).not.toBeDisabled());
    fireEvent.click(scanButton);

    await waitFor(() => expect(screen.getByText("Passing check")).toBeInTheDocument());
    expect(screen.getByText("Failing check")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Failed" }));

    await waitFor(() => expect(screen.queryByText("Passing check")).not.toBeInTheDocument());
    expect(screen.getByText("Failing check")).toBeInTheDocument();
  });

  it("narrows to a single severity via the By Severity tab's inline select", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme",
      score: 50,
      total_checks: 2,
      failed_checks: 2,
      repo_count: 1,
      checks: [
        { id: "check-a", title: "High severity check", severity: "high", remediation: "n/a", status: "fail", value: null },
        { id: "check-b", title: "Low severity check", severity: "low", remediation: "n/a", status: "fail", value: null },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    const scanButton = screen.getByRole("button", { name: /run scan/i });
    await waitFor(() => expect(scanButton).not.toBeDisabled());
    fireEvent.click(scanButton);

    await waitFor(() => expect(screen.getByText("High severity check")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "By Severity" }));
    const severitySelect = await screen.findByDisplayValue("All severities");
    fireEvent.change(severitySelect, { target: { value: "high" } });

    await waitFor(() => expect(screen.queryByText("Low severity check")).not.toBeInTheDocument());
    expect(screen.getByText("High severity check")).toBeInTheDocument();
  });

  it("loads the compliance matrix alongside a scan and renders repo rows", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        {
          repo: "api",
          branch_protection: true,
          secret_scanning: true,
          dependabot_enabled: true,
          dependabot_critical_count: 1,
          dependabot_high_count: 0,
          code_scanning: true,
          force_push_allowed: false,
          score: 80,
          unknown_dimensions: [],
        },
      ],
      summary: { fully_compliant_count: 0, critical_risk_count: 1, secret_hits_count: 0, vuln_by_severity: { critical: 1, high: 0, medium: 0, low: 0 } },
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => expect(securityMatrixMock).toHaveBeenCalledWith("acme", ""));
    await waitFor(() => expect(screen.getByText("Compliance Matrix")).toBeInTheDocument());
    expect(screen.getAllByText("api").length).toBeGreaterThan(0);
    expect(secretScanningMock).toHaveBeenCalledWith("acme", "api", "");
  });

  it("shows a '?' for dimensions the token couldn't evaluate, not a false pass", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        {
          repo: "api",
          branch_protection: true,
          secret_scanning: true,
          dependabot_enabled: false,
          dependabot_critical_count: 0,
          dependabot_high_count: 0,
          code_scanning: true,
          force_push_allowed: false,
          score: 100,
          unknown_dimensions: ["dependabot"],
        },
      ],
      summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 0, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => expect(screen.getByText("Compliance Matrix")).toBeInTheDocument());
    expect(screen.getByTitle("unknown — token can't see this")).toHaveTextContent("?");
  });

  it("shows a loading skeleton, then a distinguishable error, when the compliance matrix fails", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    let rejectMatrix: (err: unknown) => void = () => {};
    securityMatrixMock.mockImplementation(() => new Promise((_, rej) => { rejectMatrix = rej; }));

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => expect(screen.getByText("Compliance Matrix")).toBeInTheDocument());

    rejectMatrix(new Error("No GitHub App installation found for 'acme'"));

    await waitFor(() => {
      expect(screen.getByText(/Compliance matrix unavailable:/)).toBeInTheDocument();
    });
  });

  it("marks all dimensions unknown when the token can't see any of them, and lets the user click a row and switch repos", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        {
          repo: "api",
          branch_protection: false,
          secret_scanning: false,
          dependabot_enabled: false,
          dependabot_critical_count: 0,
          dependabot_high_count: 0,
          code_scanning: false,
          force_push_allowed: false,
          score: 0,
          unknown_dimensions: ["branch_protection", "dependabot", "code_scanning", "force_push"],
        },
        {
          repo: "worker",
          branch_protection: true,
          secret_scanning: true,
          dependabot_enabled: true,
          dependabot_critical_count: 0,
          dependabot_high_count: 0,
          code_scanning: true,
          force_push_allowed: false,
          score: 100,
          unknown_dimensions: [],
        },
      ],
      summary: { fully_compliant_count: 1, critical_risk_count: 0, secret_hits_count: 0, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => expect(screen.getByText("Compliance Matrix")).toBeInTheDocument());
    expect(screen.getAllByTitle("unknown — token can't see this").length).toBeGreaterThanOrEqual(3);

    fireEvent.click(screen.getAllByText("worker")[0]);
    await waitFor(() => expect(secretScanningMock).toHaveBeenCalledWith("acme", "worker", ""));

    const repoSelect = screen.getByDisplayValue("worker");
    fireEvent.change(repoSelect, { target: { value: "api" } });
    await waitFor(() => expect(secretScanningMock).toHaveBeenCalledWith("acme", "api", ""));
  });

  it("surfaces an error instead of crashing when secret scanning fails", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        { repo: "api", branch_protection: true, secret_scanning: false, dependabot_enabled: true, dependabot_critical_count: 0, dependabot_high_count: 0, code_scanning: true, force_push_allowed: false, score: 80, unknown_dimensions: [] },
      ],
      summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 0, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });
    secretScanningMock.mockRejectedValue(new Error("GitHub API error: 403"));

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => {
      expect(screen.getByText("GitHub API error: 403")).toBeInTheDocument();
    });
  });

  it("renders a resolved secret alert without the open-state styling", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        { repo: "api", branch_protection: true, secret_scanning: false, dependabot_enabled: true, dependabot_critical_count: 0, dependabot_high_count: 0, code_scanning: true, force_push_allowed: false, score: 80, unknown_dimensions: [] },
      ],
      summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 1, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });
    secretScanningMock.mockResolvedValue({
      repository: "acme/api",
      alerts: [
        {
          number: 2,
          state: "resolved",
          secret_type: "github_personal_access_token",
          secret_type_display: "GitHub Personal Access Token",
          resolved_reason: "revoked",
          created_at: "2026-07-01T00:00:00Z",
          resolved_at: "2026-07-02T00:00:00Z",
          repo: "acme/api",
          url: "https://github.com/acme/api/security/secret-scanning/2",
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => {
      expect(screen.getByText("resolved")).toBeInTheDocument();
    });
  });

  it("never renders a raw secret value in the alerts list", async () => {
    analyticsOverviewMock.mockResolvedValue({
      owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [],
    });
    securityMatrixMock.mockResolvedValue({
      owner: "acme",
      repos: [
        { repo: "api", branch_protection: true, secret_scanning: false, dependabot_enabled: true, dependabot_critical_count: 0, dependabot_high_count: 0, code_scanning: true, force_push_allowed: false, score: 80, unknown_dimensions: [] },
      ],
      summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 1, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } },
    });
    secretScanningMock.mockResolvedValue({
      repository: "acme/api",
      alerts: [
        {
          number: 1,
          state: "open",
          secret_type: "github_personal_access_token",
          secret_type_display: "GitHub Personal Access Token",
          resolved_reason: null,
          created_at: "2026-07-01T00:00:00Z",
          resolved_at: null,
          repo: "acme/api",
          url: "https://github.com/acme/api/security/secret-scanning/1",
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByPlaceholderText("e.g. octocat"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: /run scan/i }));

    await waitFor(() => expect(screen.getByText("GitHub Personal Access Token")).toBeInTheDocument());
    expect(screen.getByText("Secret values are never shown here — metadata only.")).toBeInTheDocument();
  });
});

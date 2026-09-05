import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSyncExternalStore } from "react";

// A minimal reactive store standing in for Next's router-driven searchParams so a
// tab click's router.replace(...) actually triggers a re-render in the test, the
// same way real navigation would (matches the pattern in security-page.test.tsx).
let mockSearchParams = new URLSearchParams();
const searchParamsListeners = new Set<() => void>();
const routerReplaceMock = vi.fn((url: string) => {
  mockSearchParams = new URLSearchParams(url.split("?")[1] ?? "");
  searchParamsListeners.forEach((listener) => listener());
});

vi.mock("next/navigation", () => ({
  useParams: () => ({ login: "acme" }),
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

const membersMock = vi.fn();
const outsideMock = vi.fn();
const pendingInvitationsMock = vi.fn();
const permissionAuditMock = vi.fn();
const inactiveMembersMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    invitations: {
      list: vi.fn().mockResolvedValue([]),
      revoke: vi.fn(),
      create: vi.fn(),
    },
    collab: {
      members: (...args: unknown[]) => membersMock(...args),
      outsideCollaborators: (...args: unknown[]) => outsideMock(...args),
      invitations: (...args: unknown[]) => pendingInvitationsMock(...args),
      membership: vi.fn(),
      permissionAudit: (...args: unknown[]) => permissionAuditMock(...args),
      inactiveMembers: (...args: unknown[]) => inactiveMembersMock(...args),
    },
    tokens: {
      resolve: vi.fn().mockRejectedValue(new Error("no saved token")),
    },
  },
}));

import OrgMembersPage from "@/app/settings/org/[login]/members/page";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgMembersPage />
    </QueryClientProvider>,
  );
}

describe("GithubRoster (Collaborators page)", () => {
  beforeEach(() => {
    membersMock.mockReset();
    outsideMock.mockReset();
    pendingInvitationsMock.mockReset();
    permissionAuditMock.mockReset();
    inactiveMembersMock.mockReset();
    routerReplaceMock.mockClear();
    mockSearchParams = new URLSearchParams();
    outsideMock.mockResolvedValue({ org: "acme", collaborators: [], repos_scanned: 0, repos_total: 0 });
    pendingInvitationsMock.mockResolvedValue({ org: "acme", invitations: [] });
    permissionAuditMock.mockResolvedValue({
      generated_at: new Date().toISOString(), repos_scanned: 0, repos_total: 0, repos: [],
      risk_summary: { outside_with_write_or_admin: 0, members_with_admin: 0, total_outside_collaborators: 0 },
    });
    inactiveMembersMock.mockResolvedValue({ org: "acme", sampled_repos: [], members: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders members with role badges and 2FA chips, and the footer count", async () => {
    membersMock.mockResolvedValue({
      org: "acme",
      members: [
        { login: "alice", avatar_url: "", role: "admin", site_admin: false, two_factor_enabled: true },
        { login: "bob", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: false },
      ],
      two_factor_overlay_available: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    expect(screen.getByText("bob")).toBeInTheDocument();
    expect(screen.getByText("✓ 2FA")).toBeInTheDocument();
    expect(screen.getByText("No 2FA")).toBeInTheDocument();
    expect(screen.getByText("Members without 2FA: 1")).toBeInTheDocument();
  });

  it("sorts the members table by clicking a sortable column header", async () => {
    membersMock.mockResolvedValue({
      org: "acme",
      members: [
        { login: "zoe", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: true },
        { login: "alice", avatar_url: "", role: "admin", site_admin: false, two_factor_enabled: true },
      ],
      two_factor_overlay_available: true,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText("zoe")).toBeInTheDocument());
    let rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("zoe");

    fireEvent.click(screen.getByRole("columnheader", { name: "Member" }));

    rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("alice");
    expect(rows[1]).toHaveTextContent("zoe");
  });

  it("omits the 2FA chip and footer when the overlay is unavailable", async () => {
    membersMock.mockResolvedValue({
      org: "acme",
      members: [{ login: "alice", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: null }],
      two_factor_overlay_available: false,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    expect(screen.queryByText("✓ 2FA")).not.toBeInTheDocument();
    expect(screen.queryByText("No 2FA")).not.toBeInTheDocument();
    expect(screen.queryByText(/Members without 2FA/)).not.toBeInTheDocument();
  });

  it("recomputes the 'Members without 2FA' count from the search-filtered members, not the full roster", async () => {
    membersMock.mockResolvedValue({
      org: "acme",
      members: [
        { login: "alice", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: false },
        { login: "bob", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: false },
      ],
      two_factor_overlay_available: true,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText("Members without 2FA: 2")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Search by login…"), { target: { value: "alice" } });

    await waitFor(() => expect(screen.getByText("Members without 2FA: 1")).toBeInTheDocument());
    expect(screen.queryByText("bob")).not.toBeInTheDocument();
  });

  it("shows an 'Updating…' indicator while a role-filter change refetches, distinct from client-side search", async () => {
    let resolveRefetch: (v: unknown) => void = () => {};
    membersMock
      .mockResolvedValueOnce({ org: "acme", members: [], two_factor_overlay_available: true })
      .mockReturnValueOnce(new Promise((resolve) => { resolveRefetch = resolve }));

    renderPage();
    await waitFor(() => expect(membersMock).toHaveBeenCalledWith("acme", "all", undefined));
    await waitFor(() => expect(screen.getByText("No members found")).toBeInTheDocument());

    expect(screen.queryByText(/Updating…/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue("All roles"), { target: { value: "admin" } });

    await waitFor(() => expect(screen.getByText(/Updating…/)).toBeInTheDocument());

    resolveRefetch({ org: "acme", members: [], two_factor_overlay_available: true });
    await waitFor(() => expect(screen.queryByText(/Updating…/)).not.toBeInTheDocument());
  });

  it("only fetches the active tab's data, gating other tabs", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });

    renderPage();

    await waitFor(() => {
      expect(membersMock).toHaveBeenCalled();
    });
    expect(outsideMock).not.toHaveBeenCalled();
    expect(pendingInvitationsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Outside" }));

    await waitFor(() => {
      expect(outsideMock).toHaveBeenCalled();
    });
  });

  it("shows the repo-scan cap note when outside collaborators are capped", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    outsideMock.mockResolvedValue({
      org: "acme",
      collaborators: [{ login: "carol", avatar_url: "", repos: ["acme/api"] }],
      repos_scanned: 50,
      repos_total: 140,
    });

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Outside" }));

    await waitFor(() => {
      expect(screen.getByText("Scanned 50 of 140 repos")).toBeInTheDocument();
    });
  });

  it("renders pending GitHub invitations distinctly from Clevis workspace invitations", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    pendingInvitationsMock.mockResolvedValue({
      org: "acme",
      invitations: [
        { login: "dave", email: null, role: "member", invited_at: "2026-07-01T00:00:00Z", inviter: "alice" },
      ],
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Clevis workspace invitations")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Pending GitHub invitations" }));

    await waitFor(() => {
      expect(screen.getByText("dave")).toBeInTheDocument();
    });
  });

  it("shows an empty state under Pending GitHub invitations when there are none", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Clevis workspace invitations")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Pending GitHub invitations" }));

    await waitFor(() => {
      expect(screen.getByText("No pending GitHub invitations")).toBeInTheDocument();
    });
  });

  it("filters the visible members by the search input", async () => {
    membersMock.mockResolvedValue({
      org: "acme",
      members: [
        { login: "alice", avatar_url: "", role: "admin", site_admin: false, two_factor_enabled: true },
        { login: "bob", avatar_url: "", role: "member", site_admin: false, two_factor_enabled: false },
      ],
      two_factor_overlay_available: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText("Search by login…"), { target: { value: "ali" } });

    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.queryByText("bob")).not.toBeInTheDocument();
  });

  it("refetches members with the selected role filter", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });

    renderPage();

    await waitFor(() => {
      expect(membersMock).toHaveBeenCalledWith("acme", "all", undefined);
    });

    fireEvent.change(screen.getByDisplayValue("All roles"), { target: { value: "admin" } });

    await waitFor(() => {
      expect(membersMock).toHaveBeenCalledWith("acme", "admin", undefined);
    });
  });

  it("surfaces an error message when the members query fails", async () => {
    membersMock.mockRejectedValue(new Error("No GitHub App installation found"));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("No GitHub App installation found")).toBeInTheDocument();
    });
  });

  it("highlights outside collaborators with write/admin access in the Audit tab", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    permissionAuditMock.mockResolvedValue({
      generated_at: new Date().toISOString(),
      repos_scanned: 1,
      repos_total: 1,
      repos: [
        {
          repo: "acme/api",
          collaborators: [
            { login: "carol", avatar_url: "", permission: "write", affiliation: "outside", is_outside_collaborator: true },
            { login: "alice", avatar_url: "", permission: "admin", affiliation: "direct", is_outside_collaborator: false },
          ],
        },
      ],
      risk_summary: { outside_with_write_or_admin: 1, members_with_admin: 1, total_outside_collaborators: 1 },
    });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(permissionAuditMock).toHaveBeenCalled();
    });
    expect(await screen.findByText(/Access Risk: 1 outside collaborator/)).toBeInTheDocument();
    expect(screen.getByText("carol")).toBeInTheDocument();
  });

  it("lists inactive members with an approximation note in the Audit tab", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    inactiveMembersMock.mockResolvedValue({
      org: "acme",
      sampled_repos: ["acme/api"],
      members: [{ login: "dave", avatar_url: "", role: "member", last_commit_repo: null, last_commit_days_ago: null }],
    });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(inactiveMembersMock).toHaveBeenCalled();
    });
    expect(await screen.findByText("dave")).toBeInTheDocument();
    expect(screen.getByText(/approximation, not exact/)).toBeInTheDocument();
  });

  it("shows the partial-scan note when the permission audit didn't scan every repo", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    permissionAuditMock.mockResolvedValue({
      generated_at: new Date().toISOString(),
      repos_scanned: 5,
      repos_total: 12,
      repos: [],
      risk_summary: { outside_with_write_or_admin: 0, members_with_admin: 0, total_outside_collaborators: 0 },
    });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(permissionAuditMock).toHaveBeenCalled();
    });
    expect(await screen.findByText(/scanned 5 of 12 repos/)).toBeInTheDocument();
  });

  it("shows a loading indicator for inactive members while the query is pending", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    inactiveMembersMock.mockImplementation(() => new Promise(() => {}));

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(inactiveMembersMock).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.getByText("Loading…")).toBeInTheDocument();
    });
  });

  it("shows an error instead of a false-clean empty state when inactive members fails", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    inactiveMembersMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(screen.getByText("GitHub API unreachable")).toBeInTheDocument();
    });
  });

  it("still shows inactive members that loaded fine when the permission audit call fails (#252)", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    permissionAuditMock.mockRejectedValue(new Error("GitHub rate limit exceeded"));
    inactiveMembersMock.mockResolvedValue({
      org: "acme",
      sampled_repos: ["acme/api"],
      members: [{ login: "frank", avatar_url: "", role: "member", last_commit_repo: null, last_commit_days_ago: null }],
    });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    // Previously the whole Audit tab collapsed into a single generic error line whenever
    // permissionAuditQuery failed, hiding the independently-fetched (and successfully
    // loaded) inactive-members section entirely -- both must render their own state.
    await waitFor(() => {
      expect(screen.getByText("GitHub rate limit exceeded")).toBeInTheDocument();
    });
    expect(await screen.findByText("frank")).toBeInTheDocument();
  });

  it("shows how long ago an inactive member's last commit was", async () => {
    membersMock.mockResolvedValue({ org: "acme", members: [], two_factor_overlay_available: true });
    inactiveMembersMock.mockResolvedValue({
      org: "acme",
      sampled_repos: ["acme/api"],
      members: [{ login: "erin", avatar_url: "", role: "member", last_commit_repo: "acme/api", last_commit_days_ago: 45 }],
    });

    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));

    await waitFor(() => {
      expect(screen.getByText(/last commit 45d ago in acme\/api/)).toBeInTheDocument();
    });
  });
});

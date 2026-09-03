import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useParams: () => ({ login: "acme" }),
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const membersMock = vi.fn();
const outsideMock = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    invitations: { list: vi.fn().mockResolvedValue([]), revoke: vi.fn(), create: vi.fn() },
    collab: {
      members: (...args: unknown[]) => membersMock(...args),
      outsideCollaborators: (...args: unknown[]) => outsideMock(...args),
      invitations: vi.fn().mockResolvedValue({ org: "acme", invitations: [] }),
      permissionAudit: vi.fn(),
      inactiveMembers: vi.fn(),
      membership: vi.fn(),
    },
    tokens: { resolve: vi.fn().mockRejectedValue(new Error("no saved token")) },
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

describe("OrgMembersPage avatars", () => {
  beforeEach(() => {
    searchParams = new URLSearchParams();
    membersMock.mockReset();
    outsideMock.mockReset();
    membersMock.mockResolvedValue({
      org: "acme",
      two_factor_overlay_available: true,
      members: [
        { login: "octocat", avatar_url: "https://avatars/octocat.png", role: "admin", site_admin: false, two_factor_enabled: true },
      ],
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders member avatars as decorative with the login as visible text", async () => {
    const { container } = renderPage();

    await waitFor(() => expect(screen.getByText("octocat")).toBeInTheDocument());

    const avatar = container.querySelector('img[src="https://avatars/octocat.png"]');
    expect(avatar).not.toBeNull();
    // Decorative image: the login link is the accessible label, so alt must stay empty.
    expect(avatar).toHaveAttribute("alt", "");
  });

  it("renders outside-collaborator avatars as decorative with the login as visible text", async () => {
    searchParams = new URLSearchParams("roster=outside");
    outsideMock.mockResolvedValue({
      org: "acme",
      collaborators: [
        { login: "ext-dev", avatar_url: "https://avatars/ext-dev.png", repos: ["acme/api"] },
      ],
      repos_scanned: 1,
      repos_total: 1,
    });

    const { container } = renderPage();

    await waitFor(() => expect(screen.getByText("ext-dev")).toBeInTheDocument());

    const avatar = container.querySelector('img[src="https://avatars/ext-dev.png"]');
    expect(avatar).not.toBeNull();
    // Decorative image: the collaborator login is the adjacent text, so alt must stay empty.
    expect(avatar).toHaveAttribute("alt", "");
  });
});

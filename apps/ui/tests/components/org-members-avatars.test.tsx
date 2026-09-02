import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useParams: () => ({ login: "acme" }),
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
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
});

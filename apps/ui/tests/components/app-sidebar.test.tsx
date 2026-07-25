import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/",
}));

import { AppSidebar } from "@/components/app-sidebar";
import { AuthProvider } from "@/lib/auth-context";
import { SidebarProvider } from "@/components/ui/sidebar";

const TOKEN_KEY = "clevis:token";

function b64url(value: object): string {
  return btoa(JSON.stringify(value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function makeJwt(): string {
  const header = b64url({ alg: "none", typ: "JWT" });
  const payload = b64url({
    sub: "1",
    email: "user@example.com",
    name: "User",
    is_workspace_admin: false,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return `${header}.${payload}.sig`;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderSidebar() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <SidebarProvider>
          <AppSidebar />
        </SidebarProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

// Mutable per-test fixture for the /me/orgs response; reset in beforeEach.
let orgMemberships: { org_login: string; role: "admin" | "member" }[] = [];
// Mutable per-test fixture for the /me/installations response; reset in beforeEach.
let installations: { id: number; account_login: string; account_type: string; installation_id: number | null; created_at: string }[] = [];
// Mutable per-test fixture for the cockpit response driving the health dot/badge.
let cockpitResponse: {
  latest_score: number | null;
  recent_events: { id: string; type: string; actor: string; actor_avatar: string; repo: string; summary: string; created_at: string }[];
} = { latest_score: null, recent_events: [] };

describe("AppSidebar Invite members button", () => {
  beforeEach(() => {
    localStorage.clear();
    replace.mockClear();
    localStorage.setItem(TOKEN_KEY, makeJwt());
    orgMemberships = [];
    installations = [];
    cockpitResponse = { latest_score: null, recent_events: [] };

    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(
            jsonResponse({ id: 1, email: "user@example.com", name: "User", is_workspace_admin: false }),
          );
        }
        if (url.endsWith("/me/orgs")) {
          return Promise.resolve(jsonResponse(orgMemberships));
        }
        if (url.endsWith("/me/installations")) {
          return Promise.resolve(jsonResponse(installations));
        }
        if (url.endsWith("/tokens/resolve")) {
          return Promise.resolve(jsonResponse({ detail: "No saved token for this org" }, 404));
        }
        if (url.includes("/me/analytics/cockpit/")) {
          return Promise.resolve(
            jsonResponse({
              repo_count: 0,
              member_count: 0,
              latest_score: cockpitResponse.latest_score,
              score_trend: [],
              recent_events: cockpitResponse.recent_events,
              open_pr_count: 0,
              pr_merge_rate_4w: [],
              commit_activity_4w: [],
              total_cache_size_bytes: 0,
              cache_job_success_rate: 0,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("links to the org's members page when a default_org is set and the user is admin there", async () => {
    orgMemberships = [{ org_login: "acme", role: "admin" }];
    localStorage.setItem("default_org", "acme");
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));

    const link = await screen.findByRole("link", { name: /invite members/i });
    await waitFor(() => expect(link).toHaveAttribute("href", "/settings/org/acme/members"));
  });

  it("links to Settings when no default_org is set, instead of being a dead disabled button", async () => {
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /guest|user/i }));

    const link = await screen.findByRole("link", { name: /invite members/i });
    expect(link).toHaveAttribute("href", "/settings");
    expect(link).not.toHaveAttribute("disabled");
  });

  it("falls back to the first admin org when default_org is set but the user isn't admin there", async () => {
    orgMemberships = [
      { org_login: "acme", role: "member" },
      { org_login: "widgets-inc", role: "admin" },
    ];
    localStorage.setItem("default_org", "acme");
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));

    const link = await screen.findByRole("link", { name: /invite members/i });
    await waitFor(() => expect(link).toHaveAttribute("href", "/settings/org/widgets-inc/members"));
  });

  it("falls back to Settings when default_org is set but the user is admin of no org", async () => {
    orgMemberships = [{ org_login: "acme", role: "member" }];
    localStorage.setItem("default_org", "acme");
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));

    const link = await screen.findByRole("link", { name: /invite members/i });
    await waitFor(() => expect(link).toHaveAttribute("href", "/settings"));
  });
});

describe("AppSidebar health dot and unread badge", () => {
  beforeEach(() => {
    localStorage.clear();
    replace.mockClear();
    localStorage.setItem(TOKEN_KEY, makeJwt());
    localStorage.setItem("default_org", "acme");
    orgMemberships = [{ org_login: "acme", role: "admin" }];
    installations = [];
    cockpitResponse = { latest_score: null, recent_events: [] };

    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(
            jsonResponse({ id: 1, email: "user@example.com", name: "User", is_workspace_admin: false }),
          );
        }
        if (url.endsWith("/me/orgs")) {
          return Promise.resolve(jsonResponse(orgMemberships));
        }
        if (url.endsWith("/me/installations")) {
          return Promise.resolve(jsonResponse(installations));
        }
        if (url.endsWith("/tokens/resolve")) {
          return Promise.resolve(jsonResponse({ detail: "No saved token for this org" }, 404));
        }
        if (url.includes("/me/analytics/cockpit/")) {
          return Promise.resolve(
            jsonResponse({
              repo_count: 0,
              member_count: 0,
              latest_score: cockpitResponse.latest_score,
              score_trend: [],
              recent_events: cockpitResponse.recent_events,
              open_pr_count: 0,
              pr_merge_rate_4w: [],
              commit_activity_4w: [],
              total_cache_size_bytes: 0,
              cache_job_success_rate: 0,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows no dot when there's no score yet", async () => {
    renderSidebar();
    const link = await screen.findByRole("link", { name: "Health & Security" });
    expect(link.querySelector(".bg-green-400, .bg-yellow-400, .bg-red-400")).toBeNull();
  });

  it("shows a green dot for a high score", async () => {
    cockpitResponse = { latest_score: 90, recent_events: [] };
    renderSidebar();

    const link = await screen.findByRole("link", { name: "Health & Security" });
    await waitFor(() => expect(link.querySelector(".bg-green-400")).not.toBeNull());
  });

  it("shows a yellow dot for a mid score", async () => {
    cockpitResponse = { latest_score: 60, recent_events: [] };
    renderSidebar();

    const link = await screen.findByRole("link", { name: "Health & Security" });
    await waitFor(() => expect(link.querySelector(".bg-yellow-400")).not.toBeNull());
  });

  it("shows a red dot for a low score", async () => {
    cockpitResponse = { latest_score: 20, recent_events: [] };
    renderSidebar();

    const link = await screen.findByRole("link", { name: "Health & Security" });
    await waitFor(() => expect(link.querySelector(".bg-red-400")).not.toBeNull());
  });

  it("shows an unread badge counting events newer than the last-seen timestamp", async () => {
    localStorage.setItem("activity_last_seen_at", "2026-07-01T00:00:00Z");
    cockpitResponse = {
      latest_score: null,
      recent_events: [
        { id: "1", type: "PushEvent", actor: "alice", actor_avatar: "", repo: "acme/api", summary: "pushed", created_at: "2026-07-15T00:00:00Z" },
        { id: "2", type: "PushEvent", actor: "bob", actor_avatar: "", repo: "acme/api", summary: "pushed", created_at: "2026-06-01T00:00:00Z" },
      ],
    };
    renderSidebar();

    const link = await screen.findByRole("link", { name: /Activity/ });
    await waitFor(() => expect(link).toHaveTextContent("1"));
  });

  it("shows no unread badge when there are no events newer than last-seen", async () => {
    localStorage.setItem("activity_last_seen_at", "2026-07-20T00:00:00Z");
    cockpitResponse = {
      latest_score: null,
      recent_events: [
        { id: "1", type: "PushEvent", actor: "alice", actor_avatar: "", repo: "acme/api", summary: "pushed", created_at: "2026-07-15T00:00:00Z" },
      ],
    };
    renderSidebar();

    await screen.findByRole("link", { name: /Activity/ });
    const link = screen.getByRole("link", { name: /Activity/ });
    await waitFor(() => expect(link.querySelector(".bg-primary\\/20")).toBeNull());
  });
});

describe("AppSidebar coming-soon badges", () => {
  beforeEach(() => {
    localStorage.clear();
    replace.mockClear();
    localStorage.setItem(TOKEN_KEY, makeJwt());
    orgMemberships = [];
    installations = [];
    cockpitResponse = { latest_score: null, recent_events: [] };

    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(
            jsonResponse({ id: 1, email: "user@example.com", name: "User", is_workspace_admin: false }),
          );
        }
        if (url.endsWith("/me/orgs")) {
          return Promise.resolve(jsonResponse(orgMemberships));
        }
        if (url.endsWith("/me/installations")) {
          return Promise.resolve(jsonResponse(installations));
        }
        if (url.endsWith("/tokens/resolve")) {
          return Promise.resolve(jsonResponse({ detail: "No saved token for this org" }, 404));
        }
        if (url.includes("/me/analytics/cockpit/")) {
          return Promise.resolve(
            jsonResponse({
              repo_count: 0,
              member_count: 0,
              latest_score: cockpitResponse.latest_score,
              score_trend: [],
              recent_events: cockpitResponse.recent_events,
              open_pr_count: 0,
              pr_merge_rate_4w: [],
              commit_activity_4w: [],
              total_cache_size_bytes: 0,
              cache_job_success_rate: 0,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each(["Pull Requests"])(
    "shows a 'Soon' badge on the unshipped '%s' nav item",
    async (title) => {
      renderSidebar();
      const link = await screen.findByRole("link", { name: new RegExp(title) });
      expect(link).toHaveTextContent("Soon");
    },
  );

  it.each([
    "Overview",
    "Activity",
    "Releases",
    "Repositories",
    "Health & Security",
    "Collaborators",
    "Automation",
    "Audit Log",
    "Job Queue",
    "My PRs",
    "My Reviews",
    "My Issues",
  ])(
    "shows no 'Soon' badge on the shipped '%s' nav item",
    async (title) => {
      renderSidebar();
      const link = await screen.findByRole("link", { name: new RegExp(title) });
      expect(link).not.toHaveTextContent("Soon");
    },
  );
});

describe("AppSidebar scope switcher", () => {
  beforeEach(() => {
    localStorage.clear();
    replace.mockClear();
    localStorage.setItem(TOKEN_KEY, makeJwt());
    orgMemberships = [{ org_login: "acme", role: "admin" }];
    installations = [];
    cockpitResponse = { latest_score: null, recent_events: [] };

    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(
            jsonResponse({ id: 1, email: "user@example.com", name: "User", is_workspace_admin: false }),
          );
        }
        if (url.endsWith("/me/orgs")) {
          return Promise.resolve(jsonResponse(orgMemberships));
        }
        if (url.endsWith("/me/installations")) {
          return Promise.resolve(jsonResponse(installations));
        }
        if (url.endsWith("/tokens/resolve")) {
          return Promise.resolve(jsonResponse({ detail: "No saved token for this org" }, 404));
        }
        if (url.includes("/me/analytics/cockpit/")) {
          return Promise.resolve(
            jsonResponse({
              repo_count: 0,
              member_count: 0,
              latest_score: cockpitResponse.latest_score,
              score_trend: [],
              recent_events: cockpitResponse.recent_events,
              open_pr_count: 0,
              pr_merge_rate_4w: [],
              commit_activity_4w: [],
              total_cache_size_bytes: 0,
              cache_job_success_rate: 0,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("lists the personal account and org memberships, marking the active scope", async () => {
    installations = [
      { id: 1, account_login: "octocat", account_type: "User", installation_id: 5, created_at: "2026-01-01T00:00:00Z" },
    ];
    localStorage.setItem("active_scope", JSON.stringify({ kind: "org", login: "acme" }));
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));

    const acmeRow = await screen.findByRole("button", { name: /acme/i });
    const personalRow = await screen.findByRole("button", { name: /octocat/i });
    expect(acmeRow.querySelector("svg")).not.toBeNull();
    expect(personalRow.querySelector("svg")).toBeNull();
  });

  it("switches the active scope when a different option is selected", async () => {
    installations = [
      { id: 1, account_login: "octocat", account_type: "User", installation_id: 5, created_at: "2026-01-01T00:00:00Z" },
    ];
    localStorage.setItem("active_scope", JSON.stringify({ kind: "org", login: "acme" }));
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));
    const personalRow = await screen.findByRole("button", { name: /octocat/i });
    fireEvent.click(personalRow);

    await waitFor(() =>
      expect(localStorage.getItem("active_scope")).toBe(JSON.stringify({ kind: "personal", login: "octocat" })),
    );
  });

  it("shows a connect-personal-account prompt when no personal installation exists", async () => {
    vi.stubEnv("NEXT_PUBLIC_GITHUB_APP_SLUG", "clevis");
    installations = [];
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /user/i }));

    const connectLink = await screen.findByRole("link", { name: /connect your personal github account/i });
    expect(connectLink).toHaveAttribute("href", "https://github.com/apps/clevis/installations/new");
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const orgsMineMock = vi.fn();
const installationsListMock = vi.fn();
const installationsListForOrgMock = vi.fn();
const installationsRemoveMock = vi.fn();
const tokensListMock = vi.fn();
const configGetAllMock = vi.fn();
const patchMeMock = vi.fn();
const revokeSessionsMock = vi.fn();
const configUpdateMock = vi.fn();
const routerReplace = vi.fn();
let searchParams = new URLSearchParams();

// AuthProvider (wrapping SettingsPage below) calls the real global `fetch` directly
// (not the mocked api client) to confirm the session against /auth/me. Left unmocked,
// every test attempts a real network call to localhost:8080; on the first failure it
// waits a real 2s before retrying (see auth-context.tsx's checkMe), which is slow and,
// worse, timing-dependent on this machine's TCP-refusal latency -- rendering
// `waitFor`'s default 1s timeout unreliable for anything else the test is waiting on.
// Stub it to resolve immediately so tests don't pay for or depend on that retry.
const fetchMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
  useSearchParams: () => searchParams,
}));

vi.mock("@/lib/api/client", () => ({
  api: {
    orgs: { mine: (...args: unknown[]) => orgsMineMock(...args) },
    installations: {
      list: (...args: unknown[]) => installationsListMock(...args),
      listForOrg: (...args: unknown[]) => installationsListForOrgMock(...args),
      remove: (...args: unknown[]) => installationsRemoveMock(...args),
    },
    tokens: { list: (...args: unknown[]) => tokensListMock(...args) },
    config: {
      getAll: (...args: unknown[]) => configGetAllMock(...args),
      update: (...args: unknown[]) => configUpdateMock(...args),
    },
    auth: {
      patchMe: (...args: unknown[]) => patchMeMock(...args),
      revokeSessions: (...args: unknown[]) => revokeSessionsMock(...args),
    },
  },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

import { AuthProvider } from "@/lib/auth-context";
import SettingsPage from "@/app/settings/page";

const TOKEN_KEY = "clevis:token";

function b64url(value: object): string {
  return btoa(JSON.stringify(value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function makeAdminJwt(): string {
  const header = b64url({ alg: "none", typ: "JWT" });
  const payload = b64url({
    sub: "1",
    email: "admin@example.com",
    name: "Admin",
    is_workspace_admin: true,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return `${header}.${payload}.`;
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <SettingsPage />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("SettingsPage", () => {
  beforeEach(() => {
    orgsMineMock.mockReset();
    installationsListMock.mockReset();
    installationsListForOrgMock.mockReset();
    installationsRemoveMock.mockReset();
    tokensListMock.mockReset();
    configGetAllMock.mockReset();
    patchMeMock.mockReset();
    revokeSessionsMock.mockReset();
    configUpdateMock.mockReset();
    routerReplace.mockClear();
    searchParams = new URLSearchParams();
    localStorage.clear();
    localStorage.setItem(TOKEN_KEY, makeAdminJwt());
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ id: 1, email: "admin@example.com", name: "Admin", is_workspace_admin: true }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders the profile section and surfaces a retry when a section errors, and shows instance config for admins", async () => {
    orgsMineMock.mockRejectedValue(new Error("Failed to load organizations."));
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    expect(screen.getByRole("button", { name: "Save profile" })).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Failed to load organizations.")).toBeInTheDocument();
    });
    // Two independent cards read the same failed "my-orgs" query -- OrgMembershipsSection
    // and ConnectedOrgsSection (which also needs it to know which orgs the caller admins) --
    // so each surfaces its own retry affordance.
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThanOrEqual(1);

    await waitFor(() => {
      expect(screen.getByText("Instance configuration")).toBeInTheDocument();
    });
    expect(screen.getAllByRole("button", { name: "Save" }).length).toBeGreaterThan(0);
  });

  it("shows a saving spinner then a saved confirmation when the profile is updated", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    const patchGate = deferred<{ id: number; email: string; name: string | null; is_workspace_admin: boolean }>();
    patchMeMock.mockReturnValue(patchGate.promise);

    renderPage();

    const nameInput = screen.getByPlaceholderText("Your name");
    fireEvent.change(nameInput, { target: { value: "New Name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Saving…/ })).toBeInTheDocument();
    });

    await act(async () => {
      patchGate.resolve({ id: 1, email: "admin@example.com", name: "New Name", is_workspace_admin: true });
      await patchGate.promise;
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Saved/ })).toBeInTheDocument();
    });
  });

  it("shows a saving spinner on the instance config field being saved", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    const updateGate = deferred<Record<string, string>>();
    configUpdateMock.mockReturnValue(updateGate.promise);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Instance configuration")).toBeInTheDocument();
    });

    const saveButtons = screen.getAllByRole("button", { name: "Save" });
    fireEvent.click(saveButtons[0]);

    await waitFor(() => {
      expect(saveButtons[0]).toBeDisabled();
    });

    await act(async () => {
      updateGate.resolve({ worker_poll_seconds: "5", registration_enabled: "true" });
      await updateGate.promise;
    });
  });

  it("saves a chosen leadership-digest cadence", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({
      worker_poll_seconds: "5",
      registration_enabled: "true",
      digest_cadence: "off",
    });
    configUpdateMock.mockResolvedValue({ digest_cadence: "weekly" });

    renderPage();

    const cadence = await screen.findByDisplayValue("Off");
    fireEvent.change(cadence, { target: { value: "weekly" } });

    const row = cadence.closest("div")!.parentElement!;
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(configUpdateMock).toHaveBeenCalledWith("digest_cadence", "weekly"));
  });

  it("saves the visible cadence value even when it was never in the server config", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    // digest_cadence absent from read_all() -> select shows "Off" but has no state entry.
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });
    configUpdateMock.mockResolvedValue({ digest_cadence: "off" });

    renderPage();

    const cadence = await screen.findByDisplayValue("Off");
    const row = cadence.closest("div")!.parentElement!;
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    // Must send "off", not "" (which _ENUM_KEYS would 422).
    await waitFor(() => expect(configUpdateMock).toHaveBeenCalledWith("digest_cadence", "off"));
  });

  it("renders a persisted non-default cadence and associates the label with the select", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({
      worker_poll_seconds: "5",
      registration_enabled: "true",
      digest_cadence: "monthly",
    });

    renderPage();

    // The label is programmatically tied to the control (htmlFor / id), so
    // getByLabelText resolves it, and the persisted value is what shows.
    const cadence = (await screen.findByLabelText("Leadership Digest")) as HTMLSelectElement;
    expect(cadence.tagName).toBe("SELECT");
    expect(cadence.value).toBe("monthly");
    expect(screen.getByDisplayValue("Monthly")).toBe(cadence);
  });

  it("shows a success banner and strips the query param when landing with ?installed=1", async () => {
    searchParams = new URLSearchParams({ installed: "1" });
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("GitHub App installation connected.")).toBeInTheDocument();
    });
    expect(routerReplace).toHaveBeenCalledWith("/settings");
  });

  it("requires a second click to confirm revoking all sessions, then calls the endpoint", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });
    revokeSessionsMock.mockResolvedValue({ ok: true });

    renderPage();

    const revokeButton = await screen.findByRole("button", { name: /sign out of all devices/i });
    fireEvent.click(revokeButton);
    expect(revokeSessionsMock).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: /click again to confirm/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /click again to confirm/i }));

    await waitFor(() => expect(revokeSessionsMock).toHaveBeenCalledTimes(1));
    // logout() clears the token, which removes the authenticated Settings page entirely.
    await waitFor(() => expect(localStorage.getItem(TOKEN_KEY)).toBeNull());
  });

  // ── Connected accounts (installation-connect-disconnect-ux) ────────────────

  it("shows an unconfigured message instead of the install button when the App slug isn't set", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/GitHub App integration isn.t set up on this instance yet/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /install github app/i })).not.toBeInTheDocument();
  });

  it("shows an empty state when nothing is connected", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/No accounts connected yet/i)).toBeInTheDocument();
    });
  });

  it("shows a permission-drift notice under a connected account that's missing scopes", async () => {
    orgsMineMock.mockResolvedValue([{ org_login: "acme", role: "admin" }]);
    installationsListMock.mockResolvedValue([]);
    installationsListForOrgMock.mockResolvedValue([
      {
        id: 2,
        account_login: "acme",
        account_type: "Organization",
        installation_id: 42,
        created_at: "2026-01-02T00:00:00Z",
        permissions_synced_at: "2026-09-01T00:00:00Z",
        blocked_features: [
          { feature: "stale_pr_nudges", label: "Stale pull-request nudges", missing: { pull_requests: "write" } },
        ],
      },
    ]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/needs extra GitHub access/i)).toBeInTheDocument();
      expect(screen.getByText("Stale pull-request nudges")).toBeInTheDocument();
    });
  });

  it("relabels the install button once an account is already connected", async () => {
    vi.stubEnv("NEXT_PUBLIC_GITHUB_APP_SLUG", "clevis");
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "shabnam", account_type: "User", installation_id: 7, created_at: "2026-01-01T00:00:00Z" },
    ]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    expect(await screen.findByRole("button", { name: /install on another account or org/i })).toBeInTheDocument();
    vi.unstubAllEnvs();
  });

  it("lists both personal and admin-org installations, and disconnects one after a confirm click", async () => {
    orgsMineMock.mockResolvedValue([{ org_login: "acme", role: "admin" }]);
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "shabnam", account_type: "User", installation_id: 7, created_at: "2026-01-01T00:00:00Z" },
    ]);
    installationsListForOrgMock.mockResolvedValue([
      { id: 2, account_login: "acme", account_type: "Organization", installation_id: 42, created_at: "2026-01-02T00:00:00Z" },
    ]);
    installationsRemoveMock.mockResolvedValue(undefined);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    // "acme" also appears in the separate "Your organizations" membership card (which
    // resolves independently, from orgsMineMock alone) -- waiting on that text alone
    // would race ahead of the "Connected GitHub accounts" table's own org-scoped
    // installation row, which only appears once orgInstallQueries (chained after
    // membershipsQuery) settles. Wait on the actual two Disconnect buttons instead.
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /disconnect/i })).toHaveLength(2);
    });
    expect(installationsListForOrgMock).toHaveBeenCalledWith("acme");

    const disconnectButtons = screen.getAllByRole("button", { name: /disconnect/i });

    fireEvent.click(disconnectButtons[0]);
    expect(installationsRemoveMock).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/shabnam/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /^disconnect$/i }));

    await waitFor(() => {
      expect(installationsRemoveMock).toHaveBeenCalledWith({ scope: "me" }, 7);
    });
  });

  it("disconnects an org-scoped installation with the org scope, not the personal one", async () => {
    orgsMineMock.mockResolvedValue([{ org_login: "acme", role: "admin" }]);
    installationsListMock.mockResolvedValue([]);
    installationsListForOrgMock.mockResolvedValue([
      { id: 2, account_login: "acme", account_type: "Organization", installation_id: 42, created_at: "2026-01-02T00:00:00Z" },
    ]);
    installationsRemoveMock.mockResolvedValue(undefined);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    const disconnectButton = await screen.findByRole("button", { name: /disconnect/i });
    fireEvent.click(disconnectButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^disconnect$/i }));

    await waitFor(() => {
      expect(installationsRemoveMock).toHaveBeenCalledWith({ scope: "org", orgLogin: "acme" }, 42);
    });
  });

  it("does not fetch installations for orgs the caller is only a member of, not an admin", async () => {
    orgsMineMock.mockResolvedValue([{ org_login: "acme", role: "member" }]);
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/No accounts connected yet/i)).toBeInTheDocument();
    });
    expect(installationsListForOrgMock).not.toHaveBeenCalled();
  });

  it("retries all three connected-accounts queries when Retry is clicked", async () => {
    orgsMineMock.mockRejectedValue(new Error("boom"));
    installationsListMock.mockResolvedValue([]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Failed to load connected accounts.")).toBeInTheDocument();
    });

    orgsMineMock.mockClear();
    installationsListMock.mockClear();
    const retryButtons = screen.getAllByRole("button", { name: "Retry" });
    fireEvent.click(retryButtons[retryButtons.length - 1]);

    await waitFor(() => {
      expect(orgsMineMock).toHaveBeenCalled();
      expect(installationsListMock).toHaveBeenCalled();
    });
  });

  it("shows a spinner on the row being disconnected while the request is in flight", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "shabnam", account_type: "User", installation_id: 7, created_at: "2026-01-01T00:00:00Z" },
    ]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });
    const removeGate = deferred<void>();
    installationsRemoveMock.mockReturnValue(removeGate.promise);

    renderPage();

    const disconnectButton = await screen.findByRole("button", { name: /disconnect/i });
    fireEvent.click(disconnectButton);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^disconnect$/i }));

    await waitFor(() => {
      expect(installationsRemoveMock).toHaveBeenCalled();
    });
    // The row's own button goes back to plain "Disconnect" while the confirm dialog
    // shows a busy state instead of a second confirm click.
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /working/i })).toBeDisabled();

    await act(async () => {
      removeGate.resolve();
      await removeGate.promise;
    });
  });

  it("shows an error message and lets the user try again when disconnect fails", async () => {
    orgsMineMock.mockResolvedValue([]);
    installationsListMock.mockResolvedValue([
      { id: 1, account_login: "shabnam", account_type: "User", installation_id: 7, created_at: "2026-01-01T00:00:00Z" },
    ]);
    tokensListMock.mockResolvedValue([]);
    configGetAllMock.mockResolvedValue({ worker_poll_seconds: "5", registration_enabled: "true" });
    installationsRemoveMock.mockRejectedValue(new Error("GitHub API unreachable"));

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /disconnect/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^disconnect$/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("GitHub API unreachable");
    });
    // The row is still there (not silently removed) and can be retried immediately --
    // the dialog was closed on error, not left stuck open.
    expect(screen.getByText("shabnam")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

});

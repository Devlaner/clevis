import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth, type AuthUser } from "@/lib/auth-context";

const TOKEN_KEY = "clevis:token";

function b64url(value: object): string {
  return btoa(JSON.stringify(value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function makeJwt(sub: number, email: string): string {
  const header = b64url({ alg: "none", typ: "JWT" });
  const payload = b64url({
    sub: String(sub),
    email,
    name: null,
    is_workspace_admin: false,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return `${header}.${payload}.sig`;
}

function makeJwtForUser(user: AuthUser): string {
  const header = b64url({ alg: "none", typ: "JWT" });
  const payload = b64url({
    sub: String(user.id),
    email: user.email,
    name: user.name,
    is_workspace_admin: user.is_workspace_admin,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return `${header}.${payload}.sig`;
}

const passwordUser: AuthUser = {
  id: 2,
  email: "password@example.com",
  name: "Password User",
  is_workspace_admin: false,
};

const cookieUser: AuthUser = {
  id: 1,
  email: "github@example.com",
  name: "GitHub User",
  is_workspace_admin: true,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AuthProvider mount /auth/me race", () => {
  let meDeferred: {
    promise: Promise<Response>;
    resolve: (response: Response) => void;
  };

  beforeEach(() => {
    localStorage.clear();
    meDeferred = (() => {
      let resolve!: (response: Response) => void;
      const promise = new Promise<Response>((res) => {
        resolve = res;
      });
      return { promise, resolve };
    })();

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return meDeferred.promise;
        }
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(
            jsonResponse({
              access_token: makeJwt(passwordUser.id, passwordUser.email),
              user: passwordUser,
            }),
          );
        }
        if (url.endsWith("/auth/logout")) {
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("does not overwrite a concurrent password login with a stale /auth/me response", async () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(true);
    });

    await act(async () => {
      await result.current.login("password@example.com", "supersecret1234");
    });

    expect(result.current.user).toEqual(passwordUser);
    expect(result.current.token).toBeTruthy();

    await act(async () => {
      meDeferred.resolve(jsonResponse(cookieUser));
    });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.user).toEqual(passwordUser);
  });

  it("does not overwrite a concurrent login even if the stale response's body resolves after the login completes", async () => {
    // Headers resolve before login() runs, but res.json() only resolves after —
    // this exercises the epoch re-check *after* the `await res.json()` in the
    // /auth/me handler, distinct from the epoch check at the top of the handler.
    let resolveJson!: () => void;
    const jsonPromise = new Promise<unknown>((resolve) => {
      resolveJson = () => resolve(cookieUser);
    });
    const staleResponse = {
      ok: true,
      status: 200,
      json: () => jsonPromise,
    } as Response;

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      meDeferred.resolve(staleResponse);
    });

    await act(async () => {
      await result.current.login("password@example.com", "supersecret1234");
    });

    expect(result.current.user).toEqual(passwordUser);

    await act(async () => {
      resolveJson();
    });

    // The stale /auth/me body must not clobber the concurrently-logged-in user.
    expect(result.current.user).toEqual(passwordUser);
  });

  it("ignores a stale /auth/me response after logout", async () => {
    localStorage.setItem(TOKEN_KEY, makeJwt(cookieUser.id, cookieUser.email));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      result.current.logout();
    });

    expect(result.current.user).toBeNull();

    await act(async () => {
      meDeferred.resolve(jsonResponse(cookieUser));
    });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.user).toBeNull();
  });
});

describe("AuthProvider logoutWarning", () => {
  function stubFetch(logoutResponse: () => Promise<Response>) {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(new Response(null, { status: 401 }));
        }
        if (url.endsWith("/auth/logout")) {
          return logoutResponse();
        }
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(
            jsonResponse({
              access_token: makeJwt(passwordUser.id, passwordUser.email),
              user: passwordUser,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
  }

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sets logoutWarning when the /auth/logout network call rejects", async () => {
    stubFetch(() => Promise.reject(new Error("network down")));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.logout();
    });

    await waitFor(() => {
      expect(result.current.logoutWarning).toBeTruthy();
    });
  });

  it("sets logoutWarning when /auth/logout responds non-ok", async () => {
    stubFetch(() => Promise.resolve(new Response(null, { status: 500 })));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.logout();
    });

    await waitFor(() => {
      expect(result.current.logoutWarning).toBeTruthy();
    });
  });

  it("does not set logoutWarning when /auth/logout succeeds", async () => {
    stubFetch(() => Promise.resolve(new Response(null, { status: 204 })));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.logout();
    });

    // Give the logout() fetch promise a tick to settle before asserting the negative.
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.logoutWarning).toBeNull();
  });

  it("clears logoutWarning via clearLogoutWarning", async () => {
    stubFetch(() => Promise.reject(new Error("network down")));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.logout();
    });

    await waitFor(() => {
      expect(result.current.logoutWarning).toBeTruthy();
    });

    act(() => {
      result.current.clearLogoutWarning();
    });

    expect(result.current.logoutWarning).toBeNull();
  });

  it("clears logoutWarning on the next successful login", async () => {
    stubFetch(() => Promise.reject(new Error("network down")));

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.logout();
    });

    await waitFor(() => {
      expect(result.current.logoutWarning).toBeTruthy();
    });

    await act(async () => {
      await result.current.login("password@example.com", "supersecret1234");
    });

    expect(result.current.logoutWarning).toBeNull();
  });
});

describe("AuthProvider authUnconfirmed", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("retries once after a transient /auth/me failure and clears authUnconfirmed on success", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(TOKEN_KEY, makeJwtForUser(cookieUser));

    let meCallCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          meCallCount += 1;
          if (meCallCount === 1) return Promise.reject(new Error("network blip"));
          return Promise.resolve(jsonResponse(cookieUser));
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    // Optimistic decode from the stored JWT applies immediately.
    expect(result.current.user).toEqual(cookieUser);
    expect(result.current.authUnconfirmed).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    await waitFor(() => expect(meCallCount).toBe(2));
    expect(result.current.authUnconfirmed).toBe(false);
    expect(result.current.user).toEqual(cookieUser);
  });

  it("marks authUnconfirmed after the retry also fails, without clearing the optimistic user", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(TOKEN_KEY, makeJwtForUser(cookieUser));

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.reject(new Error("network down"));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    expect(result.current.user).toEqual(cookieUser);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    await waitFor(() => expect(result.current.authUnconfirmed).toBe(true));
    // The optimistic user is kept (not wiped) — the caller decides what to do with the flag.
    expect(result.current.user).toEqual(cookieUser);
    expect(result.current.isLoading).toBe(false);
  });

  it("clears authUnconfirmed on the next 'online' event once connectivity returns", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(TOKEN_KEY, makeJwtForUser(cookieUser));

    let online = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return online ? Promise.resolve(jsonResponse(cookieUser)) : Promise.reject(new Error("offline"));
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await waitFor(() => expect(result.current.authUnconfirmed).toBe(true));

    online = true;
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });

    await waitFor(() => expect(result.current.authUnconfirmed).toBe(false));
    expect(result.current.user).toEqual(cookieUser);
  });

  it("does not set authUnconfirmed when there was no stored token to begin with", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.reject(new Error("network down"));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.authUnconfirmed).toBe(false);
    expect(result.current.user).toBeNull();
  });

  it("does not overwrite a concurrent login with a stale online-reconnect /auth/me response", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(TOKEN_KEY, makeJwtForUser(cookieUser));

    const passwordToken = makeJwt(passwordUser.id, passwordUser.email);

    let meCallCount = 0;
    const onlineDeferred = (() => {
      let resolve!: (response: Response) => void;
      const promise = new Promise<Response>((res) => {
        resolve = res;
      });
      return { promise, resolve };
    })();

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          meCallCount += 1;
          // Mount attempt (1) and its retry (2) both fail, so authUnconfirmed
          // becomes true; the online-triggered call (3) is held open so a
          // concurrent login can race it.
          if (meCallCount <= 2) return Promise.reject(new Error("network down"));
          return onlineDeferred.promise;
        }
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(
            jsonResponse({ access_token: passwordToken, user: passwordUser }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await waitFor(() => expect(result.current.authUnconfirmed).toBe(true));

    // Connectivity returns, firing the online handler's /auth/me check —
    // its response is held open via onlineDeferred.
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    await waitFor(() => expect(meCallCount).toBe(3));

    // A password login happens while that online-triggered fetch is still
    // in flight.
    await act(async () => {
      await result.current.login("password@example.com", "supersecret1234");
    });

    expect(result.current.user).toEqual(passwordUser);
    expect(result.current.token).toEqual(passwordToken);

    // The stale online-triggered /auth/me response resolves after the login.
    await act(async () => {
      onlineDeferred.resolve(jsonResponse(cookieUser));
    });

    // It must not clobber the newer, concurrently-logged-in session.
    expect(result.current.user).toEqual(passwordUser);
    expect(result.current.token).toEqual(passwordToken);
  });
});

describe("AuthProvider pendingInvitations", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const invite = { org_login: "acme", expires_at: "2030-01-01T00:00:00Z" };

  it("captures pending_invitations from a successful login response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(
            jsonResponse({ access_token: makeJwt(1, "member@e.com"), user: passwordUser, pending_invitations: [invite] }),
          );
        }
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("member@example.com", "supersecret1234");
    });

    expect(result.current.pendingInvitations).toEqual([invite]);
  });

  it("defaults to an empty list when the login response omits pending_invitations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(jsonResponse({ access_token: makeJwt(1, "member@e.com"), user: passwordUser }));
        }
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("member@example.com", "supersecret1234");
    });

    expect(result.current.pendingInvitations).toEqual([]);
  });

  it("captures pending invitations passed to setSession (register flow)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    act(() => {
      result.current.setSession(makeJwt(1, "member@e.com"), passwordUser, [invite]);
    });

    expect(result.current.pendingInvitations).toEqual([invite]);
  });

  it("clears pendingInvitations via dismissPendingInvitations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    act(() => {
      result.current.setSession(makeJwt(1, "member@e.com"), passwordUser, [invite]);
    });
    expect(result.current.pendingInvitations).toEqual([invite]);

    act(() => {
      result.current.dismissPendingInvitations();
    });
    expect(result.current.pendingInvitations).toEqual([]);
  });

  it("clears pendingInvitations on logout", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        if (url.endsWith("/auth/logout")) return Promise.resolve(new Response(null, { status: 204 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthProvider>{children}</AuthProvider>
    );
    const { result } = renderHook(() => useAuth(), { wrapper });

    act(() => {
      result.current.setSession(makeJwt(1, "member@e.com"), passwordUser, [invite]);
    });
    expect(result.current.pendingInvitations).toEqual([invite]);

    act(() => {
      result.current.logout();
    });
    expect(result.current.pendingInvitations).toEqual([]);
  });
});

describe("AuthProvider per-user browser state", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function seedPerUserState() {
    localStorage.setItem("active_scope", JSON.stringify({ kind: "org", login: "acme" }));
    localStorage.setItem("default_org", "acme");
    localStorage.setItem("activity_last_seen_at", "2024-01-01T00:00:00Z");
  }

  function expectPerUserStateCleared() {
    expect(localStorage.getItem("active_scope")).toBeNull();
    expect(localStorage.getItem("default_org")).toBeNull();
    expect(localStorage.getItem("activity_last_seen_at")).toBeNull();
  }

  const wrapper = ({ children }: { children: React.ReactNode }) => <AuthProvider>{children}</AuthProvider>;

  it("clears the previous user's scope and activity marker on setSession (register flow)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
    seedPerUserState();

    const { result } = renderHook(() => useAuth(), { wrapper });

    act(() => {
      result.current.setSession(makeJwt(9, "new-user@e.com"), { ...passwordUser, id: 9, email: "new-user@e.com" });
    });

    expectPerUserStateCleared();
  });

  it("clears the previous user's scope and activity marker on login", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/login")) {
          return Promise.resolve(jsonResponse({ access_token: makeJwt(9, "new-user@e.com"), user: passwordUser }));
        }
        if (url.endsWith("/auth/me")) return Promise.resolve(new Response(null, { status: 401 }));
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
    seedPerUserState();

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("new-user@example.com", "supersecret1234");
    });

    expectPerUserStateCleared();
  });
});

import type {
  ActionsUsageResponse,
  AnalyticsHistoryResponse,
  AnalyticsOverviewResponse,
  AuditLogOut,
  CacheClearResponse,
  CacheListResponse,
  CheckValue,
  CockpitResponse,
  CreateIssueResponse,
  DispatchResponse,
  FailedRunsResponse,
  GithubMembershipStatus,
  GithubOrgInvitationsResponse,
  GithubOrgMembersResponse,
  GithubOutsideCollaboratorsResponse,
  InactiveMembersResponse,
  InstallationLookup,
  InstallationMeta,
  InvitationCreateResponse,
  InvitationOut,
  InvitationPreview,
  JobOut,
  MyOrgMembership,
  MyIssueListResponse,
  MyPrListResponse,
  MyViewResponse,
  OrgEventsResponse,
  PendingInvitationSummary,
  PermissionAuditResponse,
  ReleaseTimelineResponse,
  RepoListResponse,
  RepoPullsResponse,
  RepoSecurityResponse,
  RepoStatsResponse,
  RunsResponse,
  SavedTokenMeta,
  ScanExportResponse,
  SecretScanningResponse,
  SecurityMatrixResponse,
  SyncInstallationsResponse,
  WorkflowsResponse,
} from "./types"

const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080"

// ── Token accessor ────────────────────────────────────────────────────────────
// The auth context writes the JWT to localStorage under this key.
// The client reads it on each request so it's always fresh after login.
const _TOKEN_KEY = "clevis:token"

function getAuthHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {}
  const token = localStorage.getItem(_TOKEN_KEY)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

// Hard ceiling on every request. Without this, a fetch to an unreachable/hanging
// API never settles, leaving callers (e.g. React Query) stuck in a loading state
// forever instead of surfacing an error.
const REQUEST_TIMEOUT_MS = 15000

async function fetchWithTimeout(url: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    // credentials:"include" sends the httpOnly session cookie (GitHub OAuth sessions) alongside
    // the Bearer header (email/password sessions); require_auth accepts either.
    return await fetch(url, { credentials: "include", ...init, signal: controller.signal })
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s — is the API reachable?`)
    }
    throw err
  } finally {
    clearTimeout(timer)
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  const json = await res.json().catch(() => null)
  if (res.status === 401) {
    // Clear stale token and let the auth guard redirect to /login
    if (typeof window !== "undefined") localStorage.removeItem(_TOKEN_KEY)
    window.dispatchEvent(new Event("clevis:unauthorized"))
  }
  if (!res.ok) throw new Error((json as { detail?: string } | null)?.detail ?? `Request failed: ${res.status}`)
  return json as T
}

async function post<T>(path: string, body: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const res = await fetchWithTimeout(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders(), ...extraHeaders },
    body: JSON.stringify(body),
  })
  return handleResponse<T>(res)
}

// Carries an optional client-supplied PAT to GET endpoints via a header (never
// a query string, which would leak into logs/browser history).
function githubTokenHeader(token?: string): Record<string, string> | undefined {
  return token ? { "X-GitHub-Token": token } : undefined
}

async function get<T>(path: string, extraHeaders?: Record<string, string>): Promise<T> {
  const res = await fetchWithTimeout(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...getAuthHeaders(), ...extraHeaders },
  })
  return handleResponse<T>(res)
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetchWithTimeout(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  })
  return handleResponse<T>(res)
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetchWithTimeout(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  })
  return handleResponse<T>(res)
}

async function del(path: string): Promise<void> {
  const res = await fetchWithTimeout(`${BASE}${path}`, {
    method: "DELETE",
    headers: { ...getAuthHeaders() },
  })
  if (res.status === 401) {
    if (typeof window !== "undefined") localStorage.removeItem(_TOKEN_KEY)
    window.dispatchEvent(new Event("clevis:unauthorized"))
  }
  if (!res.ok) {
    const json = await res.json().catch(() => ({}))
    throw new Error((json as { detail?: string }).detail ?? `Request failed: ${res.status}`)
  }
}

// ── Check value normalization ─────────────────────────────────────────────────

function normalizeCheckValue(id: string, raw: unknown): CheckValue {
  if (id === "organization_members_mfa_required") {
    return { type: "boolean", enabled: Boolean(raw) }
  }
  if (id === "repository_dependabot_alerts_clear" && typeof raw === "object" && raw !== null) {
    const r = raw as Record<string, unknown>
    return {
      type: "severity_counts",
      critical: Number(r.critical ?? 0),
      high: Number(r.high ?? 0),
      medium: Number(r.medium ?? 0),
      low: Number(r.low ?? 0),
    }
  }
  if (typeof raw === "object" && raw !== null) {
    const r = raw as Record<string, unknown>
    if ("checked" in r && "protected" in r) {
      return { type: "ratio", numerator: Number(r.protected), denominator: Number(r.checked) }
    }
    if ("enabled" in r && "total" in r) {
      return { type: "ratio", numerator: Number(r.enabled), denominator: Number(r.total) }
    }
    if ("open" in r && "repos_with_alerts" in r && "total_repos" in r) {
      const total = Number(r.total_repos)
      return { type: "ratio", numerator: total - Number(r.repos_with_alerts), denominator: total }
    }
    if ("repos_checked" in r && "force_push_allowed" in r) {
      const total = Number(r.repos_checked)
      return { type: "ratio", numerator: total - Number(r.force_push_allowed), denominator: total }
    }
  }
  return null
}

// ── API surface ───────────────────────────────────────────────────────────────

export const api = {
  analytics: {
    // token is optional — the API falls back to a connected GitHub App installation
    // token when one exists for this owner, so an empty field is fine to send.
    overview: async (owner: string, token: string): Promise<AnalyticsOverviewResponse> => {
      const data = await post<AnalyticsOverviewResponse>("/me/analytics/overview", { owner, token: token || undefined })
      return {
        ...data,
        checks: data.checks.map((c) => ({ ...c, value: normalizeCheckValue(c.id, c.value) })),
      }
    },
    history: (owner: string) =>
      get<AnalyticsHistoryResponse>(`/me/analytics/history?owner=${encodeURIComponent(owner)}`),
    // Issue #294: GitHub Actions minutes for the org's billing cycle. Org-scoped +
    // admin-only; needs an App permission Clevis doesn't request by default, so a
    // missing-permission 403 comes back as a 400 (the Overview card hides itself on error).
    actionsUsage: (org: string, token?: string) =>
      get<ActionsUsageResponse>(
        `/orgs/${encodeURIComponent(org)}/usage/actions`,
        githubTokenHeader(token),
      ),
    // Compliance export (issue #293): full scan history with per-check detail, for
    // an optional [since, until] day window. The caller renders CSV from this.
    exportHistory: (owner: string, since?: string, until?: string) => {
      const params = new URLSearchParams({ owner })
      if (since) params.set("since", since)
      if (until) params.set("until", until)
      return get<ScanExportResponse>(`/me/analytics/export?${params.toString()}`)
    },
    // token is optional — same App-or-PAT fallback as the rest of this namespace,
    // carried via header since this is a GET (see githubTokenHeader).
    cockpit: (owner: string, token?: string) =>
      get<CockpitResponse>(`/me/analytics/cockpit/${encodeURIComponent(owner)}`, githubTokenHeader(token)),
    myView: (owner: string, token?: string) =>
      get<MyViewResponse>(`/me/github/my-view?owner=${encodeURIComponent(owner)}`, githubTokenHeader(token)),
    myPrs: (owner: string, page = 1, perPage = 25, token?: string) =>
      get<MyPrListResponse>(
        `/me/github/my-prs?owner=${encodeURIComponent(owner)}&page=${page}&per_page=${perPage}`,
        githubTokenHeader(token),
      ),
    myReviews: (owner: string, page = 1, perPage = 25, token?: string) =>
      get<MyPrListResponse>(
        `/me/github/my-reviews?owner=${encodeURIComponent(owner)}&page=${page}&per_page=${perPage}`,
        githubTokenHeader(token),
      ),
    myIssues: (owner: string, page = 1, perPage = 25, token?: string) =>
      get<MyIssueListResponse>(
        `/me/github/my-issues?owner=${encodeURIComponent(owner)}&page=${page}&per_page=${perPage}`,
        githubTokenHeader(token),
      ),
  },
  security: {
    matrix: (owner: string, token?: string) =>
      get<SecurityMatrixResponse>(
        `/me/analytics/security-matrix/${encodeURIComponent(owner)}`,
        githubTokenHeader(token),
      ),
    secretScanning: (owner: string, repo: string, token?: string) =>
      get<SecretScanningResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/secret-scanning`,
        githubTokenHeader(token),
      ),
    // "Fix this" (issue #287): apply the automated fix for a failing check in
    // {owner}/{repo}. Needs the resolved token to carry the relevant write scope;
    // a 403 from GitHub comes back as a 400 with a permission hint. Admin-gated
    // when `owner` is a connected Clevis org.
    remediate: (owner: string, repo: string, checkId: string, token?: string) =>
      post<{ check_id: string; repo: string; remediated: boolean }>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/security/checks/${encodeURIComponent(checkId)}/remediate`,
        { token: token || undefined },
      ),
  },
  issues: {
    // Create a GitHub issue from a Clevis finding (issue #286). Needs the resolved
    // token (App installation or PAT) to carry `Issues: write`; a 403 from GitHub
    // surfaces here as a 400. Admin-gated when `owner` is a connected Clevis org.
    create: (owner: string, repo: string, body: { title: string; body: string }, token?: string) =>
      post<CreateIssueResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/issues`,
        { ...body, token: token || undefined },
      ),
  },
  cache: {
    list: (owner: string, repo: string, token: string) =>
      post<CacheListResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions-caches`,
        { token: token || undefined },
      ),
    clear: (
      owner: string,
      repo: string,
      body: { token: string; actor: string; dry_run: boolean; key?: string; ref?: string },
    ) =>
      post<CacheClearResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions-caches/clear`,
        { ...body, token: body.token || undefined },
      ),
  },
  repos: {
    list: (org: string, token: string) =>
      post<RepoListResponse>(`/orgs/${encodeURIComponent(org)}/repos`, { token: token || undefined }),
    stats: (org: string, owner: string, repo: string, token: string) =>
      post<RepoStatsResponse>(
        `/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/stats`,
        { token: token || undefined },
      ),
    pulls: (org: string, owner: string, repo: string, token: string) =>
      post<RepoPullsResponse>(
        `/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/pulls`,
        { token: token || undefined },
      ),
    security: (org: string, owner: string, repo: string, token: string) =>
      post<RepoSecurityResponse>(
        `/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/security`,
        { token: token || undefined },
      ),
  },
  jobs: {
    list: () => get<JobOut[]>("/jobs"),
  },
  automation: {
    // token is optional — same App-or-PAT fallback as the rest of the API, carried
    // via header since these are GETs (see githubTokenHeader).
    workflows: (owner: string, repo: string, token?: string) =>
      get<WorkflowsResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/workflows`,
        githubTokenHeader(token),
      ),
    runs: (owner: string, repo: string, token?: string, perPage = 10) =>
      get<RunsResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/runs?per_page=${perPage}`,
        githubTokenHeader(token),
      ),
    dispatch: (
      owner: string,
      repo: string,
      workflowId: number,
      body: { token: string; ref: string; inputs?: Record<string, string> },
    ) =>
      post<DispatchResponse>(
        `/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/workflows/${workflowId}/dispatch`,
        { ...body, token: body.token || undefined },
      ),
  },
  github: {
    events: (org: string, token: string, perPage = 30) =>
      post<OrgEventsResponse>(`/github/orgs/${encodeURIComponent(org)}/events`, {
        token: token || undefined,
        per_page: perPage,
      }),
    failedRuns: (org: string, token: string, limit = 20) =>
      post<FailedRunsResponse>(`/github/orgs/${encodeURIComponent(org)}/failed-runs`, {
        token: token || undefined,
        limit,
      }),
    releaseTimeline: (org: string, token: string, days = 90) =>
      post<ReleaseTimelineResponse>(`/github/orgs/${encodeURIComponent(org)}/release-timeline`, {
        token: token || undefined,
        days,
      }),
  },
  audit: {
    list: (action?: string) =>
      get<AuditLogOut[]>(`/audit${action ? `?action=${encodeURIComponent(action)}` : ""}`),
  },
  installations: {
    list: () => get<InstallationMeta[]>("/me/installations"),
    // Org-connected installations, not personal ones -- list() above only ever returns
    // the caller's personal (User-type) installations (GET /me/installations), never an
    // org's. Requires the caller to already be a recognized Clevis org member (404 if the
    // org isn't connected yet, 403 if not a member) -- callers should treat either as
    // "not installed" rather than surfacing the error.
    listForOrg: (orgLogin: string) => get<InstallationMeta[]>(`/orgs/${encodeURIComponent(orgLogin)}/installations`),
    lookup: (installationId: number) =>
      get<InstallationLookup>(`/me/installations/lookup/${installationId}`),
    sync: (
      target: { scope: "me" } | { scope: "org"; orgLogin: string },
      body: { account_login: string; account_type: string; installation_id: number },
    ) =>
      target.scope === "me"
        ? post<SyncInstallationsResponse>("/me/installations/sync", body)
        : post<SyncInstallationsResponse>(
            `/orgs/${encodeURIComponent(target.orgLogin)}/installations/sync`,
            body,
          ),
    // Disconnect: uninstalls the App on GitHub's side (a real revocation), then removes
    // the local row -- see apps/api/src/routers/installations.py's module docstring.
    remove: (
      target: { scope: "me" } | { scope: "org"; orgLogin: string },
      installationId: number,
    ) =>
      target.scope === "me"
        ? del(`/me/installations/${installationId}`)
        : del(`/orgs/${encodeURIComponent(target.orgLogin)}/installations/${installationId}`),
  },
  orgs: {
    mine: () => get<MyOrgMembership[]>("/me/orgs"),
  },
  invitations: {
    create: (orgLogin: string, email: string) =>
      post<InvitationCreateResponse>(`/orgs/${encodeURIComponent(orgLogin)}/invitations`, { email }),
    list: (orgLogin: string) => get<InvitationOut[]>(`/orgs/${encodeURIComponent(orgLogin)}/invitations`),
    revoke: (orgLogin: string, invitationId: number) =>
      post<InvitationOut>(`/orgs/${encodeURIComponent(orgLogin)}/invitations/${invitationId}/revoke`, {}),
    preview: (token: string) => get<InvitationPreview>(`/invitations/${encodeURIComponent(token)}`),
    accept: (token: string) => post<{ org_login: string; role: string }>(`/invitations/${encodeURIComponent(token)}/accept`, {}),
  },
  collab: {
    // token is optional — the API falls back to a connected GitHub App installation
    // token when one exists for this org; only orgs without one need it supplied.
    members: (orgLogin: string, role: "all" | "member" | "admin" = "all", token?: string) =>
      get<GithubOrgMembersResponse>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/members?role=${role}`,
        githubTokenHeader(token),
      ),
    outsideCollaborators: (orgLogin: string, token?: string) =>
      get<GithubOutsideCollaboratorsResponse>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/outside_collaborators`,
        githubTokenHeader(token),
      ),
    invitations: (orgLogin: string, token?: string) =>
      get<GithubOrgInvitationsResponse>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/invitations`,
        githubTokenHeader(token),
      ),
    membership: (orgLogin: string, username: string, token?: string) =>
      get<GithubMembershipStatus>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/members/${encodeURIComponent(username)}/membership`,
        githubTokenHeader(token),
      ),
    permissionAudit: (orgLogin: string, token?: string) =>
      get<PermissionAuditResponse>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/permission-audit`,
        githubTokenHeader(token),
      ),
    inactiveMembers: (orgLogin: string, days = 30, token?: string) =>
      get<InactiveMembersResponse>(
        `/github/orgs/${encodeURIComponent(orgLogin)}/inactive-members?days=${days}`,
        githubTokenHeader(token),
      ),
  },
  tokens: {
    list: () => get<SavedTokenMeta[]>("/tokens"),
    upsert: (org: string, token: string, label?: string) =>
      put<SavedTokenMeta>(`/tokens/${encodeURIComponent(org)}`, { token, label }),
    resolve: (org: string) =>
      post<{ token: string }>("/tokens/resolve", { org }),
    delete: (org: string) => del(`/tokens/${encodeURIComponent(org)}`),
  },
  config: {
    getAll: () => get<Record<string, string>>("/config"),
    update: (key: string, value: string) =>
      put<Record<string, string>>(`/config/${encodeURIComponent(key)}`, { value }),
  },
  auth: {
    setupRequired: () => get<{ setup_required: boolean }>("/auth/setup-required"),
    setup: (email: string, password: string, name?: string) =>
      post<{ access_token: string; user: { id: number; email: string; name: string | null; is_workspace_admin: boolean } }>(
        "/auth/setup",
        { email, password, name },
      ),
    register: (email: string, password: string, name?: string) =>
      post<{
        access_token: string
        user: { id: number; email: string; name: string | null; is_workspace_admin: boolean }
        pending_invitations: PendingInvitationSummary[]
      }>("/auth/register", { email, password, name }),
    patchMe: (name: string) =>
      patch<{ id: number; email: string; name: string | null; is_workspace_admin: boolean }>("/auth/me", { name }),
    revokeSessions: () => post<{ ok: boolean }>("/auth/me/revoke-sessions", {}),
    verifyEmail: (token: string) => post<{ ok: boolean }>("/auth/verify-email", { token }),
    resendVerification: () =>
      post<{ ok: boolean; already_verified: boolean }>("/auth/resend-verification", {}),
  },
}

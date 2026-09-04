import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";

const TOKEN_KEY = "clevis:token";

describe("del() 401 handling", () => {
  beforeEach(() => {
    localStorage.setItem(TOKEN_KEY, "stale-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("clears the stored token and dispatches clevis:unauthorized on a 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 401 }))),
    );

    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    await expect(api.tokens.delete("acme")).rejects.toThrow();

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(dispatchSpy).toHaveBeenCalledWith(expect.objectContaining({ type: "clevis:unauthorized" }));
  });

  it("does not clear the token or dispatch clevis:unauthorized on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))),
    );

    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    await api.tokens.delete("acme");

    expect(localStorage.getItem(TOKEN_KEY)).toBe("stale-token");
    expect(dispatchSpy).not.toHaveBeenCalledWith(expect.objectContaining({ type: "clevis:unauthorized" }));
  });
});

describe("optional token coercion (GitHub App installation fallback)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("sends token: undefined for analytics.overview when the token field is empty", async () => {
    stubOkJson({ owner: "acme", score: 100, total_checks: 0, failed_checks: 0, repo_count: 0, checks: [] });
    await api.analytics.overview("acme", "");
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ owner: "acme", token: undefined });
  });

  it("sends token: undefined for cache.list when the token field is empty", async () => {
    stubOkJson({ repository: "acme/demo", total: 0, actions_caches: [] });
    await api.cache.list("acme", "demo", "");
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined });
  });

  it("sends token: undefined for cache.clear when the token field is empty", async () => {
    stubOkJson({ queued: false, dry_run: true });
    await api.cache.clear("acme", "demo", { token: "", actor: "me", dry_run: true });
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined, actor: "me", dry_run: true });
  });

  it("POSTs branch-protection/bulk with the org in the path and drops an empty token", async () => {
    stubOkJson({ dry_run: true, diffs: [] });
    await api.branchProtection.bulk("acme", { repos: ["api"], dry_run: true, token: "" });
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/orgs/acme/branch-protection/bulk");
    expect(JSON.parse(init.body as string)).toEqual({ repos: ["api"], dry_run: true, token: undefined });
  });

  it("POSTs workflow-lint under the personal route and drops an empty token", async () => {
    stubOkJson({ findings: [], fixable: false, pr_url: null });
    await api.workflowLint.scan("acme", "api", { open_pr: true }, "");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/me/repos/acme/api/workflow-lint");
    expect(JSON.parse(init.body as string)).toEqual({ open_pr: true, token: undefined });
  });

  it("GET/PUTs the dependabot-triage setting and POSTs the run under the org path", async () => {
    stubOkJson({ enabled: false, mode: "approve_only", merge_method: "squash" });
    await api.dependabotTriage.getRepo("acme", "acme", "api");
    const [getUrl, getInit] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(getUrl).toContain("/orgs/acme/repos/acme/api/automation/dependabot-triage");
    expect(getInit.method).toBeUndefined();

    stubOkJson({ enabled: true, mode: "approve_only", merge_method: "squash" });
    await api.dependabotTriage.setRepo("acme", "acme", "api", { enabled: true, mode: "approve_only" });
    const [settingUrl, settingInit] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(settingUrl).toContain("/orgs/acme/repos/acme/api/automation/dependabot-triage");
    expect(settingInit.method).toBe("PUT");

    stubOkJson({ decisions: [] });
    await api.dependabotTriage.run("acme", { repos: ["acme/api"], dry_run: true }, "");
    const [runUrl, runInit] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(runUrl).toContain("/orgs/acme/dependabot-triage");
    expect(JSON.parse(runInit.body as string)).toEqual({ repos: ["acme/api"], dry_run: true, token: undefined });
  });

  it("builds the analytics.exportHistory URL with only owner when no window is given", async () => {
    stubOkJson({ truncated: false, row_count: 0, entries: [] });
    await api.analytics.exportHistory("acme corp");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/me/analytics/export?owner=acme+corp");
    expect(url).not.toContain("since=");
    expect(url).not.toContain("until=");
  });

  it("adds since/until to the analytics.exportHistory URL when provided", async () => {
    stubOkJson({ truncated: false, row_count: 0, entries: [] });
    await api.analytics.exportHistory("acme", "2026-01-01", "2026-03-31");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("owner=acme");
    expect(url).toContain("since=2026-01-01");
    expect(url).toContain("until=2026-03-31");
  });
});

describe("api.analytics value normalization", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("normalizes the Dependabot check's raw counts into a severity_counts value", async () => {
    stubOkJson({
      owner: "acme",
      score: 50,
      total_checks: 1,
      failed_checks: 1,
      repo_count: 1,
      checks: [
        {
          id: "repository_dependabot_alerts_clear",
          title: "No open critical/high Dependabot alerts",
          severity: "high",
          remediation: "n/a",
          status: "fail",
          value: { critical: 2, high: 1, medium: 0, low: 3 },
        },
      ],
    });
    const result = await api.analytics.overview("acme", "ghp_test");
    expect(result.checks[0].value).toEqual({ type: "severity_counts", critical: 2, high: 1, medium: 0, low: 3 });
  });

  it("normalizes the code-scanning check's raw shape into a ratio value", async () => {
    stubOkJson({
      owner: "acme",
      score: 50,
      total_checks: 1,
      failed_checks: 1,
      repo_count: 4,
      checks: [
        {
          id: "repository_code_scanning_alerts_clear",
          title: "No open code scanning alerts",
          severity: "medium",
          remediation: "n/a",
          status: "fail",
          value: { open: 3, repos_with_alerts: 1, total_repos: 4 },
        },
      ],
    });
    const result = await api.analytics.overview("acme", "ghp_test");
    expect(result.checks[0].value).toEqual({ type: "ratio", numerator: 3, denominator: 4 });
  });

  it("normalizes the force-push check's raw shape into a ratio value", async () => {
    stubOkJson({
      owner: "acme",
      score: 50,
      total_checks: 1,
      failed_checks: 1,
      repo_count: 2,
      checks: [
        {
          id: "repository_default_branch_no_force_push",
          title: "Default branch disallows force pushes",
          severity: "high",
          remediation: "n/a",
          status: "fail",
          value: { repos_checked: 2, force_push_allowed: 1 },
        },
      ],
    });
    const result = await api.analytics.overview("acme", "ghp_test");
    expect(result.checks[0].value).toEqual({ type: "ratio", numerator: 1, denominator: 2 });
  });

  it("GETs /me/analytics/cockpit/{owner} with no body and no token header when omitted", async () => {
    stubOkJson({
      repo_count: 1, member_count: 2, latest_score: 90, score_trend: [90],
      recent_events: [], open_pr_count: 0, pr_merge_rate_4w: [], commit_activity_4w: [],
      total_cache_size_bytes: 0, cache_job_success_rate: 0,
    });
    await api.analytics.cockpit("acme");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/analytics/cockpit/acme");
    expect(init.method).toBeUndefined();
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBeUndefined();
  });

  it("sends the token as an X-GitHub-Token header when supplied", async () => {
    stubOkJson({
      repo_count: 1, member_count: 2, latest_score: 90, score_trend: [90],
      recent_events: [], open_pr_count: 0, pr_merge_rate_4w: [], commit_activity_4w: [],
      total_cache_size_bytes: 0, cache_job_success_rate: 0,
    });
    await api.analytics.cockpit("acme", "ghp_test");
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
  });

  it("GETs /me/github/my-view?owner=... with an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ my_open_prs: [], review_requests: [], assigned_issues: [], my_recent_runs: [] });
    const result = await api.analytics.myView("acme", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/github/my-view?owner=acme");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ my_open_prs: [], review_requests: [], assigned_issues: [], my_recent_runs: [] });
  });

  it("GETs /me/github/my-prs?owner=...&page=...&per_page=... with an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ items: [], total_count: 0, page: 2, per_page: 10 });
    const result = await api.analytics.myPrs("acme", 2, 10, "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/github/my-prs?owner=acme&page=2&per_page=10");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ items: [], total_count: 0, page: 2, per_page: 10 });
  });

  it("GETs /me/github/my-reviews?owner=...&page=...&per_page=... with an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ items: [], total_count: 0, page: 1, per_page: 25 });
    const result = await api.analytics.myReviews("acme", 1, 25, "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/github/my-reviews?owner=acme&page=1&per_page=25");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ items: [], total_count: 0, page: 1, per_page: 25 });
  });

  it("GETs /me/github/my-issues?owner=...&page=...&per_page=... with an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ items: [], total_count: 0, page: 1, per_page: 25 });
    const result = await api.analytics.myIssues("acme", 1, 25, "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/github/my-issues?owner=acme&page=1&per_page=25");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ items: [], total_count: 0, page: 1, per_page: 25 });
  });

  it("GETs /me/analytics/history?owner=... and returns the raw scan history", async () => {
    stubOkJson([{ id: 1, owner: "acme", score: 80, total_checks: 3, failed_checks: 0, created_at: "2026-07-17T00:00:00Z" }]);
    const result = await api.analytics.history("acme");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/analytics/history?owner=acme");
    expect(result).toEqual([
      { id: 1, owner: "acme", score: 80, total_checks: 3, failed_checks: 0, created_at: "2026-07-17T00:00:00Z" },
    ]);
  });
});

describe("api.security", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("GETs /me/analytics/security-matrix/{owner} with an X-GitHub-Token header when supplied", async () => {
    const body = { owner: "acme", repos: [], summary: { fully_compliant_count: 0, critical_risk_count: 0, secret_hits_count: 0, vuln_by_severity: { critical: 0, high: 0, medium: 0, low: 0 } } };
    stubOkJson(body);
    const result = await api.security.matrix("acme", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/analytics/security-matrix/acme");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual(body);
  });

  it("GETs /me/repos/{owner}/{repo}/secret-scanning with no token header when omitted", async () => {
    const body = { repository: "acme/demo", alerts: [] };
    stubOkJson(body);
    const result = await api.security.secretScanning("acme", "demo");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/repos/acme/demo/secret-scanning");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBeUndefined();
    expect(result).toEqual(body);
  });

  it("POSTs /me/repos/{owner}/{repo}/issues with the title/body and token (#286)", async () => {
    stubOkJson({ number: 3, html_url: "https://github.com/acme/.github/issues/3" });
    const result = await api.issues.create(
      "acme",
      ".github",
      { title: "MFA off", body: "turn it on" },
      "ghp_admin",
    );
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/repos/acme/.github/issues");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      title: "MFA off",
      body: "turn it on",
      token: "ghp_admin",
    });
    expect(result).toEqual({ number: 3, html_url: "https://github.com/acme/.github/issues/3" });
  });

  it("omits the token from the issues.create body when none is supplied", async () => {
    stubOkJson({ number: 1, html_url: "u" });
    await api.issues.create("acme", ".github", { title: "x", body: "" });
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body as string).token).toBeUndefined();
  });
});

describe("api.analytics.actionsUsage (#294)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("GETs /orgs/{org}/usage/actions with an X-GitHub-Token header when supplied", async () => {
    const body = {
      total_minutes_used: 10,
      included_minutes_used: 10,
      paid_minutes_used: 0,
      minutes_used_breakdown: {},
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
    const result = await api.analytics.actionsUsage("acme", "ghp_admin");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/usage/actions");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_admin");
    expect(result).toEqual(body);
  });
});

describe("api.repos", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("POSTs to /orgs/{org}/repos with token: undefined when the token field is empty", async () => {
    stubOkJson({ org: "acme", total: 0, repos: [] });
    const result = await api.repos.list("acme", "");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/repos");
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined });
    expect(result).toEqual({ org: "acme", total: 0, repos: [] });
  });

  it("POSTs to /orgs/{org}/repos/{owner}/{repo}/stats", async () => {
    stubOkJson({ repository: "acme/demo", commit_activity: [], participation: {}, contributors: [] });
    await api.repos.stats("acme", "acme", "demo", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/repos/acme/demo/stats");
    expect(JSON.parse(init.body as string)).toEqual({ token: "ghp_test" });
  });

  it("POSTs to /orgs/{org}/repos/{owner}/{repo}/pulls", async () => {
    stubOkJson({ repository: "acme/demo", total: 0, pulls: [] });
    await api.repos.pulls("acme", "acme", "demo", "");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/repos/acme/demo/pulls");
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined });
  });
});

describe("api.collab", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("GETs /github/orgs/{org}/members with the role query param and an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ org: "acme", members: [], two_factor_overlay_available: true });
    await api.collab.members("acme", "admin", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/members?role=admin");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
  });

  it("GETs /github/orgs/{org}/outside_collaborators with no token header when omitted", async () => {
    stubOkJson({ org: "acme", collaborators: [], repos_scanned: 0, repos_total: 0 });
    await api.collab.outsideCollaborators("acme");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/outside_collaborators");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBeUndefined();
  });

  it("GETs /github/orgs/{org}/invitations", async () => {
    stubOkJson({ org: "acme", invitations: [] });
    const result = await api.collab.invitations("acme", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/invitations");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ org: "acme", invitations: [] });
  });

  it("GETs /github/orgs/{org}/members/{username}/membership", async () => {
    stubOkJson({ state: "active", role: "member" });
    const result = await api.collab.membership("acme", "alice", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/members/alice/membership");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ state: "active", role: "member" });
  });

  it("GETs /github/orgs/{org}/permission-audit with an X-GitHub-Token header when supplied", async () => {
    const body = {
      generated_at: "2026-07-20T00:00:00Z",
      repos_scanned: 1,
      repos_total: 1,
      repos: [],
      risk_summary: { outside_with_write_or_admin: 0, members_with_admin: 0, total_outside_collaborators: 0 },
    };
    stubOkJson(body);
    const result = await api.collab.permissionAudit("acme", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/permission-audit");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual(body);
  });

  it("GETs /github/orgs/{org}/inactive-members with a default days window and no token header when omitted", async () => {
    stubOkJson({ org: "acme", inactive_members: [], sampled_repos: [] });
    await api.collab.inactiveMembers("acme");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/inactive-members?days=30");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBeUndefined();
  });

  it("GETs /github/orgs/{org}/inactive-members with a custom days window", async () => {
    stubOkJson({ org: "acme", inactive_members: [], sampled_repos: [] });
    await api.collab.inactiveMembers("acme", 60, "ghp_test");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("days=60");
  });
});

describe("api.automation", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("GETs /me/repos/{owner}/{repo}/workflows with an X-GitHub-Token header when supplied", async () => {
    stubOkJson({ repository: "acme/demo", workflows: [] });
    const result = await api.automation.workflows("acme", "demo", "ghp_test");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/repos/acme/demo/workflows");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBe("ghp_test");
    expect(result).toEqual({ repository: "acme/demo", workflows: [] });
  });

  it("GETs /me/repos/{owner}/{repo}/actions/runs with a default per_page of 10 and no token header when omitted", async () => {
    stubOkJson({ repository: "acme/demo", runs: [] });
    const result = await api.automation.runs("acme", "demo");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/repos/acme/demo/actions/runs?per_page=10");
    expect((init.headers as Record<string, string>)["X-GitHub-Token"]).toBeUndefined();
    expect(result).toEqual({ repository: "acme/demo", runs: [] });
  });

  it("GETs /me/repos/{owner}/{repo}/actions/runs with a custom per_page", async () => {
    stubOkJson({ repository: "acme/demo", runs: [] });
    await api.automation.runs("acme", "demo", "ghp_test", 25);
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("per_page=25");
  });

  it("POSTs to /me/repos/{owner}/{repo}/workflows/{id}/dispatch with token: undefined when empty", async () => {
    stubOkJson({ dispatched: true, message: "Workflow dispatched." });
    const result = await api.automation.dispatch("acme", "demo", 1, { token: "", ref: "main" });
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/repos/acme/demo/workflows/1/dispatch");
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined, ref: "main" });
    expect(result).toEqual({ dispatched: true, message: "Workflow dispatched." });
  });
});

describe("api.security.remediate (issue #287)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("POSTs to the check remediation path with token: undefined when empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ check_id: "x", repo: "api", remediated: true }), { status: 200 }))),
    );
    await api.security.remediate("acme corp", "api", "repository_secret_scanning_enabled", "");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain(
      "/me/repos/acme%20corp/api/security/checks/repository_secret_scanning_enabled/remediate",
    );
    expect((init as RequestInit).method).toBe("POST");
    // JSON.stringify drops the undefined token, so the wire body is an empty object.
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  it("forwards a supplied token in the body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ check_id: "x", repo: "api", remediated: true }), { status: 200 }))),
    );
    await api.security.remediate("acme", "api", "repository_dependabot_alerts_clear", "ghp_x");
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ token: "ghp_x" });
  });
});

describe("api.prNudges.sweep (issue #289)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("POSTs the org-scoped nudge path and forwards a supplied token", async () => {
    const body = { mode: "comment", stale_days: 3, results: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
    const result = await api.prNudges.sweep("acme", "acme", "api", "ghp_admin");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/repos/acme/api/pr-nudges");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ token: "ghp_admin" });
    expect(result).toEqual(body);
  });

  it("omits the token when none is supplied", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ mode: "off", stale_days: 3, results: [] }), { status: 200 }))),
    );
    await api.prNudges.sweep("acme", "acme", "api");
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });
});

describe("api.github", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("POSTs to /github/orgs/{org}/failed-runs with token: undefined and a default limit", async () => {
    stubOkJson({ org: "acme", failed_runs: [] });
    const result = await api.github.failedRuns("acme", "");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/failed-runs");
    expect(JSON.parse(init.body as string)).toEqual({ token: undefined, limit: 20 });
    expect(result).toEqual({ org: "acme", failed_runs: [] });
  });

  it("POSTs to /github/orgs/{org}/release-timeline with a custom days window", async () => {
    stubOkJson({ org: "acme", releases: [] });
    await api.github.releaseTimeline("acme", "ghp_test", 30);
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/github/orgs/acme/release-timeline");
    expect(JSON.parse(init.body as string)).toEqual({ token: "ghp_test", days: 30 });
  });
});

describe("installations.lookup / installations.sync", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("GETs /me/installations/lookup/{id}", async () => {
    stubOkJson({ account_login: "shabnam", account_type: "User" });
    const result = await api.installations.lookup(42);
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/installations/lookup/42");
    expect(result).toEqual({ account_login: "shabnam", account_type: "User" });
  });

  it("POSTs to /me/installations/sync for scope: me", async () => {
    stubOkJson({ synced: true, token_ref: "tok_x" });
    await api.installations.sync(
      { scope: "me" },
      { account_login: "shabnam", account_type: "User", installation_id: 42 },
    );
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/installations/sync");
    expect(JSON.parse(init.body as string)).toEqual({
      account_login: "shabnam",
      account_type: "User",
      installation_id: 42,
    });
  });

  it("POSTs to /orgs/{orgLogin}/installations/sync for scope: org", async () => {
    stubOkJson({ synced: true, token_ref: "tok_y" });
    await api.installations.sync(
      { scope: "org", orgLogin: "acme" },
      { account_login: "acme", account_type: "Organization", installation_id: 7 },
    );
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/installations/sync");
  });

  it("DELETEs /me/installations/{id} for scope: me", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))));
    await api.installations.remove({ scope: "me" }, 7);
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/me/installations/7");
    expect(init.method).toBe("DELETE");
  });

  it("DELETEs /orgs/{orgLogin}/installations/{id} for scope: org", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))));
    await api.installations.remove({ scope: "org", orgLogin: "acme" }, 42);
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/orgs/acme/installations/42");
    expect(init.method).toBe("DELETE");
  });
});

describe("api.auth email verification (issue #217)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubOkJson(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    );
  }

  it("POSTs the token to /auth/verify-email", async () => {
    stubOkJson({ ok: true });
    const result = await api.auth.verifyEmail("a-token");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/auth/verify-email");
    expect(JSON.parse(init.body as string)).toEqual({ token: "a-token" });
    expect(result).toEqual({ ok: true });
  });

  it("POSTs with no body to /auth/resend-verification", async () => {
    stubOkJson({ ok: true, already_verified: false });
    const result = await api.auth.resendVerification();
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/auth/resend-verification");
    expect(JSON.parse(init.body as string)).toEqual({});
    expect(result).toEqual({ ok: true, already_verified: false });
  });
});

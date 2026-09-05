export interface MFACheckValue {
  type: "boolean"
  enabled: boolean
}

export interface RatioCheckValue {
  type: "ratio"
  numerator: number
  denominator: number
}

export interface SeverityCountsValue {
  type: "severity_counts"
  critical: number
  high: number
  medium: number
  low: number
}

export type CheckValue = MFACheckValue | RatioCheckValue | SeverityCountsValue | null

export interface CheckResult {
  id: string
  title: string
  severity: "high" | "medium" | "low"
  remediation: string
  status: "pass" | "fail" | "error" | "not_applicable"
  value: CheckValue
}

// Issue #289: POST /orgs/{org}/repos/{owner}/{repo}/pr-nudges
export interface PrNudgeResult {
  number: number
  title: string
  action: string
}

export interface PrNudgeResponse {
  mode: string
  stale_days: number
  results: PrNudgeResult[]
}

// Issue #294: GET /orgs/{org}/usage/actions (GitHub Actions minutes this billing month,
// from GitHub's enhanced-billing usage summary API). `included_minutes_used` is the
// slice covered by the plan's allowance; `paid_minutes_used` is what was billed on top.
export interface ActionsUsageResponse {
  total_minutes_used: number
  included_minutes_used: number
  paid_minutes_used: number
  minutes_used_breakdown: Record<string, number>
}

export interface AnalyticsOverviewResponse {
  owner: string
  score: number
  total_checks: number
  failed_checks: number
  repo_count: number
  checks: CheckResult[]
}

export interface ScanHistoryEntry {
  id: number
  owner: string
  score: number
  total_checks: number
  failed_checks: number
  created_at: string
}

export type AnalyticsHistoryResponse = ScanHistoryEntry[]

export interface ScanExportCheck {
  id: string
  title: string
  severity: string
  status: "pass" | "fail" | "error" | "not_applicable"
}

export interface ScanExportEntry extends ScanHistoryEntry {
  // Permissive on purpose: replays historical audit data that older runner
  // revisions may have stored with a slightly different shape.
  checks: Partial<ScanExportCheck>[]
}

export interface ScanExportResponse {
  truncated: boolean
  row_count: number
  entries: ScanExportEntry[]
}

export interface CacheEntry {
  id: number
  ref: string
  key: string
  version: string
  size_in_bytes: number
  created_at: string
  last_accessed_at: string
}

export interface CacheListResponse {
  repository: string
  total: number
  actions_caches: CacheEntry[]
}

export interface CacheClearResponse {
  queued: boolean
  dry_run?: boolean | null
  job_id?: number | null
  message?: string | null
}

export interface RepoSummary {
  name: string
  full_name: string
  private: boolean
  description: string | null
  language: string | null
  stargazers_count: number
  forks_count: number
  watchers_count: number
  open_issues_count: number
  pushed_at: string | null
  default_branch: string
  html_url: string
}

export interface RepoListResponse {
  org: string
  total: number
  repos: RepoSummary[]
}

export interface CommitActivityWeek {
  week: number
  total: number
  days: number[]
}

export interface LatestRelease {
  tag_name: string
  published_at: string | null
  html_url: string
}

export interface RepoStatsResponse {
  repository: string
  commit_activity: CommitActivityWeek[]
  // "aggregate" when commit_activity is estimated from stored push-event counts
  // (S6) instead of GitHub's own commit-level stats/commit_activity endpoint.
  commit_activity_source: "github" | "aggregate"
  participation: { all?: number[]; owner?: number[] }
  contributors: { author?: { login?: string }; total: number }[]
  stargazers_count: number
  forks_count: number
  watchers_count: number
  open_issues_count: number
  default_branch: string
  latest_release: LatestRelease | null
}

export interface PullSummary {
  number: number
  title: string
  user: string | null
  created_at: string
  html_url: string
}

export interface RepoPullsResponse {
  repository: string
  total: number
  pulls: PullSummary[]
}

export interface RepoSecurityResponse {
  repository: string
  branch_protection: "protected" | "unprotected" | "unknown"
  secret_scanning: "enabled" | "disabled" | "unknown"
}

export interface OrgEvent {
  id: string
  type: string
  actor: string
  actor_avatar: string
  repo: string
  summary: string
  created_at: string
}

export interface OrgEventsResponse {
  org: string
  events: OrgEvent[]
}

export interface FailedRunSummary {
  repo: string
  workflow_name: string
  branch: string
  run_id: number
  started_at: string
  duration_seconds: number | null
  url: string
  actor: string
  consecutive_failures: number
}

export interface FailedRunsResponse {
  org: string
  runs: FailedRunSummary[]
}

export interface ReleaseSummary {
  repo: string
  tag_name: string
  name: string
  published_at: string
  is_prerelease: boolean
  body_preview: string
  url: string
}

export interface ReleaseTimelineResponse {
  org: string
  releases: ReleaseSummary[]
}

export interface JobOut {
  id: number
  job_type: string
  status: "queued" | "processing" | "done" | "failed"
  result: string | null
  created_at: string
  updated_at: string
}

export interface AuditLogOut {
  id: number
  actor: string
  action: string
  target: string
  payload: string
  created_at: string
}

export interface SavedTokenMeta {
  org: string
  label: string | null
  created_at: string
  updated_at: string
}

export interface BlockedFeature {
  feature: string
  label: string
  missing: Record<string, string>
}

export interface InstallationMeta {
  id: number
  account_login: string
  account_type: string
  installation_id: number | null
  created_at: string
  // Permission-drift fields. `permissions_synced_at` is null for installs whose
  // permissions have never been observed (pre-tracking, or before the first
  // permission-accept webhook / a reconnect) — `blocked_features` is empty then too.
  // Optional so older callers/fixtures that don't set them still typecheck; the API
  // always includes them.
  permissions_synced_at?: string | null
  blocked_features?: BlockedFeature[]
}

export interface InstallationLookup {
  account_login: string
  account_type: string
}

export interface SyncInstallationsResponse {
  synced: boolean
  token_ref: string
}

export interface MyOrgMembership {
  org_login: string
  role: "admin" | "member"
}

export interface PendingInvitationSummary {
  // No token here by design — see PendingInvitationSummary in apps/api/src/routers/auth.py.
  org_login: string
  expires_at: string
}

export interface InvitationOut {
  id: number
  org_id: number
  email: string
  status: "pending" | "accepted" | "revoked"
  created_at: string
  accepted_at: string | null
}

export interface InvitationCreateResponse {
  invitation: InvitationOut
  invite_link: string
}

export interface InvitationPreview {
  org_login: string
  status: "pending" | "accepted" | "revoked"
}

export interface GithubOrgMember {
  login: string
  avatar_url: string
  role: "member" | "admin"
  site_admin: boolean
  two_factor_enabled: boolean | null
}

export interface GithubOrgMembersResponse {
  org: string
  members: GithubOrgMember[]
  two_factor_overlay_available: boolean
}

export interface GithubOutsideCollaborator {
  login: string
  avatar_url: string
  repos: string[]
}

export interface GithubOutsideCollaboratorsResponse {
  org: string
  collaborators: GithubOutsideCollaborator[]
  repos_scanned: number
  repos_total: number
}

export interface GithubOrgInvitation {
  login: string | null
  email: string | null
  role: string
  invited_at: string
  inviter: string | null
}

export interface GithubOrgInvitationsResponse {
  org: string
  invitations: GithubOrgInvitation[]
}

export interface GithubMembershipStatus {
  state: "active" | "pending"
  role: "member" | "admin"
}

export interface CollaboratorPermission {
  login: string
  avatar_url: string
  permission: "read" | "triage" | "write" | "maintain" | "admin"
  affiliation: "direct" | "outside"
  is_outside_collaborator: boolean
}

export interface RepoPermissions {
  repo: string
  collaborators: CollaboratorPermission[]
}

export interface PermissionRiskSummary {
  outside_with_write_or_admin: number
  members_with_admin: number
  total_outside_collaborators: number
}

export interface PermissionAuditResponse {
  generated_at: string
  repos_scanned: number
  repos_total: number
  repos: RepoPermissions[]
  risk_summary: PermissionRiskSummary
}

export interface InactiveMember {
  login: string
  avatar_url: string
  role: "member" | "admin"
  last_commit_repo: string | null
  last_commit_days_ago: number | null
}

export interface InactiveMembersResponse {
  org: string
  sampled_repos: string[]
  members: InactiveMember[]
}

export interface PrWeekBucket {
  week: string
  opened: number
  merged: number
}

export interface AtRiskRepo {
  repo: string
  reasons: string[]
  severity: "warning" | "critical"
}

export interface MilestoneSummary {
  repo: string
  title: string
  due_on: string | null
  open_issues: number
  closed_issues: number
  progress_pct: number
  state: "on_track" | "at_risk" | "overdue"
}

export interface PrCycleTimeWeek {
  week: string
  avg_days: number
}

export interface CockpitResponse {
  repo_count: number
  member_count: number
  latest_score: number | null
  score_trend: number[]
  recent_events: OrgEvent[]
  open_pr_count: number
  pr_merge_rate_4w: PrWeekBucket[]
  commit_activity_4w: number[]
  total_cache_size_bytes: number
  cache_job_success_rate: number
  commit_heatmap_52w: number[]
  at_risk_repos: AtRiskRepo[]
  milestones: MilestoneSummary[]
  pr_cycle_time_8w: PrCycleTimeWeek[]
  release_cadence_4w: number[]
  commit_activity_source: "github" | "aggregate"
  recent_events_source: "github" | "aggregate"
  recent_events_stale: boolean
  degraded: boolean
}

export interface MyViewPRSummary {
  number: number
  title: string
  repository: string
  html_url: string
  updated_at: string
}

export interface MyViewIssueSummary {
  number: number
  title: string
  repository: string
  html_url: string
  updated_at: string
}

export interface MyViewRunSummary {
  repository: string
  id: number
  name: string | null
  status: string
  conclusion: string | null
  html_url: string
  created_at: string
}

export interface MyViewResponse {
  my_open_prs: MyViewPRSummary[]
  review_requests: MyViewPRSummary[]
  assigned_issues: MyViewIssueSummary[]
  my_recent_runs: MyViewRunSummary[]
  // True when GitHub couldn't tell Clevis who the signed-in user is on GitHub (an
  // installation/App token can't call GET /user, and this user has no GitHub-OAuth-linked
  // login to fall back on) -- distinguishes that from "you really have zero open items."
  identity_unresolved: boolean
}

export interface MyPrListResponse {
  items: MyViewPRSummary[]
  total_count: number
  page: number
  per_page: number
  identity_unresolved: boolean
}

export interface MyIssueListResponse {
  items: MyViewIssueSummary[]
  total_count: number
  page: number
  per_page: number
  identity_unresolved: boolean
}

export interface WorkflowSummary {
  id: number
  name: string
  path: string
  state: string
  last_run_status: string | null
  last_run_conclusion: string | null
  last_run_at: string | null
}

export interface WorkflowsResponse {
  repository: string
  workflows: WorkflowSummary[]
}

export interface RunSummary {
  id: number
  name: string | null
  status: string
  conclusion: string | null
  head_branch: string
  created_at: string
  duration_ms: number | null
}

export interface RunsResponse {
  repository: string
  runs: RunSummary[]
}

export interface DispatchResponse {
  dispatched: boolean
  message: string | null
}

// issue #288 — bulk branch-protection apply
export interface BranchProtectionPreset {
  required_pull_request_reviews?: { required_approving_review_count: number } | null
  enforce_admins?: boolean
  allow_force_pushes?: boolean
  allow_deletions?: boolean
  required_status_checks?: { strict: boolean; contexts: string[] } | null
  restrictions?: null
}

export interface BranchProtectionRepoDiff {
  repo: string
  branch: string
  currently_protected: boolean
  would_change: boolean
  changes: Record<string, { from: unknown; to: unknown }>
  error: string | null
}

export interface BranchProtectionRepoResult {
  repo: string
  applied: boolean
  error: string | null
}

export interface BranchProtectionBulkResponse {
  dry_run: boolean
  diffs?: BranchProtectionRepoDiff[]
  results?: BranchProtectionRepoResult[]
}

// issue #291 — workflow policy lint + auto-fix PR
export interface WorkflowLintFinding {
  path: string
  rule: string
  severity: string
  message: string
}

export interface WorkflowLintResponse {
  findings: WorkflowLintFinding[]
  fixable: boolean
  pr_url: string | null
}

// issue #290 — Dependabot auto-triage
export interface DependabotTriageDecision {
  repo: string
  number: number | null
  title: string
  action: string
  reason: string
}

export interface DependabotTriageResponse {
  decisions: DependabotTriageDecision[]
}

export interface RepoSecurityRow {
  repo: string
  branch_protection: boolean
  secret_scanning: boolean
  dependabot_enabled: boolean
  dependabot_critical_count: number
  dependabot_high_count: number
  code_scanning: boolean
  force_push_allowed: boolean
  score: number
  // Dimension names the token couldn't evaluate (403/429/network error) -- excluded
  // from `score`. Distinct from a genuine 404 "this is off" answer, which isn't unknown.
  unknown_dimensions: string[]
  // "aggregate" when dependabot/code_scanning came from ingested webhook events instead
  // of a live GitHub call (post-S6 PR 3) -- branch_protection/force_push/secret_scanning
  // have no ingested event covering them and stay live either way.
  alerts_source: "github" | "aggregate"
}

export interface VulnCounts {
  critical: number
  high: number
  medium: number
  low: number
}

export interface MatrixSummary {
  fully_compliant_count: number
  critical_risk_count: number
  secret_hits_count: number
  vuln_by_severity: VulnCounts
}

export interface SecurityMatrixResponse {
  owner: string
  repos: RepoSecurityRow[]
  summary: MatrixSummary
}

export interface SecretAlert {
  number: number
  state: string
  secret_type: string
  secret_type_display: string
  resolved_reason: string | null
  created_at: string
  resolved_at: string | null
  repo: string
  // null when no usable link exists (e.g. aggregate-sourced alerts don't store GitHub's
  // html_url) -- the UI renders a plain non-link row in that case.
  url: string | null
}

export interface SecretScanningResponse {
  repository: string
  alerts: SecretAlert[]
  source: "github" | "aggregate"
}

// Issue #286: response from POST /me/repos/{owner}/{repo}/issues.
export interface CreateIssueResponse {
  number: number
  html_url: string
}

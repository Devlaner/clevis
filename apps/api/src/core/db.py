from collections.abc import Generator
from datetime import date, datetime
import logging

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.core.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class GitHubInstallation(Base):
    __tablename__ = "github_installations"
    __table_args__ = (
        CheckConstraint(
            "(org_id IS NOT NULL AND owner_user_id IS NULL) "
            "OR (org_id IS NULL AND owner_user_id IS NOT NULL)",
            name="ck_github_installations_org_xor_owner",
        ),
        Index(
            "uq_github_installations_org_account",
            "org_id",
            "account_login",
            unique=True,
            postgresql_where="org_id IS NOT NULL",
        ),
        Index(
            "uq_github_installations_user_account",
            "owner_user_id",
            "account_login",
            unique=True,
            postgresql_where="owner_user_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_login: Mapped[str] = mapped_column(String, nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auth_mode: Mapped[str] = mapped_column(String, nullable=False)
    token_ref: Mapped[str] = mapped_column(String, nullable=False)
    # Exactly one of org_id / owner_user_id is set: org-connected installs vs. personal installs.
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Backfilled from org_id/owner_user_id by migration 0025, dual-written by installation_repo
    # since PR 4, NOT NULL enforced by migration 0029 now that PR 4's dual-write covers every
    # installation-creation path.
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable, no historical backfill (migration 0028) -- actor/target are free text, not
    # FKs, so pre-migration rows can't be reliably attributed to a tenant. Per the design
    # decision on #190, these stay visible only via a require_workspace_admin-gated view
    # once RLS lands, never to ordinary tenant members.
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_job_type", "status", "job_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Counts attempts caused by either the worker reclaiming a job stuck in
    # 'processing' (its worker likely crashed) or a transient failure being requeued —
    # a single shared cap on both prevents a job from retrying forever either way.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Touched by the worker every ~10s while a job handler is actually running (see
    # apps/worker/src/worker.py's _JobHeartbeat / _touch_job_heartbeat). Lets _reclaim_stale_jobs tell
    # a genuinely slow-but-alive job apart from one whose worker crashed -- updated_at alone
    # can't, since it's only set at claim time and doesn't change again until the job
    # finishes. Null for a job that was claimed before this column existed, or hasn't had
    # its first heartbeat tick yet.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDelivery(Base):
    """Durable landing spot for verified GitHub webhook payloads (issue #191/S3), written by
    the unauthenticated receiver before enqueueing onto Redis Streams. No RLS: like `jobs`,
    this is a system-internal table -- the receiver writes it with no app.tenant_id session
    var set (there's no authenticated tenant context at webhook-receive time), and a future
    S4 consumer fleet needs to read across all tenants, not one tenant's rows at a time. Access
    control is HMAC-signature verification at the receiver, not row-level tenant isolation."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_tenant_id", "tenant_id"),
        Index("ix_webhook_deliveries_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Nullable: resolution can fail (installation not found -- e.g. a webhook for an org that
    # uninstalled between delivery and processing).
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    # X-GitHub-Delivery. Not unique-constrained: GitHub redelivers the same delivery id on
    # retry, and deduping is the future S4 event-processor's job, not this table's.
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)  # X-GitHub-Event
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Exact verified raw body bytes, not re-serialized JSON, so a byte-for-byte copy of what
    # HMAC was checked against is preserved for any future signature re-verification/replay.
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # queued | queue_failed -- queue_failed lets a future re-enqueue sweep find rows whose
    # Redis XADD didn't succeed even though the payload itself was durably stored.
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoEvent(Base):
    """Normalized, deduplicated event store (issue #191/S4 PR 1), populated by
    apps/worker's Redis Streams consumer from webhook_deliveries rows. Not read by the
    API yet -- that's S6's job. RLS-enabled (migration 0036), strict tenant_id equality
    (no OR-NULL group): the consumer deliberately skips normalizing any
    webhook_deliveries row with a null tenant_id rather than write one here, so this
    table never has a null tenant_id row to begin with."""

    __tablename__ = "repo_events"
    __table_args__ = (
        Index("ix_repo_events_tenant_id", "tenant_id"),
        UniqueConstraint("delivery_id", name="uq_repo_events_delivery_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    # X-GitHub-Delivery -- the actual idempotency key; ON CONFLICT DO NOTHING on this
    # column is what makes normalization safe to run twice on a redelivered event.
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    actor_avatar: Mapped[str] = mapped_column(String, nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    # The event's own timestamp where the raw payload has one; falls back to the
    # webhook_deliveries row's received_at otherwise (see event_consumer.py) -- not
    # every ingested event type has one canonical top-level timestamp field.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoEventDailyCount(Base):
    """Materialized rollup half of S4 (issue #191), migration 0037. Upserted by
    apps/worker's event_consumer.py in the same transaction as each RepoEvent insert --
    not a separate batch job -- and only when that insert actually happened (a
    redelivered/deduped event must not double-count here). Not read by the API yet --
    S6's job, same deferral as RepoEvent. Composite PK instead of a surrogate id: this
    row is purely identified by (tenant_id, repo, event_type, day), it's an upsert
    target, and nothing references it by foreign key."""

    __tablename__ = "repo_event_daily_counts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "repo", "event_type", "day", name="pk_repo_event_daily_counts"),
        Index("ix_repo_event_daily_counts_tenant_id_day", "tenant_id", "day"),
    )

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class SecurityAlert(Base):
    """Normalized store for GitHub Security-alert webhook events (dependabot_alert/
    code_scanning_alert/secret_scanning_alert), migration 0039 (post-S6, PR 2 of 3).
    Populated by apps/worker's event_consumer.py from webhook_deliveries rows already
    durably queued since PR #350. Not read by the API yet -- that's PR 3's job, same
    deferral pattern as RepoEvent was under S4 before S6 read it.

    One polymorphic table (kind discriminates dependabot/code_scanning/secret_scanning)
    rather than three near-identical tables -- see migration 0039's docstring for the
    full rationale. Upserted (not insert-and-skip like RepoEvent): a redelivered alert
    webhook reflects a real state transition (e.g. open -> dismissed), not a duplicate
    of an immutable log entry, so (tenant_id, repo, kind, number) is an upsert key, not
    a dedupe-and-drop key."""

    __tablename__ = "security_alerts"
    __table_args__ = (
        Index("ix_security_alerts_tenant_id", "tenant_id"),
        Index("ix_security_alerts_tenant_id_repo", "tenant_id", "repo"),
        UniqueConstraint("tenant_id", "repo", "kind", "number", name="uq_security_alerts_tenant_repo_kind_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrgMember(Base):
    """Normalized store for org membership, migration 0040 (Collaborators PR 1 of 3).
    Populated by apps/worker's event_consumer.py from the `organization` webhook event's
    member_added/member_removed actions. Not read by the API yet -- that's PR 3's job.

    Represents current membership, not a log: a row is deleted on member_removed, not
    soft-marked. `role` is captured from the member_added payload's membership.role at
    add-time (accurate then), but no webhook event covers a role changing *afterward* at
    all (verified against GitHub's docs -- no member_updated/role-change action exists) --
    so this column can silently drift stale for an existing member whose role later
    changes, until a future reconciliation poll (Collaborators PR 2, not built yet)
    corrects it. See migration 0040's docstring for the full rationale."""

    __tablename__ = "org_members"
    __table_args__ = (
        Index("ix_org_members_tenant_id", "tenant_id"),
        UniqueConstraint("tenant_id", "login", name="uq_org_members_tenant_login"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    login: Mapped[str] = mapped_column(String, nullable=False)
    avatar_url: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="member")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RepoCollaborator(Base):
    """Normalized store for direct repo access grants, migration 0040 (Collaborators PR 1
    of 3). Populated by apps/worker's event_consumer.py from the `member` webhook event's
    added/edited/removed actions. Not read by the API yet -- that's PR 3's job.

    `source` is 'direct' only in this PR -- team-based repo access ('team') is explicitly
    deferred (requires joining the `team` event against `membership`'s per-team roster, a
    materially bigger modeling problem than a direct grant, and GitHub's own team-event
    payloads don't reliably convey the permission level either). See migration 0040's
    docstring for the full rationale. Represents current access, not a log: a row is
    deleted on a `removed` action, not soft-marked."""

    __tablename__ = "repo_collaborators"
    __table_args__ = (
        Index("ix_repo_collaborators_tenant_id", "tenant_id"),
        Index("ix_repo_collaborators_tenant_id_repo", "tenant_id", "repo"),
        UniqueConstraint("tenant_id", "repo", "login", name="uq_repo_collaborators_tenant_repo_login"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    login: Mapped[str] = mapped_column(String, nullable=False)
    permission: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="direct")
    is_outside_collaborator: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActivitySyncCursor(Base):
    """Per-tenant "how far synced" cursor (issue #192, S5 PR 2), migration 0038. One row per
    tenant, upserted by apps/worker's github.backfill_repo_events handler after every successful
    run -- both the install-time trigger (S5 PR 1, PR #342) and the scheduled gap-heal sweep this
    PR adds. Per-tenant, not per-repo: GitHub's Events API is org/user-scoped, not repo-scoped, so
    there is nothing to key a per-repo cursor on yet. last_synced_at is nullable -- a row doesn't
    exist until the tenant's first successful sync completes."""

    __tablename__ = "activity_sync_cursors"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    account_login: Mapped[str] = mapped_column(String, nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SavedToken(Base):
    __tablename__ = "saved_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Best-effort backfill by migration 0027, matched on org (free text, not an FK) against
    # orgs.github_login -- stays nullable since legacy rows for a renamed/deleted org won't match.
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Case-insensitive uniqueness (see migration 0019) -- Postgres can't express "unique
        # ignoring case" through a plain column constraint, so this is a functional index
        # instead. Callers must query/insert via a lowercase comparison (see src.routers.auth,
        # src.routers.github_auth); this index alone doesn't normalize existing values.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null for users who only sign in with GitHub (no email/password credential).
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_workspace_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Bumped by POST /auth/me/revoke-sessions to invalidate all previously issued JWTs.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # GitHub identity (set when the user links / signs in via GitHub OAuth).
    github_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    github_login: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # True for GitHub-linked accounts (GitHub already vouches for the email) and for the
    # first-run /auth/setup admin (the deploying operator, implicitly trusted). False for
    # self-service /auth/register accounts until they click the emailed verification link.
    # Existing rows at migration time are backfilled true (see 0017) -- already-deployed
    # users are grandfathered in as trusted, only new self-registrations start unverified.
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verify_token: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    email_verify_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Org(Base):
    __tablename__ = "orgs"
    __table_args__ = (
        # Composite FK instead of a plain tenant_id -> tenants.id FK: requires that
        # whatever tenant tenant_id names must itself have org_id = this exact org's id,
        # so tenant_id can't point at a personal tenant, another org's tenant, or a tenant
        # shared across multiple orgs (see migration 0024's docstring).
        ForeignKeyConstraint(["tenant_id", "id"], ["tenants.id", "tenants.org_id"], name="fk_orgs_tenant_id_reciprocal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Nullable: not known for orgs backfilled from pre-existing installations; filled in
    # lazily the next time a member of the org authenticates and the GitHub membership
    # check runs.
    github_org_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    github_login: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 1:1 pointer to this org's tenant row (migration 0022 backfills one 'org'-kind tenant
    # per org before this column exists; migration 0024 adds + backfills it; org_repo has
    # dual-written it on every org since PR 4). Stays nullable permanently, unlike
    # invitations/github_installations.tenant_id (migration 0029) -- creating a brand-new
    # org requires the Org and its reciprocal Tenant row to each reference the other's
    # not-yet-existing id, and Postgres NOT NULL can't be deferred like a FK can.
    # org_repo.get_or_create's self-healing dual-write plus the verification queries in
    # migration 0029's docstring are the ongoing guarantee instead.
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # "admin" | "member"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'org' AND org_id IS NOT NULL AND personal_user_id IS NULL) "
            "OR (kind = 'personal' AND org_id IS NULL AND personal_user_id IS NOT NULL)",
            name="ck_tenants_kind_xor",
        ),
        Index("uq_tenants_org_id", "org_id", unique=True, postgresql_where="org_id IS NOT NULL"),
        Index(
            "uq_tenants_personal_user_id",
            "personal_user_id",
            unique=True,
            postgresql_where="personal_user_id IS NOT NULL",
        ),
        # Lets orgs.tenant_id declare a composite FK to (id, org_id), enforcing that an
        # org's tenant_id can only point at a tenant whose org_id reciprocally points back
        # at that same org -- not a personal tenant, another org's tenant, or a tenant
        # shared by multiple orgs. Redundant with id's own PK uniqueness in isolation, but
        # required for the composite FK to be legal.
        UniqueConstraint("id", "org_id", name="uq_tenants_id_org_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "org" | "personal"
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True)
    personal_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # "admin" | "member"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending|accepted|revoked
    invited_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Backfilled from org_id's tenant by migration 0026, dual-written by invitation_repo
    # since PR 4, NOT NULL enforced by migration 0029.
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)


class ScanResult(Base):
    __tablename__ = "scan_results"
    __table_args__ = (Index("ix_scan_results_owner_created_at", "owner", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    total_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Set for personal-endpoint scans only -- lets GET /me/analytics/history gate
    # access to owners with no workspace Org/membership to check instead (see
    # migration 0016). Org-scoped scans leave this null; that read path is gated
    # by org membership, not this column.
    scanned_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Best-effort backfill by migration 0027: matched via owner -> orgs.github_login first,
    # falling back to scanned_by_user_id's personal tenant. Stays nullable -- some legacy
    # rows (renamed/deleted org) won't match either path.
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


engine = create_engine(settings.database_url.get_secret_value())
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def set_session_user(db: Session, user_id: int) -> None:
    """Sets app.user_id alone, for write paths that know the acting user but not (yet, or
    ever) a single tenant -- e.g. require_auth (every authenticated request), OAuth-login
    provisioning across multiple orgs, and pre-login user-creation flows. Issue #190 step
    6c: migration 0031's memberships/github_installations policies accept a row whose
    user_id/owner_user_id matches this alongside the existing tenant_id match, since every
    write to those tables is confirmed self-scoped (the row's own user, never someone
    else's). src.core.rbac's _set_tenant_session_context sets both this and app.tenant_id
    together once a specific tenant is actually known; this is the narrower, more widely
    applicable half of that. Same plain-SET reasoning as that function -- see its docstring.
    """
    db.execute(text(f"SET app.user_id = {int(user_id)}"))


def set_session_tenant(db: Session, tenant_id: int) -> None:
    """Sets app.tenant_id alone, for system code paths that resolve a tenant_id without an
    acting user -- e.g. the GitHub webhook receiver (issue #191/S3), which runs before any
    authenticated session exists (HMAC-signature-verified, not login-gated), so there's no
    app.user_id to set alongside it. Satisfies the tenant_id-equality half of migration
    0031's github_installations/memberships policies. The tenant_id here must come from a
    trusted resolution (e.g. resolve_installation_tenant_id(), migration 0035's SECURITY
    DEFINER function) -- this call only sets session state for RLS to read, it does not
    itself verify the caller is entitled to that tenant. Same plain-SET reasoning as
    set_session_user/rbac.set_tenant_session_context -- see their docstrings.
    """
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        # require_org_role/require_personal_tenant (src.core.rbac) set app.tenant_id/
        # app.user_id via plain SET (not SET LOCAL, since a request can commit more than
        # once and SET LOCAL would stop applying after the first commit). Plain SET persists
        # for the life of the physical connection, not just this request's Session -- since
        # SQLAlchemy's connection pool only rolls back pending transactions on checkin, an
        # already-committed SET would otherwise silently leak into whichever unrelated
        # request reuses this connection next. Reset explicitly before returning to the pool.
        try:
            db.rollback()
            db.execute(text("RESET app.tenant_id"))
            db.execute(text("RESET app.user_id"))
            db.commit()
            db.close()
        except Exception:
            # A failure partway through (e.g. a transient network drop after RESET but
            # before commit) leaves it unclear whether the reset actually took -- closing
            # normally here would return that connection to the pool for reuse anyway.
            # invalidate() instead forces the pool to discard the underlying DBAPI
            # connection rather than risk handing a possibly-still-tenant-scoped
            # connection to an unrelated later request.
            logger.exception("failed to reset tenant session context; invalidating connection instead of reusing it")
            db.invalidate()

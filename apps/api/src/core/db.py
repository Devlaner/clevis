from collections.abc import Generator
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.core.config import settings


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
    # not-yet-existing id, and Postgres NOT NULL can't be deferred like a FK can. See
    # migration 0029's docstring. org_repo.get_or_create's self-healing dual-write plus
    # docs/tenant-id-verification.md's checks are the ongoing guarantee instead.
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


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

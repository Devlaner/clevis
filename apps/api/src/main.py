import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import settings
from src.core.logging import setup_logging
from src.core.middleware import RequestIdMiddleware
from src.routers import (
    actions_cache,
    analytics,
    audit,
    auth,
    automation,
    branch_protection,
    collab,
    config,
    dependabot_triage,
    github,
    github_auth,
    health,
    installations,
    invitations,
    issues,
    jobs,
    orgs,
    pr_nudges,
    remediation,
    repos,
    security,
    tokens,
    webhooks,
    workflow_lint,
)
from src.services.digest_loop import digest_loop
from src.services.gap_heal_loop import gap_heal_loop
from src.services.membership_reconcile_loop import membership_reconcile_loop

# CORS allowed origins are a deploy-time security boundary, set via the CORS_ORIGINS env var.
_cors_origins = settings.cors_origins


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    # Two independent background loops -- activity-sync gap-healing (S5) and org-membership
    # reconciliation (Collaborators PR 2 of 3) are unrelated sweeps, each already tolerant of
    # a single iteration's exception without dying, so there's no reason to share one task.
    tasks = [
        asyncio.create_task(gap_heal_loop()),
        asyncio.create_task(membership_reconcile_loop()),
        # Issue #292: leadership digest. A no-op unless digest_cadence is configured.
        asyncio.create_task(digest_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="clevis API",
    version="0.1.0",
    lifespan=lifespan,
    # Interactive docs are intentionally disabled in all environments so the API
    # surface is never published. Use Postman/curl for manual endpoint testing.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Required so the browser sends the httpOnly session cookie on cross-origin (UI->API)
    # requests. Note: credentialed CORS is incompatible with a "*" origin — CORS_ORIGINS must
    # list explicit UI origins in any deployment that relies on the cookie session.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(github_auth.router, prefix="/auth/github", tags=["github-auth"])
app.include_router(installations.router, tags=["installations"])
app.include_router(orgs.router, tags=["orgs"])
app.include_router(invitations.router, tags=["invitations"])
app.include_router(analytics.router, tags=["analytics"])
app.include_router(actions_cache.router, tags=["actions-cache"])
app.include_router(repos.router, tags=["repos"])
app.include_router(github.router, tags=["github"])
app.include_router(collab.router, tags=["collab"])
app.include_router(security.router, tags=["security"])
app.include_router(issues.router, tags=["issues"])
app.include_router(remediation.router, tags=["security"])
app.include_router(automation.router, tags=["automation"])
app.include_router(pr_nudges.router, tags=["pull-requests"])
app.include_router(branch_protection.router, tags=["automation"])
app.include_router(workflow_lint.router, tags=["automation"])
app.include_router(dependabot_triage.router, tags=["automation"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(audit.router, prefix="/audit", tags=["audit"])
app.include_router(tokens.router, prefix="/tokens", tags=["tokens"])
app.include_router(config.router, prefix="/config", tags=["config"])
app.include_router(webhooks.router, tags=["webhooks"])

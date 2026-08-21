"""S5 PR 1: install-time backfill of recent org/user activity into repo_events.

Runs as a one-shot `github.backfill_repo_events` job through the existing apps/worker
jobs poll loop (see worker.py's _handle_backfill_repo_events) -- reuses that machinery
(SELECT ... FOR UPDATE SKIP LOCKED, heartbeat, retry/failure handling) rather than a new
execution path. Calls the same GitHub Events API the live Activity Feed already polls
(apps/api/src/routers/github.py's org_events endpoint) -- GitHub caps this at roughly
the last ~90 days / ~300 events, so this is "recent activity on connect", not a deep
historical backfill (that would require composing from commits/PRs/issues REST
endpoints instead -- explicitly deferred, see the S5 PR 1 plan notes).

Maps only the 5 event types apps/worker/src/event_consumer.py already tracks from live
webhooks, to the same lowercase event_type vocabulary, so a repo_events row looks the
same regardless of source. Synthetic delivery_id ("backfill:<github event id>") keeps
this idempotent against a retried job or a second install-sync call, via the same
ON CONFLICT (delivery_id) DO NOTHING repo_events_store.py relies on.

No bot filtering here (unlike the live Activity Feed's presentation-layer _is_bot filter
in apps/api/src/routers/github.py) -- repo_events itself stores every actor unfiltered
regardless of source; filtering is a read-time/display concern, not a storage concern.
"""

from datetime import datetime

import httpx

# Self-imposed ceiling on top of GitHub's own Events API cap (~300 events total) -- not
# the primary limit, just a bound so a pathological Link-header loop can't run forever.
_MAX_PAGES = 3
_PER_PAGE = 100

_EVENT_TYPE_MAP = {
    "PushEvent": "push",
    "PullRequestEvent": "pull_request",
    "IssuesEvent": "issues",
    "ReleaseEvent": "release",
    "CreateEvent": "create",
}


def _events_path(account_login: str, account_type: str) -> str:
    # account_type mirrors github_installations.account_type ("User" for a personal
    # install, anything else -- "Organization" in practice -- for an org install).
    if account_type == "User":
        return f"/users/{account_login}/events"
    return f"/orgs/{account_login}/events"


def fetch_events(client: httpx.Client, base: str, headers: dict, account_login: str, account_type: str) -> list[dict]:
    """Follows the Link: rel="next" header (httpx parses it into response.links) up to
    _MAX_PAGES. Raises httpx.HTTPStatusError/RequestError on failure -- callers map
    those to the same requeue/fail decision worker.py's other job handlers already use."""
    events: list[dict] = []
    url = f"{base}{_events_path(account_login, account_type)}"
    params: dict | None = {"per_page": _PER_PAGE}
    for _ in range(_MAX_PAGES):
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            break
        events.extend(page)
        next_link = resp.links.get("next")
        if not next_link:
            break
        url = next_link["url"]
        params = None  # already encoded in the next link's URL
    return events


def _summarize(event_type: str, payload: dict) -> str:
    """Mirrors event_consumer.py's _summarize, adapted to the Events API's nested
    `payload` sub-object shape (vs. a raw webhook body's top-level fields) -- the same
    per-type mapping apps/api/src/routers/github.py's own _summarize already implements
    for the live feed, duplicated here rather than imported across the service
    boundary (apps/worker doesn't import apps/api -- same precedent as
    event_consumer.py's own _summarize)."""
    if event_type == "push":
        size = payload.get("size")
        commits = payload.get("commits") or []
        count = size if isinstance(size, int) else len(commits)
        branch = (payload.get("ref") or "").removeprefix("refs/heads/")
        noun = "commit" if count == 1 else "commits"
        return f"pushed {count} {noun} to {branch}" if branch else f"pushed {count} {noun}"

    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        verb = "merged" if action == "closed" and pr.get("merged") else action
        return f"{verb} PR #{payload.get('number')}: {pr.get('title', '')}"

    if event_type == "issues":
        issue = payload.get("issue") or {}
        action = payload.get("action", "")
        return f"{action} issue #{issue.get('number')}: {issue.get('title', '')}"

    if event_type == "release":
        release = payload.get("release") or {}
        return f"created release {release.get('tag_name', '')}"

    if event_type == "create":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref") or ""
        return f"created {ref_type} {ref}".strip()

    return event_type


def normalize(raw_event: dict) -> dict | None:
    """Returns repo_events column values (minus tenant_id, which the caller already
    knows from the job payload) for one Events-API event, or None if it's not one of
    the 5 tracked types or is missing a field a real GitHub event always has."""
    event_type = _EVENT_TYPE_MAP.get(raw_event.get("type"))
    if event_type is None:
        return None
    event_id = raw_event.get("id")
    repo = (raw_event.get("repo") or {}).get("name")
    created_at_raw = raw_event.get("created_at")
    if not event_id or not repo or not created_at_raw:
        return None
    try:
        occurred_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    actor = raw_event.get("actor") or {}
    payload = raw_event.get("payload") or {}
    return {
        "delivery_id": f"backfill:{event_id}",
        "event_type": event_type,
        "actor": actor.get("login", ""),
        "actor_avatar": actor.get("avatar_url", ""),
        "repo": repo,
        "summary": _summarize(event_type, payload),
        "occurred_at": occurred_at,
    }

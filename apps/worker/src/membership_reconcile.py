"""Collaborators PR 2 of 3: full org-roster GitHub fetch for the reconciliation poll.

Runs as a one-shot `github.reconcile_org_membership` job through the existing apps/worker jobs
poll loop (see worker.py's _handle_reconcile_org_membership) -- same machinery as backfill.py's
`github.backfill_repo_events` (SELECT ... FOR UPDATE SKIP LOCKED, heartbeat, retry/failure
handling), not a new execution path.

The GitHub calls and their fallback posture mirror apps/api/src/routers/collab.py's
list_members/list_outside_collaborators exactly (same endpoints, same admin-filter role
cross-reference, same "2FA overlay is best-effort" shape) -- this is the same data, just fetched
from a background job instead of a request handler, so a Clevis org's roster stays correct even
between page loads. apps/worker doesn't import apps/api (established precedent, see backfill.py's
own docstring), so the pagination/retry helpers below duplicate backfill.py's
_get_with_retry/_retry_delay_seconds/_is_secondary_rate_limit rather than importing them --
same reasoning as event_consumer.py's own duplicated _summarize.
"""

import time

import httpx

_PER_PAGE = 100
_MAX_RETRY_AFTER_SECONDS = 60


class RosterIncomplete(Exception):
    """Raised when _get_all_pages can't return the full page set -- GitHub's pagination looped
    back to an already-fetched URL, a page body wasn't a list, or a page body wasn't valid JSON.

    Distinct from httpx.HTTPStatusError/RequestError: those mean "the request failed", this
    means "requests succeeded but the result can't be trusted as complete" --
    reconcile_org_members treats fetch_org_roster's member list as authoritative and DELETEs
    anyone not in it, so returning a partial list here would look identical to real departures
    and wipe real members. Must never be swallowed by the members/admins/outside_collaborators
    calls; the 2FA overlay call treats it the same as a failed overlay (best-effort, preserves
    existing values) since it isn't destructive the same way.
    """


def _get_all_pages(client: httpx.Client, base: str, headers: dict, path: str, params: dict) -> list[dict]:
    """Follows the Link: rel="next" header until it runs out -- an org roster can genuinely
    span far more pages than backfill.py's self-limited Events API call, so this doesn't cap
    at some fixed page count (a real large org would just permanently fail to reconcile).
    Instead it tracks visited URLs and raises RosterIncomplete if pagination ever loops back to
    one -- the only page-count anomaly that's actually a bug, not just "a big org". Also raises
    RosterIncomplete for a non-list or non-JSON page body. Raises
    httpx.HTTPStatusError/RequestError once _get_with_retry's own retries are exhausted;
    callers decide requeue-vs-fail for either exception."""
    results: list[dict] = []
    url = f"{base}{path}"
    page_params: dict | None = {**params, "per_page": _PER_PAGE}
    seen_urls: set[str] = set()
    while True:
        if url in seen_urls:
            raise RosterIncomplete(f"{path!r} pagination looped back to an already-fetched page")
        seen_urls.add(url)
        resp = _get_with_retry(client, url, headers, page_params)
        resp.raise_for_status()
        try:
            page = resp.json()
        except ValueError as error:
            raise RosterIncomplete(f"non-JSON page body from {path!r}: {error}") from error
        if not isinstance(page, list):
            raise RosterIncomplete(f"expected a list page from {path!r}, got {type(page).__name__}")
        results.extend(page)
        next_link = resp.links.get("next")
        if not next_link:
            break
        url = next_link["url"]
        page_params = None  # already encoded in the next link's URL
    return results


def _is_secondary_rate_limit(resp: httpx.Response) -> bool:
    if resp.status_code != 403:
        return False
    return "Retry-After" in resp.headers or resp.headers.get("X-RateLimit-Remaining") == "0"


def _retry_delay_seconds(resp: httpx.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    if raw is not None:
        try:
            return min(float(raw), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    reset_raw = resp.headers.get("X-RateLimit-Reset")
    if reset_raw is not None:
        try:
            delay = float(reset_raw) - time.time()
            if delay > 0:
                return min(delay, _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    if resp.status_code == 429 or _is_secondary_rate_limit(resp):
        return _MAX_RETRY_AFTER_SECONDS
    return 2**attempt


def _get_with_retry(client: httpx.Client, url: str, headers: dict, params: dict | None) -> httpx.Response:
    for attempt in range(3):
        try:
            resp = client.get(url, headers=headers, params=params)
        except httpx.RequestError:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise
        if (resp.status_code == 429 or _is_secondary_rate_limit(resp) or resp.status_code >= 500) and attempt < 2:
            time.sleep(_retry_delay_seconds(resp, attempt))
            continue
        return resp
    raise RuntimeError("request loop exhausted without returning")


def fetch_org_roster(client: httpx.Client, base: str, headers: dict, org_login: str) -> dict:
    """Returns {"members": [...], "two_factor_disabled_logins": set|None, "outside_logins": set}.

    members: role is resolved the same way collab.py's list_members does (a member is "admin"
    iff their login appears in the role=admin-filtered call, "member" otherwise) -- GitHub's
    plain member list doesn't carry role directly.

    two_factor_disabled_logins is None if the overlay call itself failed -- same best-effort
    posture as collab.py's list_members (`filter=2fa_disabled` needs org-owner scope a token
    might lack), kept distinct from an empty set ("checked, nobody has 2FA disabled") so the
    caller doesn't overwrite previously known-good data with a false negative.
    """
    admins_raw = _get_all_pages(client, base, headers, f"/orgs/{org_login}/members", {"role": "admin"})
    all_raw = _get_all_pages(client, base, headers, f"/orgs/{org_login}/members", {"role": "all"})
    admin_logins = {m["login"] for m in admins_raw if "login" in m}
    members = [
        {
            "login": m["login"],
            "avatar_url": m.get("avatar_url", ""),
            "role": "admin" if m["login"] in admin_logins else "member",
        }
        for m in all_raw
        if "login" in m
    ]

    two_factor_disabled_logins: set[str] | None
    try:
        no_2fa_raw = _get_all_pages(client, base, headers, f"/orgs/{org_login}/members", {"filter": "2fa_disabled"})
        two_factor_disabled_logins = {m["login"] for m in no_2fa_raw if "login" in m}
    except (httpx.HTTPStatusError, httpx.RequestError, RosterIncomplete):
        two_factor_disabled_logins = None

    outside_raw = _get_all_pages(client, base, headers, f"/orgs/{org_login}/outside_collaborators", {})
    outside_logins = {c["login"] for c in outside_raw if "login" in c}

    return {
        "members": members,
        "two_factor_disabled_logins": two_factor_disabled_logins,
        "outside_logins": outside_logins,
    }

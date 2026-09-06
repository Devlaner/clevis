import time
import httpx
from fastapi import HTTPException

from src.core.config import settings


def _is_secondary_rate_limit(resp: httpx.Response) -> bool:
    """GitHub's secondary/abuse rate limit commonly returns 403 (not 429), often with a
    Retry-After header -- especially under this codebase's concurrent ThreadPoolExecutor
    fan-out (repos.py, collab.py, analytics.py). Without this, a 403 that would succeed on
    retry raises immediately instead of backing off like the 429 case already does. A
    genuine permission-denied 403 (e.g. token lacks scope) has neither header, so it's
    unaffected and still surfaces immediately."""
    if resp.status_code != 403:
        return False
    return "Retry-After" in resp.headers or resp.headers.get("X-RateLimit-Remaining") == "0"


def github_error(exc: Exception) -> HTTPException:
    """Map an httpx exception raised by GitHubClient into the HTTPException every
    GitHub-proxying router returns to its caller."""
    if isinstance(exc, httpx.HTTPStatusError):
        detail = f"GitHub API error: {exc.response.status_code}"
        try:
            message = exc.response.json().get("message")
        except (ValueError, AttributeError):
            message = None
        if message:
            detail = f"{detail}: {message}"
        return HTTPException(status_code=400, detail=detail)
    if isinstance(exc, httpx.RequestError):
        return HTTPException(status_code=503, detail="GitHub API unreachable")
    raise exc


class GitHubClient:
    def __init__(self, token: str, base_url: str | None = None):
        self.base = base_url or settings.github_api_base
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict | list:
        url = f"{self.base}{path}"
        with httpx.Client(timeout=20) as client:
            for attempt in range(3):
                try:
                    resp = client.request(method, url, headers=self.headers, params=params, json=json)
                except httpx.RequestError:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise
                if (resp.status_code == 429 or _is_secondary_rate_limit(resp)) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json() if resp.text else {}
        raise RuntimeError("request loop exhausted without returning")

    def request_paginated(self, path: str, params: dict | None = None) -> list:
        """GET every page of a list endpoint, following the `Link: rel="next"` header."""
        results: list = []
        url: str | None = f"{self.base}{path}"
        next_params = dict(params or {})
        next_params.setdefault("per_page", 100)
        with httpx.Client(timeout=20) as client:
            while url:
                resp = None
                for attempt in range(3):
                    try:
                        resp = client.get(url, headers=self.headers, params=next_params)
                    except httpx.RequestError:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise
                    if (resp.status_code == 429 or _is_secondary_rate_limit(resp)) and attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    resp.raise_for_status()
                    break
                results.extend(resp.json())
                next_params = {}
                url = None
                for part in resp.headers.get("Link", "").split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
        return results

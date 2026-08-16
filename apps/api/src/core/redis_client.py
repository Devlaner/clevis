"""Thin Redis client accessor for the webhook ingestion queue (issue #191/S3).

A single process-wide client (redis-py pools connections internally, so this isn't
"one connection" -- it's one client object reusing a pool), lazily constructed from
settings.redis_url the first time it's needed rather than at import time, so a test
run that never touches Redis never has to have it reachable.
"""

import redis

from src.core.config import settings

_client: redis.Redis | None = None


# redis-py 5.2.1 defaults both timeouts to None (block forever). The webhook receiver
# calls xadd() synchronously from inside an async route (src/routers/webhooks.py), so an
# unresponsive-but-not-yet-refused Redis (e.g. a network partition, not just "connection
# refused") would otherwise stall the whole event loop, not just this one request --
# unlike a fast connection error, which the existing try/except already handles fine.
# Finite but generous relative to a webhook receiver's expected response time; no
# automatic retries (the caller's own queue_failed fallback is the retry story, and
# retrying writes here risks a duplicate XADD with no dedupe defined yet).
_SOCKET_CONNECT_TIMEOUT_SECONDS = 2
_SOCKET_TIMEOUT_SECONDS = 2


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
    return _client

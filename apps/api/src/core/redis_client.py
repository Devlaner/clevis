"""Thin Redis client accessor for the webhook ingestion queue (issue #191/S3).

A single process-wide client (redis-py pools connections internally, so this isn't
"one connection" -- it's one client object reusing a pool), lazily constructed from
settings.redis_url the first time it's needed rather than at import time, so a test
run that never touches Redis never has to have it reachable.
"""

import redis

from src.core.config import settings

_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    return _client

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_root = Path(__file__).resolve().parent
_env_file: str | None = None
while _root != _root.parent:
    candidate = _root / ".env"
    if candidate.exists():
        _env_file = str(candidate)
        break
    _root = _root.parent


class Settings(BaseSettings):
    # Secrets + the deploy-time GitHub API base (env). worker_poll_seconds stays in the
    # app_config table so it can be tuned live without a restart.
    database_url: SecretStr
    job_secret_key: SecretStr
    github_api_base: str = "https://api.github.com"
    # issue #191/S4 -- webhook_events Redis Stream consumer (event_consumer.py). Mirrors
    # apps/api/src/core/config.py's redis_url exactly (same SecretStr reasoning: a
    # production URL may embed AUTH credentials).
    redis_url: SecretStr

    model_config = SettingsConfigDict(env_file=_env_file, extra="ignore")


settings = Settings()

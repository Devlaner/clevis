"""
Instance configuration router — /config

GET  /config          Returns all app_config key/value pairs (owner only)
PUT  /config/{key}    Updates a config value (owner only)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.core.app_config import _ACCEPTED_KEYS, read_all, set_config
from src.core.auth import UserOut, require_workspace_admin

logger = logging.getLogger(__name__)
router = APIRouter()

_INT_KEYS = {
    "worker_poll_seconds",
    "gap_heal_poll_seconds",
    "gap_heal_stale_hours",
    "pr_nudge_stale_days",
    "digest_poll_seconds",
}
_BOOL_KEYS = {"registration_enabled"}
# key -> allowed values, for small closed-vocabulary settings (issues #292, #289).
_ENUM_KEYS = {
    "digest_cadence": {"off", "weekly", "monthly"},
    "pr_nudge_mode": {"off", "comment", "label"},
}


class ConfigValue(BaseModel):
    value: str


@router.get("", response_model=dict[str, str])
def get_all_config(_user: UserOut = Depends(require_workspace_admin)) -> dict[str, str]:
    """Return all instance config values. Owner only."""
    return read_all()


@router.put("/{key}", response_model=dict[str, str])
def update_config(
    key: str,
    body: ConfigValue,
    _user: UserOut = Depends(require_workspace_admin),
) -> dict[str, str]:
    """Update a single config value. Owner only."""
    if key not in _ACCEPTED_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    # Type validation
    if key in _INT_KEYS:
        try:
            parsed_int = int(body.value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{key} must be an integer")
        if parsed_int < 1:
            raise HTTPException(status_code=422, detail=f"{key} must be at least 1")

    if key in _BOOL_KEYS and body.value not in ("true", "false"):
        raise HTTPException(status_code=422, detail=f"{key} must be 'true' or 'false'")

    if key in _ENUM_KEYS and body.value not in _ENUM_KEYS[key]:
        allowed = ", ".join(sorted(_ENUM_KEYS[key]))
        raise HTTPException(status_code=422, detail=f"{key} must be one of: {allowed}")

    try:
        set_config(key, body.value)
    except Exception:
        logger.exception("Failed to update config key %r", key)
        raise HTTPException(status_code=500, detail="Failed to update config")

    return read_all()

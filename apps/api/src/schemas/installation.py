from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SyncInstallationsInput(BaseModel):
    auth_mode: str = "app"
    account_login: str
    account_type: str = "Organization"
    installation_id: int | None = None


class SyncInstallationsResponse(BaseModel):
    synced: bool
    token_ref: str


class InstallationLookupOut(BaseModel):
    account_login: str
    account_type: str


class BlockedFeatureOut(BaseModel):
    feature: str
    label: str
    missing: dict[str, str]


class InstallationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_login: str
    account_type: str
    installation_id: int | None
    created_at: datetime
    # Permission-drift fields (issue: GitHub App re-consent). `permissions_synced_at` is
    # None for installs that predate permission tracking / the first accept webhook —
    # `blocked_features` is empty in that case too, so the UI shows a "not yet checked"
    # state rather than a false "all good".
    permissions_synced_at: datetime | None = None
    blocked_features: list[BlockedFeatureOut] = []

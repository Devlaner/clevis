from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from checks.runner import run_all_checks

router = APIRouter()


class AnalyticsInput(BaseModel):
    owner: str
    token: str


@router.post("/overview")
def analytics_overview(payload: AnalyticsInput):
    try:
        report = run_all_checks(owner=payload.owner, token=payload.token)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))

    failed = [c for c in report["checks"] if c["status"] == "fail"]
    score = 100 - int((len(failed) / max(1, len(report["checks"]))) * 100)
    return {
        "owner": payload.owner,
        "score": score,
        "total_checks": len(report["checks"]),
        "failed_checks": len(failed),
        "checks": report["checks"],
    }

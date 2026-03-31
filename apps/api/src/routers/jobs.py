from fastapi import APIRouter

from src.services.jobs import list_jobs

router = APIRouter()


@router.get("")
def jobs():
    return {"jobs": list_jobs()}

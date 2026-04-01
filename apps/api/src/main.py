from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.logging import setup_logging
from src.core.middleware import RequestIdMiddleware
from src.core.storage import init_storage
from src.routers import actions_cache, analytics, auth, health, jobs

setup_logging()
init_storage()

app = FastAPI(title="clevis API", version="0.1.0")
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(actions_cache.router, prefix="/repos", tags=["actions-cache"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])

"""Unit tests (handler) and HTTP integration tests (router mounted on FastAPI)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers.health import healthz, router


def test_healthz_unit_returns_payload():
    assert healthz() == {"status": "ok"}


def test_healthz_integration_http():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

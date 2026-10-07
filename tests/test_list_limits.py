import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import require_reader
from app.routers import dashboard, review


@pytest.fixture
def reader_app():
    app = FastAPI()
    app.state.limiter = dashboard.limiter
    app.dependency_overrides[require_reader] = lambda: None
    app.include_router(dashboard.router)
    app.include_router(review.router)
    return app


def test_every_dashboard_and_review_limit_is_bounded(reader_app):
    schema = reader_app.openapi()
    limit_parameters = [
        parameter
        for path, methods in schema["paths"].items()
        if path.startswith(("/api/v1/dashboard/", "/api/v1/review/"))
        for operation in methods.values()
        for parameter in operation.get("parameters", [])
        if parameter["name"] == "limit"
    ]

    assert len(limit_parameters) == 12
    assert all(parameter["schema"]["minimum"] == 1 for parameter in limit_parameters)
    assert all(parameter["schema"]["maximum"] == 100 for parameter in limit_parameters)


@pytest.mark.parametrize("path", [
    "/api/v1/dashboard/active-campaigns",
    "/api/v1/review/pending-alerts",
])
@pytest.mark.parametrize("limit", ["0", "-1", "101", "999999999"])
def test_out_of_range_list_limit_is_rejected_before_database_access(reader_app, path, limit):
    with TestClient(reader_app) as client:
        response = client.get(path, params={"limit": limit})

    assert response.status_code == 422

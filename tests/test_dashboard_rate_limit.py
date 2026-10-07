from starlette.requests import Request

from app.config import settings
from app.routers.dashboard import dashboard_rate_key


def _request(session: str | None) -> Request:
    headers = []
    if session is not None:
        headers.append((b"cookie", f"{settings.RADAR_SESSION_COOKIE}={session}".encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/dashboard/overview",
        "query_string": b"",
        "headers": headers,
        "client": ("172.20.0.5", 43210),
        "server": ("fastapi_api", 8005),
        "scheme": "http",
    })


def test_dashboard_quota_isolated_by_session_behind_shared_gateway():
    first = dashboard_rate_key(_request("first.signed"))
    second = dashboard_rate_key(_request("second.signed"))

    assert first == dashboard_rate_key(_request("first.signed"))
    assert first != second
    assert "first.signed" not in first
    assert dashboard_rate_key(_request(None)) == "peer:172.20.0.5"

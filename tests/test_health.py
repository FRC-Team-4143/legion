"""Tests for the /health endpoint and the admin dashboard's System Status panel
(services/health.py), which pings Tempus/Munus over the internal Docker network."""
import httpx

from app.config import settings
from app.services import health as health_mod


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def test_health_endpoint_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "legion"}


async def test_check_sibling_apps_reports_not_configured_when_no_public_url():
    # Class defaults leave tempus_public_url / munus_public_url blank.
    results = await health_mod.check_sibling_apps()
    assert {r["name"]: r["status"] for r in results} == {
        "Tempus": "not_configured", "Munus": "not_configured",
    }


async def test_check_sibling_apps_ok(monkeypatch):
    settings.tempus_public_url = "https://tempus.example.org"
    settings.munus_public_url = "https://munus.example.org"

    class _FakeResponse:
        status_code = 200

    async def _fake_get(url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(health_mod._client, "get", _fake_get)

    results = await health_mod.check_sibling_apps()
    by_name = {r["name"]: r for r in results}
    assert by_name["Tempus"]["status"] == "ok"
    assert by_name["Tempus"]["public_url"] == "https://tempus.example.org"
    assert isinstance(by_name["Tempus"]["latency_ms"], int)
    assert by_name["Munus"]["status"] == "ok"


async def test_check_sibling_apps_down_on_connect_error(monkeypatch):
    settings.tempus_public_url = "https://tempus.example.org"
    settings.munus_public_url = ""

    async def _fake_get(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(health_mod._client, "get", _fake_get)

    results = await health_mod.check_sibling_apps()
    by_name = {r["name"]: r for r in results}
    assert by_name["Tempus"]["status"] == "down"
    assert "connection refused" in by_name["Tempus"]["detail"]
    assert by_name["Munus"]["status"] == "not_configured"


async def test_check_sibling_apps_error_status(monkeypatch):
    settings.tempus_public_url = "https://tempus.example.org"
    settings.munus_public_url = ""

    class _FakeResponse:
        status_code = 503

    async def _fake_get(url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(health_mod._client, "get", _fake_get)

    results = await health_mod.check_sibling_apps()
    tempus = next(r for r in results if r["name"] == "Tempus")
    assert tempus["status"] == "error"
    assert tempus["detail"] == "HTTP 503"


async def test_dashboard_renders_system_status(client, monkeypatch):
    async def _fake_check_sibling_apps():
        return [
            {"name": "Tempus", "status": "ok", "latency_ms": 12, "public_url": "https://tempus.example.org"},
            {"name": "Munus", "status": "not_configured", "public_url": ""},
        ]
    monkeypatch.setattr(health_mod, "check_sibling_apps", _fake_check_sibling_apps)

    await _login(client)
    resp = await client.get("/admin")
    assert resp.status_code == 200
    html = resp.text
    assert "System Status" in html
    assert "Tempus" in html and "Munus" in html
    assert "Responded in 12ms" in html
    assert "Not configured" in html
    # The old worthless team/subteam counts are gone from the dashboard.
    assert "Teams</div>" not in html
    assert "Subteams</div>" not in html

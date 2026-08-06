"""Tests for the editable admin Settings page: .env writeback + validation.

The scheduler isn't started under the test transport, so the handler's
``reschedule_all`` call is a safe no-op (``app.state.scheduler`` is absent).
Settings are reset to class defaults after every test by the autouse
``_isolate_settings_from_dotenv`` fixture in conftest.py.

``app.routers.admin`` is deliberately imported lazily inside each test (not at
module level) — importing it freezes its module-level break-glass session
``_signer`` to whatever ``settings.session_secret`` holds *at that moment*. Done
at module level, that import runs during pytest's collection phase, before the
autouse fixture has swapped in the fixed test secret, permanently binding
``_signer`` to the real `.env` secret for the rest of the test session and
breaking every other test that signs an `admin_session` cookie.
"""
from app.config import settings


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


def _form(**overrides):
    """A complete settings form pre-filled from the current singleton."""
    form = {
        "timezone": settings.timezone,
        "backup_time": settings.backup_time,
        "backup_day": settings.backup_day,
        "backup_keep": settings.backup_keep,
    }
    form.update(overrides)
    return form


async def test_settings_post_writes_env_and_updates_singleton(client, tmp_path, monkeypatch):
    from app.routers import admin
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    await _login(client)

    # Known baseline so each POSTed value is an actual change.
    settings.timezone = "America/New_York"
    settings.backup_day = "sun"
    settings.backup_time = "23:30"
    settings.backup_keep = 14

    resp = await client.post("/admin/settings", data=_form(
        timezone="America/Denver",
        backup_day="fri",
        backup_time="02:15",
        backup_keep="21",
    ), follow_redirects=False)

    assert resp.status_code == 303
    assert "error" not in resp.headers.get("location", "")

    # Live singleton updated immediately.
    assert settings.timezone == "America/Denver"
    assert settings.backup_day == "fri"
    assert settings.backup_time == "02:15"
    assert settings.backup_keep == 21

    # Persisted to .env for the next restart.
    written = env_file.read_text()
    assert "TIMEZONE=America/Denver" in written
    assert "BACKUP_DAY=fri" in written
    assert "BACKUP_TIME=02:15" in written
    assert "BACKUP_KEEP=21" in written


async def test_settings_post_rejects_bad_timezone(client, tmp_path, monkeypatch):
    from app.routers import admin
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    await _login(client)
    original_tz = settings.timezone

    resp = await client.post(
        "/admin/settings", data=_form(timezone="Not/AZone"), follow_redirects=False
    )

    assert resp.status_code == 303
    assert "error=" in resp.headers.get("location", "")
    assert settings.timezone == original_tz
    if env_file.exists():
        assert "Not/AZone" not in env_file.read_text()


async def test_settings_post_rejects_bad_day(client, tmp_path, monkeypatch):
    from app.routers import admin
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    await _login(client)
    original_day = settings.backup_day

    resp = await client.post(
        "/admin/settings", data=_form(backup_day="funday"), follow_redirects=False
    )

    assert resp.status_code == 303
    assert "error=" in resp.headers.get("location", "")
    assert settings.backup_day == original_day


async def test_settings_requires_auth(client):
    resp = await client.get("/admin/settings", follow_redirects=False)
    assert resp.status_code in (302, 303)

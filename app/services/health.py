"""Best-effort health pings for the sibling FRC apps, shown on the admin dashboard's
System Status panel — a high-level "is the whole suite up" view, not just Legion.

Pings go over the internal Docker network (the same *_interact_url host Slack
dispatch already relays to — see config.py) since that's reachable regardless of
public DNS/TLS. Whether an app is checked at all is gated on its *_public_url being
set, mirroring services/home.py's own "is this app wired into this deployment" signal
— an app with no public URL configured has no tile on the home page either, and
pinging it would just report "down" for something that was never meant to be here.
"""
import asyncio
import time
from urllib.parse import urlparse

import httpx

from app.config import settings

HEALTH_TIMEOUT = 3.0

# A single long-lived client (reused connections), mirroring routers/slack_dispatch.py's
# module-level `_client`. Tests monkeypatch this instance's `.get` directly rather than
# the `httpx.AsyncClient` class — the test transport's own client is also an
# AsyncClient, so a class-wide patch would intercept that one too.
_client = httpx.AsyncClient(timeout=HEALTH_TIMEOUT)


def _internal_base_url(interact_url: str) -> str:
    """Scheme+host from an *_interact_url setting, e.g.
    "http://tempus:8000/slack/interact" -> "http://tempus:8000"."""
    parsed = urlparse(interact_url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _ping(name: str, internal_url: str, public_url: str) -> dict:
    start = time.monotonic()
    try:
        resp = await _client.get(f"{internal_url}/health")
        latency_ms = round((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return {"name": name, "status": "ok", "latency_ms": latency_ms, "public_url": public_url}
        return {
            "name": name, "status": "error", "latency_ms": latency_ms,
            "detail": f"HTTP {resp.status_code}", "public_url": public_url,
        }
    except httpx.TimeoutException:
        return {"name": name, "status": "down", "detail": "Timed out", "public_url": public_url}
    except httpx.HTTPError as e:
        return {"name": name, "status": "down", "detail": str(e) or type(e).__name__, "public_url": public_url}


async def _check_one(name: str, interact_url: str, public_url: str) -> dict:
    if not public_url:
        return {"name": name, "status": "not_configured", "public_url": ""}
    return await _ping(name, _internal_base_url(interact_url), public_url)


async def check_sibling_apps() -> list[dict]:
    """Ping Tempus & Munus's /health endpoints concurrently. An app whose
    *_public_url isn't configured is reported "not_configured" rather than pinged."""
    apps = [
        ("Tempus", settings.tempus_interact_url, settings.tempus_public_url),
        ("Munus", settings.munus_interact_url, settings.munus_public_url),
    ]
    return list(await asyncio.gather(*(_check_one(*a) for a in apps)))

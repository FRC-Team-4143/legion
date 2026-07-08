"""
Home page — the signed-in member's personalized "app launcher" landing page at
"/". The tile list is computed entirely from the member's mw_sso cookie claims
(groups, role) — no DB query needed.
"""
from app.config import settings


def tiles_for(identity: dict) -> list[dict]:
    """Which MARS/WARS destinations this member's claims qualify them for.

    Each tile: {app, tier, url, icon}. Nothing is shown for an app whose public
    URL isn't configured (settings.tempus_public_url / munus_public_url blank),
    even if the member otherwise holds the matching group — a missing URL would
    otherwise render a broken link.
    """
    groups = set(identity.get("groups") or [])
    role = identity.get("role")
    tiles: list[dict] = []

    if "legion-admin" in groups:
        tiles.append({"app": "Legion", "tier": "Admin", "url": "/admin", "icon": "bi-shield-lock"})
    elif "legion-manager" in groups:
        tiles.append({"app": "Legion", "tier": "Manager", "url": "/admin", "icon": "bi-shield-lock"})

    if settings.tempus_public_url:
        if "tempus-admin" in groups:
            tiles.append({
                "app": "Tempus", "tier": "Admin",
                "url": f"{settings.tempus_public_url}/admin", "icon": "bi-clock-history",
            })
        elif "tempus-manager" in groups:
            tiles.append({
                "app": "Tempus", "tier": "Manager",
                "url": f"{settings.tempus_public_url}/admin", "icon": "bi-clock-history",
            })
        # Unconditional — Tempus's personal page is open to every member (student or
        # mentor), not gated on a role like Munus's student-only tile below.
        tiles.append({
            "app": "Tempus", "tier": "Shop Hours",
            "url": f"{settings.tempus_public_url}/me", "icon": "bi-stopwatch",
        })

    if settings.munus_public_url:
        if "munus-admin" in groups:
            tiles.append({
                "app": "Munus", "tier": "Admin",
                "url": f"{settings.munus_public_url}/admin", "icon": "bi-heart",
            })
        elif "munus-manager" in groups:
            tiles.append({
                "app": "Munus", "tier": "Manager",
                "url": f"{settings.munus_public_url}/admin", "icon": "bi-heart",
            })
        if role == "student":
            tiles.append({
                "app": "Munus", "tier": "Volunteer Hours",
                "url": f"{settings.munus_public_url}/me", "icon": "bi-clipboard-check",
            })

    return tiles

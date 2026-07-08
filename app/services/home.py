"""
Home page — the signed-in member's personalized "app launcher" landing page at
"/". The tile list is computed entirely from the member's mw_sso cookie claims
(groups, role) — no DB query needed.
"""
from app.config import settings

# One icon per app for its staff (admin/manager) tiles, and a separate icon for
# that app's personal-dashboard tile if it has one — a single explicit table
# instead of re-typing a Bootstrap Icons class at each call site below.
_APP_ICONS = {
    "Legion": "bi-shield-lock",
    "Tempus": "bi-clock-history",
    "Munus": "bi-heart",
}
_PERSONAL_ICONS = {
    "Tempus": "bi-stopwatch",
    "Munus": "bi-clipboard-check",
}


def tiles_for(identity: dict) -> list[dict]:
    """Which MARS/WARS destinations this member's claims qualify them for.

    Each tile: {app, tier, url, icon, kind}. `kind` is "staff" (admin/manager
    tiles) or "personal" (a member's own dashboard) — drives the grouping and
    the staff badge on the home page. Nothing is shown for an app whose public
    URL isn't configured (settings.tempus_public_url / munus_public_url blank),
    even if the member otherwise holds the matching group — a missing URL would
    otherwise render a broken link.
    """
    groups = set(identity.get("groups") or [])
    role = identity.get("role")
    tiles: list[dict] = []

    if "legion-admin" in groups:
        tiles.append({"app": "Legion", "tier": "Admin", "url": "/admin", "icon": _APP_ICONS["Legion"], "kind": "staff"})
    elif "legion-manager" in groups:
        tiles.append({"app": "Legion", "tier": "Manager", "url": "/admin", "icon": _APP_ICONS["Legion"], "kind": "staff"})

    if settings.tempus_public_url:
        if "tempus-admin" in groups:
            tiles.append({
                "app": "Tempus", "tier": "Admin",
                "url": f"{settings.tempus_public_url}/admin", "icon": _APP_ICONS["Tempus"], "kind": "staff",
            })
        elif "tempus-manager" in groups:
            tiles.append({
                "app": "Tempus", "tier": "Manager",
                "url": f"{settings.tempus_public_url}/admin", "icon": _APP_ICONS["Tempus"], "kind": "staff",
            })
        # Unconditional — Tempus's personal page is open to every member (student or
        # mentor), not gated on a role like Munus's student-only tile below.
        tiles.append({
            "app": "Tempus", "tier": "Shop Hours",
            "url": f"{settings.tempus_public_url}/me", "icon": _PERSONAL_ICONS["Tempus"], "kind": "personal",
        })

    if settings.munus_public_url:
        if "munus-admin" in groups:
            tiles.append({
                "app": "Munus", "tier": "Admin",
                "url": f"{settings.munus_public_url}/admin", "icon": _APP_ICONS["Munus"], "kind": "staff",
            })
        elif "munus-manager" in groups:
            tiles.append({
                "app": "Munus", "tier": "Manager",
                "url": f"{settings.munus_public_url}/admin", "icon": _APP_ICONS["Munus"], "kind": "staff",
            })
        if role == "student":
            tiles.append({
                "app": "Munus", "tier": "Volunteer Hours",
                "url": f"{settings.munus_public_url}/me", "icon": _PERSONAL_ICONS["Munus"], "kind": "personal",
            })

    return tiles

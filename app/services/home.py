"""
Home page — the signed-in member's personalized "app launcher" landing page at
"/". The tile list is computed entirely from the member's mw_sso cookie claims
(groups, role) — no DB query needed.
"""
from app.config import settings

# One icon per app, shared by its staff (admin/manager) tiles, its personal-dashboard
# tile, and its Slack Commands section — a single explicit table instead of re-typing
# a Bootstrap Icons class at each call site below.
_APP_ICONS = {
    "Legion": "bi-shield-lock",
    "Tempus": "bi-stopwatch",
    "Munus": "bi-heart",
    "Virtus": "bi-flag",
    "Merces": "bi-gift",
    "Colosseum": "bi-clipboard-data",
    "Scriptum": "bi-code-slash",
    "Alumni": "bi-mortarboard",
}

# Each app's registered Slack slash commands, paired with a short description (see each
# app's routers/slack.py for the actual behavior). Shown on every tile for that app
# regardless of tier — Legion has no access to Tempus's/Munus's local Mentor/Student
# tables to filter this by who can actually run which command, so it's a discovery aid,
# not an access gate.
_APP_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "Legion": [
        ("/legion", "Get a one-tap link to Legion"),
    ],
    "Tempus": [
        ("/tempus", "Get a one-tap link to your dashboard"),
        ("/hours", "Check your weekly hours"),
        ("/shop", "See who's currently signed in"),
        ("/edit", "Edit a student's session (mentors)"),
        ("/gtfo", "Signs out all students (mentors)"),
        ("/qr", "Get your kiosk QR badge"),
    ],
    "Virtus": [
        ("/virtus", "See your goals and any reviews you owe"),
    ],
    "Munus": [
        ("/munus", "Get a one-tap link to Munus"),
        ("/vhours", "Check your volunteer hours"),
    ],
    "Merces": [
        ("/merces", "Check your balance and open the rewards store"),
    ],
    "Alumni": [
        ("/alumni", "Get a one-tap link to your survey"),
    ],
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
            "url": f"{settings.tempus_public_url}/me", "icon": _APP_ICONS["Tempus"], "kind": "personal",
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
                "url": f"{settings.munus_public_url}/me", "icon": _APP_ICONS["Munus"], "kind": "personal",
            })
        elif role == "mentor":
            # Mentors have no /me (student-only) — send them to the opportunities list
            # instead, where they can see who's signed up for each shift (read-only).
            tiles.append({
                "app": "Munus", "tier": "Opportunities",
                "url": f"{settings.munus_public_url}/opportunities", "icon": _APP_ICONS["Munus"], "kind": "personal",
            })

    if settings.virtus_public_url:
        if "virtus-admin" in groups:
            tiles.append({
                "app": "Virtus", "tier": "Admin",
                "url": f"{settings.virtus_public_url}/admin", "icon": _APP_ICONS["Virtus"], "kind": "staff",
            })
        elif "virtus-manager" in groups:
            tiles.append({
                "app": "Virtus", "tier": "Manager",
                "url": f"{settings.virtus_public_url}/admin", "icon": _APP_ICONS["Virtus"], "kind": "staff",
            })
        # Unconditional, like Tempus's: Virtus's personal page is open to every member.
        # Mentors need it too — being someone's assigned reviewer is not a Legion group,
        # so a mentor's "reviews I owe" list lives behind this tile, not the Admin one.
        tiles.append({
            "app": "Virtus", "tier": "Goals & Reviews",
            "url": f"{settings.virtus_public_url}/me", "icon": _APP_ICONS["Virtus"], "kind": "personal",
        })

    if settings.merces_public_url:
        if "merces-admin" in groups:
            tiles.append({
                "app": "Merces", "tier": "Admin",
                "url": f"{settings.merces_public_url}/admin", "icon": _APP_ICONS["Merces"], "kind": "staff",
            })
        elif "merces-manager" in groups:
            tiles.append({
                "app": "Merces", "tier": "Manager",
                "url": f"{settings.merces_public_url}/admin", "icon": _APP_ICONS["Merces"], "kind": "staff",
            })
        # Student-only, like Munus's: Merces's personal page is a student's rewards
        # balance + store. Mentors are staff here, not balance holders, so they get the
        # Admin tile (if in a group) and no personal one.
        if role == "student":
            tiles.append({
                "app": "Merces", "tier": "My Balance",
                "url": f"{settings.merces_public_url}/me", "icon": _APP_ICONS["Merces"], "kind": "personal",
            })

    # Colosseum has no admin tier and no groups: a member's team_number is their workspace,
    # and Colosseum refuses anyone without one, so that's the only thing gating the tile.
    if settings.colosseum_public_url and identity.get("team_number"):
        tiles.append({
            "app": "Colosseum", "tier": "Data & Planning",
            "url": settings.colosseum_public_url, "icon": _APP_ICONS["Colosseum"], "kind": "personal",
        })

    # Scriptum is invitation-only: each workspace runs on a paid worker droplet, so a
    # member needs `scriptum-user` (granted by hand; scriptum-admin doesn't imply it) for
    # the Coding tile, and Scriptum itself refuses everyone else.
    if settings.scriptum_public_url:
        if "scriptum-admin" in groups:
            tiles.append({
                "app": "Scriptum", "tier": "Admin",
                "url": f"{settings.scriptum_public_url}/admin/", "icon": _APP_ICONS["Scriptum"], "kind": "staff",
            })
        if "scriptum-user" in groups:
            tiles.append({
                "app": "Scriptum", "tier": "Virtual Coding Workspace",
                "url": f"{settings.scriptum_public_url}/", "icon": _APP_ICONS["Scriptum"], "kind": "personal",
            })

    if settings.alumni_public_url:
        # Staff-only — Alumni has no personal tile here, since the people it's about are
        # by definition no longer active Legion members and never get a signed-in home
        # page to see a tile on in the first place. They reach Alumni via the magic link
        # in their graduation DM instead (see the Alumni app's own services/survey.py).
        if "alumni-admin" in groups:
            tiles.append({
                "app": "Alumni", "tier": "Admin",
                "url": f"{settings.alumni_public_url}/admin", "icon": _APP_ICONS["Alumni"], "kind": "staff",
            })
        elif "alumni-manager" in groups:
            tiles.append({
                "app": "Alumni", "tier": "Manager",
                "url": f"{settings.alumni_public_url}/admin", "icon": _APP_ICONS["Alumni"], "kind": "staff",
            })

    return tiles


def commands_for(tiles: list[dict]) -> list[dict]:
    """One entry per distinct app appearing in `tiles` that has registered Slack
    commands, in first-seen order — feeds the page's own "Slack Commands" reference
    section. Kept separate from the tile grid (rather than attached to each tile)
    so a launcher tile's size doesn't depend on how many commands its app has, and
    so an app with multiple tiles (e.g. both an Admin and a Shop Hours tile) only
    lists its commands once.
    """
    seen_apps: set[str] = set()
    sections: list[dict] = []
    for tile in tiles:
        app = tile["app"]
        if app in seen_apps or app not in _APP_COMMANDS:
            continue
        seen_apps.add(app)
        sections.append({"app": app, "icon": _APP_ICONS[app], "commands": _APP_COMMANDS[app]})
    return sections

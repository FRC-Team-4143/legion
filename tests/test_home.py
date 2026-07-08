"""Legion's signed-in home page ("/"): a role-aware app-launcher computed from the
mw_sso cookie's claims, replacing the old unconditional redirect to /admin."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member
from app.services.home import _APP_COMMANDS, commands_for, tiles_for
from app.services.sso import make_sso_token


def _identity(groups=(), role="mentor"):
    return {"groups": list(groups), "role": role}


# ── tiles_for() — pure logic, no DB/HTTP needed ─────────────────────────────────

def test_no_groups_or_role_yields_no_tiles():
    assert tiles_for(_identity()) == []


def test_legion_admin_tile():
    tiles = tiles_for(_identity(groups=["legion-admin"]))
    assert tiles == [{"app": "Legion", "tier": "Admin", "url": "/admin", "icon": "bi-shield-lock", "kind": "staff"}]


def test_legion_manager_tile_only_without_admin():
    tiles = tiles_for(_identity(groups=["legion-manager"]))
    assert tiles == [{"app": "Legion", "tier": "Manager", "url": "/admin", "icon": "bi-shield-lock", "kind": "staff"}]

    # Holding both: admin takes precedence, only one Legion tile.
    both = tiles_for(_identity(groups=["legion-admin", "legion-manager"]))
    assert len([t for t in both if t["app"] == "Legion"]) == 1
    assert both[0]["tier"] == "Admin"


def test_tempus_tiles_require_configured_public_url():
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = ""
        assert tiles_for(_identity(groups=["tempus-admin"])) == []

        settings.tempus_public_url = "https://tempus.example.org"
        tiles = tiles_for(_identity(groups=["tempus-admin"]))
        assert tiles == [
            {"app": "Tempus", "tier": "Admin", "url": "https://tempus.example.org/admin", "icon": "bi-clock-history", "kind": "staff"},
            {"app": "Tempus", "tier": "Shop Hours", "url": "https://tempus.example.org/me", "icon": "bi-stopwatch", "kind": "personal"},
        ]
    finally:
        settings.tempus_public_url = original


def test_tempus_manager_tile():
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"

        manager_tiles = tiles_for(_identity(groups=["tempus-manager"]))
        assert manager_tiles == [
            {"app": "Tempus", "tier": "Manager", "url": "https://tempus.example.org/admin", "icon": "bi-clock-history", "kind": "staff"},
            {"app": "Tempus", "tier": "Shop Hours", "url": "https://tempus.example.org/me", "icon": "bi-stopwatch", "kind": "personal"},
        ]

        # Holding both: admin takes precedence, only one Admin/Manager tile (plus Shop Hours).
        both = tiles_for(_identity(groups=["tempus-admin", "tempus-manager"]))
        assert [t["tier"] for t in both] == ["Admin", "Shop Hours"]
    finally:
        settings.tempus_public_url = original


def test_tempus_shop_hours_tile_is_unconditional():
    """Unlike Munus's role-gated personal tile, Tempus's Shop Hours tile is open to
    every signed-in member — students and mentors both attend the shop."""
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        for role in ("student", "mentor"):
            tiles = tiles_for(_identity(role=role))
            assert tiles == [{
                "app": "Tempus", "tier": "Shop Hours",
                "url": "https://tempus.example.org/me", "icon": "bi-stopwatch", "kind": "personal",
            }]
    finally:
        settings.tempus_public_url = original


def test_munus_admin_and_manager_tiles():
    original = settings.munus_public_url
    try:
        settings.munus_public_url = "https://munus.example.org"

        admin_tiles = tiles_for(_identity(groups=["munus-admin"]))
        assert admin_tiles == [{
            "app": "Munus", "tier": "Admin",
            "url": "https://munus.example.org/admin", "icon": "bi-heart", "kind": "staff",
        }]

        manager_tiles = tiles_for(_identity(groups=["munus-manager"]))
        assert manager_tiles == [{
            "app": "Munus", "tier": "Manager",
            "url": "https://munus.example.org/admin", "icon": "bi-heart", "kind": "staff",
        }]
    finally:
        settings.munus_public_url = original


def test_munus_student_portal_tile():
    original = settings.munus_public_url
    try:
        settings.munus_public_url = ""
        assert tiles_for(_identity(role="student")) == []

        settings.munus_public_url = "https://munus.example.org"
        tiles = tiles_for(_identity(role="student"))
        assert tiles == [{
            "app": "Munus", "tier": "Volunteer Hours",
            "url": "https://munus.example.org/me", "icon": "bi-clipboard-check", "kind": "personal",
        }]

        # A student who's also a munus-manager gets both tiles independently.
        both = tiles_for(_identity(groups=["munus-manager"], role="student"))
        assert {t["tier"] for t in both if t["app"] == "Munus"} == {"Manager", "Volunteer Hours"}
    finally:
        settings.munus_public_url = original


def test_all_three_apps_together():
    original = (settings.tempus_public_url, settings.munus_public_url)
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        settings.munus_public_url = "https://munus.example.org"
        tiles = tiles_for(_identity(groups=["legion-admin", "tempus-admin", "munus-admin"]))
        # Tempus now yields two tiles (Admin + the unconditional Shop Hours tile).
        assert [t["app"] for t in tiles] == ["Legion", "Tempus", "Tempus", "Munus"]
    finally:
        settings.tempus_public_url, settings.munus_public_url = original


def test_tiles_grouped_by_kind():
    """A member with both a staff group and a personal-tile-qualifying role gets
    tiles tagged for both groupings (drives the "Your Apps" / "Admin Tools"
    sections on the home page)."""
    original = (settings.tempus_public_url, settings.munus_public_url)
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        settings.munus_public_url = "https://munus.example.org"
        tiles = tiles_for(_identity(groups=["tempus-admin"], role="student"))
        staff = [t for t in tiles if t["kind"] == "staff"]
        personal = [t for t in tiles if t["kind"] == "personal"]
        assert [t["app"] for t in staff] == ["Tempus"]
        assert {t["app"] for t in personal} == {"Tempus", "Munus"}
    finally:
        settings.tempus_public_url, settings.munus_public_url = original


# ── commands_for() — the separate "Slack Commands" reference section ───────────

def test_app_commands_content():
    """Guards the actual command/description text, not just the wiring."""
    tempus_slugs = [cmd for cmd, _ in _APP_COMMANDS["Tempus"]]
    assert tempus_slugs == ["/hours", "/shop", "/edit", "/qr"]
    assert _APP_COMMANDS["Munus"] == [("/vhours", "Check your volunteer hours")]
    assert "Legion" not in _APP_COMMANDS


def test_commands_for_no_tiles_is_empty():
    assert commands_for([]) == []


def test_commands_for_skips_apps_with_no_commands():
    """Legion has no registered Slack commands, so a Legion-only tile list yields no
    section at all."""
    tiles = tiles_for(_identity(groups=["legion-admin"]))
    assert commands_for(tiles) == []


def test_commands_for_lists_each_app_once():
    """Tempus contributes two tiles (Admin + Shop Hours) for this identity — its
    commands must appear exactly once, not once per tile."""
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        tiles = tiles_for(_identity(groups=["tempus-admin"]))
        sections = commands_for(tiles)
        assert [s["app"] for s in sections] == ["Tempus"]
        assert sections[0]["commands"] == _APP_COMMANDS["Tempus"]
        assert sections[0]["icon"] == "bi-clock-history"
    finally:
        settings.tempus_public_url = original


def test_commands_for_multiple_apps_in_first_seen_order():
    original = (settings.tempus_public_url, settings.munus_public_url)
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        settings.munus_public_url = "https://munus.example.org"
        tiles = tiles_for(_identity(groups=["legion-admin", "tempus-admin", "munus-admin"]))
        sections = commands_for(tiles)
        assert [s["app"] for s in sections] == ["Tempus", "Munus"]
    finally:
        settings.tempus_public_url, settings.munus_public_url = original


# ── GET / — route-level behavior ────────────────────────────────────────────────

async def test_root_without_cookie_redirects_to_sso_authorize(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/authorize?app=legion&return_to=%2F"


async def test_root_with_valid_cookie_renders_home_with_tiles(client, db, make_member):
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        member = await make_member(name="Ada Admin", groups=["tempus-admin"])
        loaded = (
            await db.execute(
                select(Member).options(selectinload(Member.team), selectinload(Member.groups))
                .where(Member.id == member.id)
            )
        ).scalars().first()
        client.cookies.set("mw_sso", make_sso_token(loaded))

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Hi, Ada Admin!" in resp.text
        assert "https://tempus.example.org/admin" in resp.text
    finally:
        settings.tempus_public_url = original


async def test_root_shows_tempus_commands_once_in_their_own_section(client, db, make_member):
    """Ada holds both tempus-admin (an Admin tile) and gets the unconditional Shop
    Hours tile — Tempus's commands must render exactly once, in the dedicated
    section, not duplicated per tile."""
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        member = await make_member(name="Ada Admin", groups=["tempus-admin"])
        loaded = (
            await db.execute(
                select(Member).options(selectinload(Member.team), selectinload(Member.groups))
                .where(Member.id == member.id)
            )
        ).scalars().first()
        client.cookies.set("mw_sso", make_sso_token(loaded))

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Slack Commands" in resp.text
        assert resp.text.count("Check your weekly hours") == 1
        assert "/shop" in resp.text
    finally:
        settings.tempus_public_url = original


async def test_root_shows_no_commands_section_for_legion_only_tile(client, db, make_member):
    member = await make_member(name="Legion Admin", groups=["legion-admin"])
    loaded = (
        await db.execute(
            select(Member).options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(loaded))

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Slack Commands" not in resp.text


async def test_root_with_no_matching_groups_shows_empty_state(client, db, make_member):
    member = await make_member(name="Regular Member")
    loaded = (
        await db.execute(
            select(Member).options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(loaded))

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "No apps assigned yet" in resp.text


async def test_root_shows_both_section_headings_when_applicable(client, db, make_member):
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        member = await make_member(name="Ada Admin", groups=["tempus-admin"])
        loaded = (
            await db.execute(
                select(Member).options(selectinload(Member.team), selectinload(Member.groups))
                .where(Member.id == member.id)
            )
        ).scalars().first()
        client.cookies.set("mw_sso", make_sso_token(loaded))

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Your Apps" in resp.text
        assert "Admin Tools" in resp.text
        assert "STAFF" in resp.text
    finally:
        settings.tempus_public_url = original


async def test_root_shows_only_your_apps_for_plain_member(client, db, make_member):
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"
        member = await make_member(name="Plain Member")
        loaded = (
            await db.execute(
                select(Member).options(selectinload(Member.team), selectinload(Member.groups))
                .where(Member.id == member.id)
            )
        ).scalars().first()
        client.cookies.set("mw_sso", make_sso_token(loaded))

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Your Apps" in resp.text
        assert "Admin Tools" not in resp.text
    finally:
        settings.tempus_public_url = original

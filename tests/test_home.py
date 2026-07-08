"""Legion's signed-in home page ("/"): a role-aware app-launcher computed from the
mw_sso cookie's claims, replacing the old unconditional redirect to /admin."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member
from app.services.home import tiles_for
from app.services.sso import make_sso_token


def _identity(groups=(), role="mentor"):
    return {"groups": list(groups), "role": role}


# ── tiles_for() — pure logic, no DB/HTTP needed ─────────────────────────────────

def test_no_groups_or_role_yields_no_tiles():
    assert tiles_for(_identity()) == []


def test_legion_admin_tile():
    tiles = tiles_for(_identity(groups=["legion-admin"]))
    assert tiles == [{"app": "Legion", "tier": "Admin", "url": "/admin", "icon": "bi-shield-lock"}]


def test_legion_manager_tile_only_without_admin():
    tiles = tiles_for(_identity(groups=["legion-manager"]))
    assert tiles == [{"app": "Legion", "tier": "Manager", "url": "/admin", "icon": "bi-shield-lock"}]

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
            {"app": "Tempus", "tier": "Admin", "url": "https://tempus.example.org/admin", "icon": "bi-clock-history"},
            {"app": "Tempus", "tier": "Shop Hours", "url": "https://tempus.example.org/me", "icon": "bi-stopwatch"},
        ]
    finally:
        settings.tempus_public_url = original


def test_tempus_manager_tile():
    original = settings.tempus_public_url
    try:
        settings.tempus_public_url = "https://tempus.example.org"

        manager_tiles = tiles_for(_identity(groups=["tempus-manager"]))
        assert manager_tiles == [
            {"app": "Tempus", "tier": "Manager", "url": "https://tempus.example.org/admin", "icon": "bi-clock-history"},
            {"app": "Tempus", "tier": "Shop Hours", "url": "https://tempus.example.org/me", "icon": "bi-stopwatch"},
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
                "url": "https://tempus.example.org/me", "icon": "bi-stopwatch",
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
            "url": "https://munus.example.org/admin", "icon": "bi-heart",
        }]

        manager_tiles = tiles_for(_identity(groups=["munus-manager"]))
        assert manager_tiles == [{
            "app": "Munus", "tier": "Manager",
            "url": "https://munus.example.org/admin", "icon": "bi-heart",
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
            "url": "https://munus.example.org/me", "icon": "bi-clipboard-check",
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

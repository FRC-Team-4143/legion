"""User groups: seeding, M2M assignment, serialization, the SSO claim, the read API,
admin CRUD (create / rename / archive / purge), and managing membership from the group's
own page (not the member create/edit forms — see test_member_forms_have_no_group_controls)."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Group, Member
from app.services.members import serialize_member
from app.services.sso import make_sso_token, read_sso_token


async def _login(client):
    # Test admin_password is fixed in conftest.py's _isolate_settings_from_dotenv.
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _group(db, slug):
    db.expire_all()  # drop cached state so we read what an HTTP request committed
    return (await db.execute(select(Group).where(Group.slug == slug))).scalars().first()


async def _member(db, member_id):
    db.expire_all()
    return (
        await db.execute(
            select(Member)
            .options(
                selectinload(Member.team),
                selectinload(Member.subteam),
                selectinload(Member.groups),
            )
            .where(Member.id == member_id)
        )
    ).scalars().first()


# ── Seeding ──────────────────────────────────────────────────────────────────

async def test_default_groups_seeded(db):
    slugs = set((await db.execute(select(Group.slug))).scalars().all())
    assert {
        "legion-admin", "legion-manager", "tempus-admin", "munus-admin", "munus-manager",
    } <= slugs


# ── Model + serialization ────────────────────────────────────────────────────

async def test_member_groups_roundtrip_and_serialize(db, make_member):
    m = await make_member(name="Ada Lovelace", groups=["munus-admin", "legion-admin"])
    loaded = await _member(db, m.id)
    assert {g.slug for g in loaded.groups} == {"munus-admin", "legion-admin"}
    assert set(serialize_member(loaded)["groups"]) == {"munus-admin", "legion-admin"}


async def test_member_without_groups_serializes_empty(db, make_member):
    m = await make_member(name="No Groups")
    assert serialize_member(await _member(db, m.id))["groups"] == []


# ── SSO token ────────────────────────────────────────────────────────────────

async def test_sso_token_carries_groups_and_drops_is_admin(db, make_member):
    m = await make_member(name="Ada Lovelace", groups=["legion-admin"])
    claims = read_sso_token(make_sso_token(await _member(db, m.id)))
    assert claims["groups"] == ["legion-admin"]
    assert "is_admin" not in claims


# ── Read API ─────────────────────────────────────────────────────────────────

async def test_api_members_includes_group_slugs(client, api_key, make_member):
    await make_member(name="Ada Lovelace", groups=["munus-admin"])
    resp = await client.get("/api/members", headers={"X-API-Key": api_key})
    ada = next(m for m in resp.json()["members"] if m["name"] == "Ada Lovelace")
    assert ada["groups"] == ["munus-admin"]


async def test_api_groups_endpoint(client, api_key):
    resp = await client.get("/api/groups", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    by_slug = {g["slug"]: g for g in resp.json()["groups"]}
    assert by_slug["legion-admin"]["label"] == "Legion Admin"
    assert by_slug["legion-admin"]["is_active"] is True


async def test_api_groups_requires_key(client):
    assert (await client.get("/api/groups")).status_code == 503


# ── Admin CRUD ───────────────────────────────────────────────────────────────

async def test_admin_groups_page_renders(client):
    await _login(client)
    resp = await client.get("/admin/groups")
    assert resp.status_code == 200
    assert "Legion Admin" in resp.text


async def test_admin_create_group_derives_slug(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/groups", data={"label": "Ops Lead"}, follow_redirects=False
    )
    assert resp.status_code == 303
    g = await _group(db, "ops-lead")
    assert g is not None and g.label == "Ops Lead"


async def test_admin_create_group_rejects_duplicate_slug(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/groups", data={"label": "Legion Admin", "slug": "legion-admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


async def test_admin_edit_group_renames_label_keeps_slug(client, db):
    await _login(client)
    grp = await _group(db, "munus-manager")
    resp = await client.post(
        f"/admin/groups/{grp.id}/edit", data={"label": "Volunteer Manager"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = await _group(db, "munus-manager")
    assert updated.label == "Volunteer Manager"
    assert updated.slug == "munus-manager"  # slug is immutable for the apps' checks


async def test_admin_archive_group_keeps_existing_membership(client, db, make_member):
    m = await make_member(name="Ada Lovelace", groups=["munus-admin"])
    mid = m.id  # capture before _group()'s expire_all() expires the instance
    await _login(client)
    grp = await _group(db, "munus-admin")
    resp = await client.post(f"/admin/groups/{grp.id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    assert (await _group(db, "munus-admin")).is_active is False
    # Archiving only hides it from new assignment — the member keeps it (and the API/token
    # still emit it).
    loaded = await _member(db, mid)
    assert "munus-admin" in {g.slug for g in loaded.groups}


async def test_admin_purge_requires_archived_first(client, db):
    """An active group can't be purged — it has to be archived first."""
    await _login(client)
    grp = await _group(db, "munus-admin")
    resp = await client.post(f"/admin/groups/{grp.id}/purge", follow_redirects=False)
    assert resp.status_code == 303
    assert await _group(db, "munus-admin") is not None  # still there, untouched


async def test_admin_purge_group_deletes_it_and_clears_membership(client, db, make_member):
    m = await make_member(name="Ada Lovelace", groups=["munus-admin"])
    mid = m.id  # capture before _group()'s expire_all() expires the instance
    await _login(client)
    grp = await _group(db, "munus-admin")
    grp_id = grp.id

    await client.post(f"/admin/groups/{grp_id}/toggle", follow_redirects=False)  # archive first
    resp = await client.post(f"/admin/groups/{grp_id}/purge", follow_redirects=False)
    assert resp.status_code == 303

    assert await _group(db, "munus-admin") is None
    # The member is untouched — just no longer holds the deleted group.
    loaded = await _member(db, mid)
    assert loaded is not None
    assert "munus-admin" not in {g.slug for g in loaded.groups}


# ── Managing membership from the group's own page ─────────────────────────────

async def test_member_forms_have_no_group_controls(client, db, make_member):
    """Group membership is only managed from /admin/groups/{id} now — the member
    create/edit forms shouldn't render group checkboxes or accept group_ids."""
    m = await make_member(name="Grace Hopper", groups=["legion-admin"])
    await _login(client)

    create_page = await client.get("/admin/members")
    assert "group_ids" not in create_page.text

    edit_page = await client.get(f"/admin/members/{m.id}/edit")
    assert "group_ids" not in edit_page.text

    # Even if a client forged the old field name, it's simply ignored.
    await client.post(
        f"/admin/members/{m.id}/edit",
        data={"name": "Grace Hopper", "role": "student", "group_ids": ["1"]},
        follow_redirects=False,
    )
    assert {g.slug for g in (await _member(db, m.id)).groups} == {"legion-admin"}


async def test_group_detail_page_lists_members_and_addable_members(client, db, make_member):
    await make_member(name="Ada Lovelace", groups=["munus-admin"])
    await make_member(name="Grace Hopper")  # not in the group -> addable
    await _login(client)
    grp = await _group(db, "munus-admin")

    resp = await client.get(f"/admin/groups/{grp.id}")
    assert resp.status_code == 200
    assert "Ada Lovelace" in resp.text
    assert "Grace Hopper" in resp.text  # offered in the "add a member" select


async def test_group_add_member(client, db, make_member):
    m = await make_member(name="New Person")
    mid = m.id  # capture before _group()'s expire_all() expires the instance
    await _login(client)
    grp = await _group(db, "tempus-admin")

    resp = await client.post(
        f"/admin/groups/{grp.id}/members", data={"member_id": mid}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert {g.slug for g in (await _member(db, mid)).groups} == {"tempus-admin"}


async def test_group_remove_member(client, db, make_member):
    m = await make_member(name="Grace Hopper", groups=["munus-admin", "munus-manager"])
    mid = m.id  # capture before _group()'s expire_all() expires the instance
    await _login(client)
    grp = await _group(db, "munus-admin")

    resp = await client.post(
        f"/admin/groups/{grp.id}/members/{mid}/remove", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert {g.slug for g in (await _member(db, mid)).groups} == {"munus-manager"}


async def test_group_add_member_is_idempotent(client, db, make_member):
    """Posting the same member twice doesn't duplicate the association row."""
    m = await make_member(name="New Person")
    mid = m.id  # capture before _group()'s expire_all() expires the instance
    await _login(client)
    grp = await _group(db, "tempus-admin")

    for _ in range(2):
        await client.post(
            f"/admin/groups/{grp.id}/members", data={"member_id": mid}, follow_redirects=False,
        )
    assert [g.slug for g in (await _member(db, mid)).groups] == ["tempus-admin"]


# ── updated_at must bump on pure group-membership changes ──────────────────────
# Regression tests: sibling apps' roster sync is incremental (?updated_since=), keyed
# off Member.updated_at. Modifying the member_user_groups association table alone
# doesn't trigger SQLAlchemy's onupdate on `members` — a group add/remove/purge must
# bump updated_at explicitly, or a member whose *only* change was a group assignment
# is invisible to every future sync.

async def _backdate(db, member_id):
    m = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    m.updated_at = datetime.utcnow() - timedelta(days=1)
    await db.commit()


async def test_group_add_member_bumps_updated_at(client, db, make_member):
    m = await make_member(name="New Person")
    mid = m.id
    await _backdate(db, mid)
    await _login(client)
    grp = await _group(db, "tempus-admin")

    await client.post(f"/admin/groups/{grp.id}/members", data={"member_id": mid}, follow_redirects=False)

    db.expire_all()
    updated = (await db.execute(select(Member).where(Member.id == mid))).scalars().first()
    assert updated.updated_at > datetime.utcnow() - timedelta(minutes=1)


async def test_group_remove_member_bumps_updated_at(client, db, make_member):
    m = await make_member(name="Grace Hopper", groups=["munus-admin"])
    mid = m.id
    await _backdate(db, mid)
    await _login(client)
    grp = await _group(db, "munus-admin")

    await client.post(f"/admin/groups/{grp.id}/members/{mid}/remove", follow_redirects=False)

    db.expire_all()
    updated = (await db.execute(select(Member).where(Member.id == mid))).scalars().first()
    assert updated.updated_at > datetime.utcnow() - timedelta(minutes=1)


async def test_admin_purge_group_bumps_member_updated_at(client, db, make_member):
    m = await make_member(name="Ada Lovelace", groups=["munus-admin"])
    mid = m.id
    await _backdate(db, mid)
    await _login(client)
    grp = await _group(db, "munus-admin")
    grp_id = grp.id

    await client.post(f"/admin/groups/{grp_id}/toggle", follow_redirects=False)  # archive first
    await client.post(f"/admin/groups/{grp_id}/purge", follow_redirects=False)

    db.expire_all()
    updated = (await db.execute(select(Member).where(Member.id == mid))).scalars().first()
    assert updated.updated_at > datetime.utcnow() - timedelta(minutes=1)

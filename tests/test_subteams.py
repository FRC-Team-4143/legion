"""Admin CRUD for subteams: create / rename / archive / restore / purge. Mirrors the
groups admin tests — same lookup-table pattern (models.py's Subteam)."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member, Subteam


async def _login(client):
    # Test admin_password is fixed in conftest.py's _isolate_settings_from_dotenv.
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _subteam(db, slug):
    db.expire_all()  # drop cached state so we read what an HTTP request committed
    return (await db.execute(select(Subteam).where(Subteam.slug == slug))).scalars().first()


async def _member(db, member_id):
    db.expire_all()
    return (
        await db.execute(
            select(Member).options(selectinload(Member.subteam)).where(Member.id == member_id)
        )
    ).scalars().first()


async def test_admin_create_subteam_derives_slug(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/subteams", data={"label": "Marketing"}, follow_redirects=False
    )
    assert resp.status_code == 303
    st = await _subteam(db, "marketing")
    assert st is not None and st.label == "Marketing"


async def test_admin_create_subteam_rejects_duplicate_slug(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/subteams", data={"label": "Software", "slug": "software"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


async def test_admin_edit_subteam_renames_label_keeps_slug(client, db):
    await _login(client)
    st = await _subteam(db, "design")
    resp = await client.post(
        f"/admin/subteams/{st.id}/edit", data={"label": "Design & UX"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = await _subteam(db, "design")
    assert updated.label == "Design & UX"
    assert updated.slug == "design"


async def test_admin_archive_subteam_keeps_existing_assignment(client, db, make_member):
    m = await make_member(name="Ada Lovelace", subteam_slug="business")
    mid = m.id  # capture before _subteam()'s expire_all() expires the instance
    await _login(client)
    st = await _subteam(db, "business")
    resp = await client.post(f"/admin/subteams/{st.id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    assert (await _subteam(db, "business")).is_active is False
    # Archiving only hides it from new assignment — the member keeps it.
    assert (await _member(db, mid)).subteam.slug == "business"


async def test_admin_purge_requires_archived_first(client, db):
    await _login(client)
    st = await _subteam(db, "business")
    resp = await client.post(f"/admin/subteams/{st.id}/purge", follow_redirects=False)
    assert resp.status_code == 303
    assert await _subteam(db, "business") is not None  # still there, untouched


async def test_admin_purge_subteam_deletes_it_and_clears_member_assignment(client, db, make_member):
    m = await make_member(name="Ada Lovelace", subteam_slug="business")
    mid = m.id  # capture before _subteam()'s expire_all() expires the instance
    await _login(client)
    st = await _subteam(db, "business")
    st_id = st.id

    await client.post(f"/admin/subteams/{st_id}/toggle", follow_redirects=False)  # archive first
    resp = await client.post(f"/admin/subteams/{st_id}/purge", follow_redirects=False)
    assert resp.status_code == 303

    assert await _subteam(db, "business") is None
    # The member is untouched — just detached from the deleted subteam, not deleted itself.
    loaded = await _member(db, mid)
    assert loaded is not None
    assert loaded.subteam is None

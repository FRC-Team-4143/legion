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


# ── List page counts: a column per team x role ─────────────────────────────────

def _row_html(html, slug):
    """The <tr> chunk of the subteams table for the row with this slug."""
    return next(c for c in html.split("<tr") if f">{slug}</code>" in c)


def _count_cells(html, slug):
    """That row's ordered per-team-per-role count cells (4143 students, 4143 mentors,
    4423 students, 4423 mentors, then the No Team pair when it's shown)."""
    import re
    return [int(n) for n in re.findall(r'class="text-end[^"]*">(\d+)</td>', _row_html(html, slug))]


def _total(html, slug):
    import re
    return int(re.search(r'badge bg-secondary">(\d+)<', _row_html(html, slug)).group(1))


async def test_subteams_list_splits_counts_by_team_and_role(client, make_member):
    from app.models import MemberRole

    await make_member(name="Ada Lovelace", team_number=4143, subteam_slug="software")
    await make_member(name="Grace Hopper", team_number=4143, subteam_slug="software")
    await make_member(name="Alan Turing", team_number=4143, subteam_slug="software",
                      role=MemberRole.mentor)
    await make_member(name="Katherine Johnson", team_number=4423, subteam_slug="software")
    # Archived members don't count toward anything.
    await make_member(name="Mary Jackson", team_number=4423, subteam_slug="software",
                      is_active=False)
    await _login(client)

    resp = await client.get("/admin/subteams")
    assert resp.status_code == 200
    # Grouped header: a team number spanning its own Students / Mentors pair.
    assert '<th colspan="2" class="text-center border-start">4143</th>' in resp.text
    assert '<th colspan="2" class="text-center border-start">4423</th>' in resp.text
    assert _count_cells(resp.text, "software") == [2, 1, 1, 0]
    assert _total(resp.text, "software") == 4


async def test_subteams_list_shows_zeros_for_empty_subteam(client, make_member):
    await make_member(name="Ada Lovelace", team_number=4143, subteam_slug="software")
    await _login(client)

    resp = await client.get("/admin/subteams")
    # "business" has no members at all but still gets a full row of zeros.
    assert _count_cells(resp.text, "business") == [0, 0, 0, 0]
    assert _total(resp.text, "business") == 0


async def test_subteams_list_no_team_columns_only_when_needed(client, make_member):
    await make_member(name="Ada Lovelace", team_number=4143, subteam_slug="software")
    await _login(client)

    resp = await client.get("/admin/subteams")
    assert "No Team" not in resp.text

    # A member with no team assigned makes the pair appear — and still counts in Total.
    await make_member(name="Grace Hopper", team_number=None, subteam_slug="software")
    resp = await client.get("/admin/subteams")
    assert "No Team" in resp.text
    assert _count_cells(resp.text, "software") == [1, 0, 0, 0, 1, 0]
    assert _total(resp.text, "software") == 2

"""The `legion-manager` group: routine roster upkeep (dashboard, member list/create/
edit/regenerate-username) but nothing security-sensitive — no groups, teams, subteams,
CSV import, API info, audit log, backup, or destructive/bulk member actions. Those stay
`legion-admin`-only (see `_require_auth` vs `_require_staff` in routers/admin.py)."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Group, Member
from app.services.sso import make_sso_token


async def _as_manager(client, db, make_member, name="Manager Mel"):
    member = await make_member(name=name, groups=["legion-manager"])
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(loaded))
    return loaded


async def test_manager_can_reach_dashboard_and_members(client, db, make_member):
    await _as_manager(client, db, make_member)
    assert (await client.get("/admin")).status_code == 200
    assert (await client.get("/admin/members")).status_code == 200


async def test_manager_can_create_and_edit_members(client, db, make_member):
    await _as_manager(client, db, make_member)
    resp = await client.post(
        "/admin/members", data={"name": "New Person", "role": "student"}, follow_redirects=False,
    )
    assert resp.status_code == 303

    created = (
        await db.execute(select(Member).where(Member.name == "New Person"))
    ).scalars().first()
    resp = await client.get(f"/admin/members/{created.id}/edit")
    assert resp.status_code == 200
    resp = await client.post(
        f"/admin/members/{created.id}/edit",
        data={"name": "New Person", "role": "student"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = await client.post(
        f"/admin/members/{created.id}/regenerate-username", follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_manager_cannot_manage_groups(client, db, make_member):
    await _as_manager(client, db, make_member)
    assert (await client.get("/admin/groups")).status_code == 403
    assert (await client.post("/admin/groups", data={"label": "New Group"})).status_code == 403


async def test_manager_denied_page_stays_in_admin_shell(client, db, make_member):
    """Regression test: denial used to render a bare standalone document with no
    sidebar — a dead end. It now stays inside the admin shell (full sidebar still
    there and clickable) with a blurred placeholder + "No Access" badge naming the
    section, so a manager can just click elsewhere instead of hitting a wall."""
    await _as_manager(client, db, make_member)
    resp = await client.get("/admin/teams")
    assert resp.status_code == 403
    assert 'href="/admin/members"' in resp.text  # sidebar still rendered
    assert 'href="/admin/groups"' in resp.text
    assert "No Access" in resp.text
    assert "Teams" in resp.text


async def test_fully_unauthorized_member_gets_same_shell_wrapped_page(client, db, make_member):
    member = await make_member(name="Regular Member", groups=[])
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(loaded))

    resp = await client.get("/admin")
    assert resp.status_code == 403
    assert 'href="/admin/members"' in resp.text
    assert "No Access" in resp.text
    assert "Dashboard" in resp.text


async def test_manager_cannot_view_or_edit_group_membership(client, db, make_member):
    await _as_manager(client, db, make_member)
    other = await make_member(name="Regular Member")
    tempus_admin = (
        await db.execute(select(Group).where(Group.slug == "tempus-admin"))
    ).scalars().first()

    assert (await client.get(f"/admin/groups/{tempus_admin.id}")).status_code == 403
    resp = await client.post(
        f"/admin/groups/{tempus_admin.id}/members", data={"member_id": other.id},
    )
    assert resp.status_code == 403


async def test_manager_cannot_manage_teams_or_subteams(client, db, make_member):
    await _as_manager(client, db, make_member)
    assert (await client.get("/admin/teams")).status_code == 403
    assert (await client.get("/admin/subteams")).status_code == 403


async def test_manager_cannot_import_csv(client, db, make_member):
    await _as_manager(client, db, make_member)
    assert (await client.get("/admin/import")).status_code == 403


async def test_manager_cannot_view_api_audit_or_backup(client, db, make_member):
    await _as_manager(client, db, make_member)
    assert (await client.get("/admin/api")).status_code == 403
    assert (await client.get("/admin/audit")).status_code == 403
    assert (await client.get("/admin/backup")).status_code == 403


async def test_manager_cannot_delete_restore_purge_or_bulk_actions(client, db, make_member):
    await _as_manager(client, db, make_member)
    target = await make_member(name="Target Member")
    assert (await client.post(f"/admin/members/{target.id}/delete")).status_code == 403
    assert (await client.post(f"/admin/members/{target.id}/restore")).status_code == 403
    assert (await client.post(f"/admin/members/{target.id}/purge")).status_code == 403
    assert (await client.post("/admin/members/bump-grades")).status_code == 403
    assert (await client.post("/admin/members/sync-slack")).status_code == 403


async def test_manager_can_create_member_with_slack_id(client, db, make_member):
    """Linking Slack on a brand-new (unprivileged) member is routine onboarding, not
    an escalation vector — still fine for a manager."""
    await _as_manager(client, db, make_member)
    resp = await client.post(
        "/admin/members",
        data={"name": "New Person", "role": "student", "slack_user_id": "U0NEW"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    created = (await db.execute(select(Member).where(Member.name == "New Person"))).scalars().first()
    assert created.slack_user_id == "U0NEW"


async def test_manager_cannot_reassign_existing_members_slack_id(client, db, make_member):
    """Regression test: a manager must not be able to re-point another member's
    slack_user_id — doing so to a privileged member would let them self-approve the
    SSO Slack push and mint a cookie carrying that member's groups."""
    await _as_manager(client, db, make_member)
    target = await make_member(name="Target Admin", groups=["legion-admin"])
    target_id = target.id

    resp = await client.post(
        f"/admin/members/{target_id}/edit",
        data={"name": "Target Admin", "role": "student", "slack_user_id": "U0ATTACKER"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db.expire_all()
    reloaded = (await db.execute(select(Member).where(Member.id == target_id))).scalars().first()
    assert reloaded.slack_user_id is None

    # Editing other fields without touching slack_user_id is still fine for a manager.
    resp = await client.post(
        f"/admin/members/{target_id}/edit",
        data={"name": "Target Admin Renamed", "role": "student"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    reloaded = (await db.execute(select(Member).where(Member.id == target_id))).scalars().first()
    assert reloaded.name == "Target Admin Renamed"
    assert reloaded.slack_user_id is None


async def test_legion_admin_still_has_full_access(client, db, make_member):
    """Sanity check the split didn't accidentally narrow the admin tier too."""
    member = await make_member(name="Ada Admin", groups=["legion-admin"])
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(loaded))

    assert (await client.get("/admin/groups")).status_code == 200
    assert (await client.get("/admin/teams")).status_code == 200
    assert (await client.get("/admin/import")).status_code == 200

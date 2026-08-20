"""years_on_team: the yearly auto-increment scheduler job, admin form persistence, CSV
import, and API serialization."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import AuditLog, Member, MemberRole
from app.services.members import serialize_member
from app.services.scheduler import job_yearly_tenure_increment


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _get(db, name):
    db.expire_all()  # drop cached state so we read what the job/route committed
    return (await db.execute(select(Member).where(Member.name == name))).scalars().first()


async def _loaded(db, name):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam), selectinload(Member.groups))
            .where(Member.name == name)
        )
    ).scalars().first()


def _csv_upload(text: str):
    return {"file": ("members.csv", text.encode("utf-8"), "text/csv")}


async def test_yearly_increment_bumps_active_members_of_either_role(client, db, make_member):
    await make_member(name="Active Student", role=MemberRole.student, years_on_team=2)
    await make_member(name="Active Mentor", role=MemberRole.mentor, years_on_team=5)
    await make_member(name="Archived Member", is_active=False, years_on_team=3)

    await job_yearly_tenure_increment()

    assert (await _get(db, "Active Student")).years_on_team == 3
    assert (await _get(db, "Active Mentor")).years_on_team == 6
    assert (await _get(db, "Archived Member")).years_on_team == 3  # untouched


async def test_yearly_increment_writes_audit_log(client, db, make_member):
    await make_member(name="Someone", years_on_team=0)

    await job_yearly_tenure_increment()

    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "member.yearly_tenure_increment"))
    ).scalars().first()
    assert log is not None
    assert log.actor == "system"


async def test_yearly_increment_noop_when_no_active_members(db, make_member):
    await make_member(name="Archived Only", is_active=False, years_on_team=1)

    await job_yearly_tenure_increment()  # must not raise

    assert (await _get(db, "Archived Only")).years_on_team == 1
    count = (
        await db.execute(select(AuditLog).where(AuditLog.action == "member.yearly_tenure_increment"))
    ).scalars().all()
    assert count == []  # no rows changed -> nothing logged


async def test_serialize_member_exposes_years_on_team(db, make_member):
    await make_member(name="Vet Vera", role=MemberRole.mentor, years_on_team=7)
    data = serialize_member(await _loaded(db, "Vet Vera"))
    assert data["years_on_team"] == 7


async def test_serialize_member_years_on_team_defaults_to_zero(db, make_member):
    await make_member(name="New Newton")
    data = serialize_member(await _loaded(db, "New Newton"))
    assert data["years_on_team"] == 0


async def test_create_member_with_years_on_team(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/members",
        data={"name": "Backfilled Bea", "role": "mentor", "years_on_team": "6"},
    )
    assert resp.status_code in (302, 303)
    assert (await _get(db, "Backfilled Bea")).years_on_team == 6


async def test_create_member_without_years_on_team_defaults_to_zero(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/members", data={"name": "Fresh Frank", "role": "student"},
    )
    assert resp.status_code in (302, 303)
    assert (await _get(db, "Fresh Frank")).years_on_team == 0


async def test_edit_member_updates_years_on_team(client, db, make_member):
    m = await make_member(name="Edit Edie", role=MemberRole.student, years_on_team=1)
    await _login(client)
    resp = await client.post(
        f"/admin/members/{m.id}/edit",
        data={"name": "Edit Edie", "role": "student", "years_on_team": "4"},
    )
    assert resp.status_code in (302, 303)
    assert (await _get(db, "Edit Edie")).years_on_team == 4


async def test_import_sets_years_on_team(client, db):
    await _login(client)
    csv_text = (
        "role,name,years_on_team\n"
        "student,Imported Ivy,3\n"
        "mentor,Imported Ike,\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200
    assert (await _get(db, "Imported Ivy")).years_on_team == 3
    assert (await _get(db, "Imported Ike")).years_on_team == 0  # blank -> default for new row


async def test_import_blank_years_on_team_leaves_existing_value_unchanged(client, db, make_member):
    """Unlike graduation_year, a blank years_on_team column must NOT reset an existing
    member's count to 0 — it's a running counter the yearly job maintains, and a
    routine CSV re-import (e.g. to sync team assignments) shouldn't silently erase it."""
    await make_member(name="Kept Kim", role=MemberRole.student, team_number=4143, years_on_team=5)
    await _login(client)
    csv_text = (
        "role,name,team_number\n"
        "student,Kept Kim,4423\n"  # no years_on_team column at all
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200
    kim = await _get(db, "Kept Kim")
    assert kim.years_on_team == 5  # unchanged
    assert (await _loaded(db, "Kept Kim")).team.number == 4423  # other fields still update


async def test_import_reports_invalid_years_on_team(client, db):
    await _login(client)
    csv_text = (
        "role,name,years_on_team\n"
        "student,Bad Years,several\n"
        "student,Good Years,2\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200
    assert await _get(db, "Bad Years") is None
    assert (await _get(db, "Good Years")).years_on_team == 2

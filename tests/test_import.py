"""CSV import: upsert by name, team/focus lookup, is_lead, error rows."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member, MemberRole


async def _login(client):
    # Default ADMIN_PASSWORD in tests is "changeme".
    await client.post("/admin/login", data={"password": "changeme"})


def _csv_upload(text: str):
    return {"file": ("members.csv", text.encode("utf-8"), "text/csv")}


async def _member(db, name):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.focus_group))
            .where(Member.name == name)
        )
    ).scalars().first()


async def test_import_creates_members(client, db):
    await _login(client)
    csv_text = (
        "role,name,team_number,focus_group,slack_user_id,is_lead\n"
        "student,Alice Smith,4143,software,U01ABC,\n"
        "mentor,Jane Doe,4423,design,U02XYZ,true\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200

    alice = await _member(db, "Alice Smith")
    assert alice.role == MemberRole.student
    assert alice.team.number == 4143
    assert alice.focus_group.slug == "software"
    assert alice.member_code and len(alice.member_code) == 8

    jane = await _member(db, "Jane Doe")
    assert jane.role == MemberRole.mentor
    assert jane.is_lead is True


async def test_import_upserts_by_name(client, db, make_member):
    await make_member(name="Bob Jones", team_number=4143, focus_slug="software", slack="U0OLD")
    await _login(client)
    csv_text = (
        "role,name,team_number,focus_group,slack_user_id,is_lead\n"
        "student,bob jones,4423,design,,\n"  # case-insensitive match, moves teams
    )
    await client.post("/admin/import", files=_csv_upload(csv_text))

    bob = await _member(db, "Bob Jones")
    assert bob.team.number == 4423
    assert bob.focus_group.slug == "design"


async def test_import_reports_unknown_team_and_focus(client, db):
    await _login(client)
    csv_text = (
        "role,name,team_number,focus_group,slack_user_id,is_lead\n"
        "student,Bad Team,9999,software,,\n"
        "student,Bad Focus,4143,nope,,\n"
        "student,Good One,4143,software,,\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200
    # Only the valid row is created.
    assert await _member(db, "Good One") is not None
    assert await _member(db, "Bad Team") is None
    assert await _member(db, "Bad Focus") is None


async def test_import_requires_auth(client, db):
    csv_text = "role,name\nstudent,Nobody\n"
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    # Unauthenticated -> redirected to login, no member created.
    assert resp.status_code in (302, 303)
    assert await _member(db, "Nobody") is None

"""CSV import: upsert by name, team/focus lookup, is_lead, error rows."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member, MemberRole, StudentGrade


async def _login(client):
    # Default ADMIN_PASSWORD in tests is "changeme".
    await client.post("/admin/login", data={"password": "changeme"})


def _csv_upload(text: str):
    return {"file": ("members.csv", text.encode("utf-8"), "text/csv")}


async def _member(db, name):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.name == name)
        )
    ).scalars().first()


async def test_import_creates_members(client, db):
    await _login(client)
    csv_text = (
        "role,name,team_number,subteam,slack_user_id,is_lead\n"
        "student,Alice Smith,4143,software,U01ABC,\n"
        "mentor,Jane Doe,4423,design,U02XYZ,true\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200

    alice = await _member(db, "Alice Smith")
    assert alice.role == MemberRole.student
    assert alice.team.number == 4143
    assert alice.subteam.slug == "software"
    assert alice.member_code and len(alice.member_code) == 8

    jane = await _member(db, "Jane Doe")
    assert jane.role == MemberRole.mentor
    assert jane.is_lead is True


async def test_import_upserts_by_name(client, db, make_member):
    await make_member(name="Bob Jones", team_number=4143, subteam_slug="software", slack="U0OLD")
    await _login(client)
    csv_text = (
        "role,name,team_number,subteam,slack_user_id,is_lead\n"
        "student,bob jones,4423,design,,\n"  # case-insensitive match, moves teams
    )
    await client.post("/admin/import", files=_csv_upload(csv_text))

    bob = await _member(db, "Bob Jones")
    assert bob.team.number == 4423
    assert bob.subteam.slug == "design"


async def test_import_reports_unknown_team_and_focus(client, db):
    await _login(client)
    csv_text = (
        "role,name,team_number,subteam,slack_user_id,is_lead\n"
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


async def test_import_grade_and_guardians(client, db):
    await _login(client)
    csv_text = (
        "role,name,grade,parent_guardian_1,parent_guardian_2\n"
        "student,Ada Byron,Sophomore,Anne Byron,George Byron\n"   # label form
        "student,Bea Green,junior_high,,\n"                        # enum-value form
        "mentor,Cyril Fox,Senior,Ignored Parent,\n"               # grade/parent ignored for mentors
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200

    ada = await _member(db, "Ada Byron")
    assert ada.grade == StudentGrade.sophomore
    assert ada.parent_guardian_1 == "Anne Byron"
    assert ada.parent_guardian_2 == "George Byron"

    assert (await _member(db, "Bea Green")).grade == StudentGrade.junior_high

    # Mentors never carry grade / guardians even if the CSV supplies them.
    cyril = await _member(db, "Cyril Fox")
    assert cyril.grade is None
    assert cyril.parent_guardian_1 is None


async def test_import_reports_unknown_grade(client, db):
    await _login(client)
    csv_text = (
        "role,name,grade\n"
        "student,Bad Grade,Kindergarten\n"
        "student,Good Grade,Freshman\n"
    )
    resp = await client.post("/admin/import", files=_csv_upload(csv_text))
    assert resp.status_code == 200
    assert await _member(db, "Bad Grade") is None
    assert (await _member(db, "Good Grade")).grade == StudentGrade.freshman

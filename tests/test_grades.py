"""Yearly grade auto-increase: /admin/members/bump-grades."""
from sqlalchemy import select

from app.models import Member, MemberRole, StudentGrade, Team


async def _login(client):
    # Test admin_password is fixed in conftest.py's _isolate_settings_from_dotenv.
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _get(db, name):
    db.expire_all()  # drop cached state so we read what the endpoint committed
    return (await db.execute(select(Member).where(Member.name == name))).scalars().first()


async def test_bump_advances_and_graduates(client, db, make_member):
    await make_member(name="Frosh", grade=StudentGrade.freshman)
    await make_member(name="Junior Jim", grade=StudentGrade.junior)
    await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    await make_member(name="Old Grad", grade=StudentGrade.alumni)
    await make_member(name="No Grade")  # grade is None
    await make_member(name="Coach", role=MemberRole.mentor, grade=StudentGrade.junior)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    # Each active, graded student advances one step.
    assert (await _get(db, "Frosh")).grade == StudentGrade.sophomore
    assert (await _get(db, "Junior Jim")).grade == StudentGrade.senior

    # A senior graduates to alumni AND is archived.
    sue = await _get(db, "Senior Sue")
    assert sue.grade == StudentGrade.alumni
    assert sue.is_active is False

    # Already-alumni are left alone (and stay active).
    grad = await _get(db, "Old Grad")
    assert grad.grade == StudentGrade.alumni
    assert grad.is_active is True

    # Grade-less students and mentors are untouched.
    assert (await _get(db, "No Grade")).grade is None
    assert (await _get(db, "Coach")).grade == StudentGrade.junior


async def test_bump_requires_auth(client, db, make_member):
    await make_member(name="Frosh", grade=StudentGrade.freshman)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    assert (await _get(db, "Frosh")).grade == StudentGrade.freshman  # unchanged


async def test_bump_to_junior_moves_to_4143(client, db, make_member):
    """A sophomore on 4423 who bumps up to Junior moves onto 4143 — MARS' Minions is
    the underclassman team, and reaching Junior is the trigger to move to MARS/WARS."""
    await make_member(name="Soph Sam", grade=StudentGrade.sophomore, team_number=4423)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    sam = await _get(db, "Soph Sam")
    assert sam.grade == StudentGrade.junior
    team = (await db.execute(select(Team).where(Team.id == sam.team_id))).scalars().first()
    assert team.number == 4143


async def test_bump_to_junior_already_on_4143_is_noop_for_team(client, db, make_member):
    await make_member(name="Soph Sue", grade=StudentGrade.sophomore, team_number=4143)

    await _login(client)
    await client.post("/admin/members/bump-grades")

    sue = await _get(db, "Soph Sue")
    assert sue.grade == StudentGrade.junior
    team = (await db.execute(select(Team).where(Team.id == sue.team_id))).scalars().first()
    assert team.number == 4143


async def test_already_junior_before_bump_is_not_moved(client, db, make_member):
    """The team move only fires on the sophomore->junior transition itself — an
    already-Junior student (who bumps to Senior this run) keeps whatever team they
    were manually placed on, even if that's still 4423."""
    await make_member(name="Junior Jan", grade=StudentGrade.junior, team_number=4423)

    await _login(client)
    await client.post("/admin/members/bump-grades")

    jan = await _get(db, "Junior Jan")
    assert jan.grade == StudentGrade.senior
    team = (await db.execute(select(Team).where(Team.id == jan.team_id))).scalars().first()
    assert team.number == 4423  # untouched


async def test_bump_reports_team_moved_count_in_message(client, db, make_member):
    await make_member(name="Soph Sam", grade=StudentGrade.sophomore, team_number=4423)
    await make_member(name="Frosh Fi", grade=StudentGrade.freshman, team_number=4423)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    location = resp.headers.get("location", "")
    assert "1 moved to team 4143" in location.replace("%20", " ")

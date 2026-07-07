"""Yearly grade auto-increase: /admin/members/bump-grades."""
from sqlalchemy import select

from app.models import Member, MemberRole, StudentGrade


async def _login(client):
    await client.post("/admin/login", data={"password": "changeme"})


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

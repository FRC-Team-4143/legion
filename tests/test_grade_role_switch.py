"""Grade survives a student->mentor role switch (a common FRC pattern — a returning
student mentoring their old team); guardians are still cleared, since they're strictly
student-only. This used to also cover `graduation_year` and the terminal `alumni` grade
value — both moved to the Alumni app (see legion/CLAUDE.md's "Members are unified" and
"Graduation is a push-event, not stored state" sections); Alumni's own test suite now
owns "does a returning alumnus keep their history", since Legion no longer stores any."""
from sqlalchemy import select

from app.models import Member, MemberRole, StudentGrade


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _get(db, name):
    db.expire_all()
    return (await db.execute(select(Member).where(Member.name == name))).scalars().first()


async def test_switching_student_to_mentor_keeps_grade_clears_guardians(client, db, make_member):
    m = await make_member(
        name="Robin Returning", role=MemberRole.student, grade=StudentGrade.senior,
        parent_guardian_1="U03GUARD1", parent_guardian_2="U03GUARD2",
    )
    await _login(client)
    resp = await client.post(
        f"/admin/members/{m.id}/edit",
        data={
            "name": "Robin Returning",
            "role": "mentor",
            "grade": StudentGrade.senior.value,
            "parent_guardian_1": "U03GUARD1",
            "parent_guardian_2": "U03GUARD2",
        },
    )
    assert resp.status_code in (302, 303)

    updated = await _get(db, "Robin Returning")
    assert updated.role == MemberRole.mentor
    assert updated.grade == StudentGrade.senior
    assert updated.parent_guardian_1 is None
    assert updated.parent_guardian_2 is None


async def test_creating_mentor_directly_with_grade_is_kept(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/members",
        data={
            "name": "Longtime Coach",
            "role": "mentor",
            "grade": StudentGrade.senior.value,
            "parent_guardian_1": "U03IGNORED",
        },
    )
    assert resp.status_code in (302, 303)

    coach = await _get(db, "Longtime Coach")
    assert coach.grade == StudentGrade.senior
    assert coach.parent_guardian_1 is None

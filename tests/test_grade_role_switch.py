"""Grade/graduation_year survive a student->mentor role switch (a past alumnus who's
now mentoring); guardians are still cleared, since they're strictly student-only."""
from sqlalchemy import select

from app.models import Member, MemberRole, StudentGrade


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _get(db, name):
    db.expire_all()
    return (await db.execute(select(Member).where(Member.name == name))).scalars().first()


async def test_switching_alumna_to_mentor_keeps_grade_and_year_clears_guardians(client, db, make_member):
    m = await make_member(
        name="Robin Returning", role=MemberRole.student, grade=StudentGrade.alumni,
        graduation_year=2022, parent_guardian_1="U03GUARD1", parent_guardian_2="U03GUARD2",
    )
    await _login(client)
    resp = await client.post(
        f"/admin/members/{m.id}/edit",
        data={
            "name": "Robin Returning",
            "role": "mentor",
            "grade": StudentGrade.alumni.value,
            "graduation_year": "2022",
            "parent_guardian_1": "U03GUARD1",
            "parent_guardian_2": "U03GUARD2",
        },
    )
    assert resp.status_code in (302, 303)

    updated = await _get(db, "Robin Returning")
    assert updated.role == MemberRole.mentor
    assert updated.grade == StudentGrade.alumni
    assert updated.graduation_year == 2022
    assert updated.parent_guardian_1 is None
    assert updated.parent_guardian_2 is None


async def test_creating_mentor_directly_with_grade_and_year_is_kept(client, db):
    await _login(client)
    resp = await client.post(
        "/admin/members",
        data={
            "name": "Longtime Coach",
            "role": "mentor",
            "grade": StudentGrade.alumni.value,
            "graduation_year": "2015",
            "parent_guardian_1": "U03IGNORED",
        },
    )
    assert resp.status_code in (302, 303)

    coach = await _get(db, "Longtime Coach")
    assert coach.grade == StudentGrade.alumni
    assert coach.graduation_year == 2015
    assert coach.parent_guardian_1 is None

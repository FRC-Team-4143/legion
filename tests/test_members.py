"""Member service: canonical code generation, uniqueness, serialization."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member, MemberRole, StudentGrade
from app.services.members import generate_member_code, serialize_member


async def _loaded(db, name):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.name == name)
        )
    ).scalars().first()


async def test_generate_member_code_is_unique_and_hex(db, make_member):
    code = await generate_member_code(db)
    assert len(code) == 8
    int(code, 16)  # valid hex — raises if not

    # A code already in use is never handed out again.
    await make_member(name="Existing", code=code)
    second = await generate_member_code(db)
    assert second != code


async def test_member_code_is_stable_across_rename(db, make_member):
    m = await make_member(name="Grace Hopper")
    original = m.member_code
    m.name = "Grace B. Hopper"
    await db.commit()
    await db.refresh(m)
    assert m.member_code == original  # rename must not change the canonical code


async def test_serialize_member_shape(db, make_member):
    from sqlalchemy.orm import selectinload

    await make_member(
        name="Kay Ryan", role=MemberRole.mentor, team_number=4423,
        subteam_slug="design", slack="U0LEAD", is_lead=True,
    )
    m = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.name == "Kay Ryan")
        )
    ).scalars().first()
    data = serialize_member(m)
    assert data["name"] == "Kay Ryan"
    assert data["role"] == "mentor"
    assert data["team_number"] == 4423
    assert data["subteam"] == {"slug": "design", "label": "Design"}
    assert data["slack_user_id"] == "U0LEAD"
    assert data["is_lead"] is True
    assert data["updated_at"].endswith("Z")


async def test_member_without_team_or_focus_serializes_null(db, make_member):
    from sqlalchemy.orm import selectinload

    await make_member(name="No Team", team_number=None, subteam_slug=None)
    m = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.name == "No Team")
        )
    ).scalars().first()
    data = serialize_member(m)
    assert data["team_number"] is None
    assert data["subteam"] is None


async def test_serialize_member_exposes_grade_but_not_guardians(db, make_member):
    await make_member(
        name="Gale Boetticher", role=MemberRole.student, grade=StudentGrade.junior,
        parent_guardian_1="Pat Boetticher", parent_guardian_2="Sam Boetticher",
    )
    data = serialize_member(await _loaded(db, "Gale Boetticher"))
    # School year is on the wire...
    assert data["grade"] == "junior"
    # ...but guardian PII is deliberately kept off the API.
    assert "parent_guardian_1" not in data
    assert "parent_guardian_2" not in data


async def test_serialize_member_grade_null_for_mentor(db, make_member):
    await make_member(name="No Grade", role=MemberRole.mentor)
    data = serialize_member(await _loaded(db, "No Grade"))
    assert data["grade"] is None

"""Slack profile field mapping (pure function — no Slack/network calls)."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member, MemberRole, StudentGrade
from app.services.slack_profile import (
    FIELD_SUBTEAM, FIELD_PARENT_1, FIELD_PARENT_2, FIELD_SCHOOL_YEAR, FIELD_TEAM,
    build_profile_fields,
)


async def _loaded(db, name):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.name == name)
        )
    ).scalars().first()


async def test_build_profile_fields_student(db, make_member):
    await make_member(
        name="Student One", role=MemberRole.student, team_number=4143,
        subteam_slug="software", grade=StudentGrade.sophomore,
        parent_guardian_1="Parent A", parent_guardian_2="Parent B",
    )
    fields = build_profile_fields(await _loaded(db, "Student One"))

    assert fields[FIELD_TEAM]["value"] == "MARS/WARS"
    assert fields[FIELD_SCHOOL_YEAR]["value"] == "Sophomore"  # label, not enum value
    assert fields[FIELD_SUBTEAM]["value"] == "Software"
    assert fields[FIELD_PARENT_1]["value"] == "Parent A"
    assert fields[FIELD_PARENT_2]["value"] == "Parent B"


async def test_build_profile_fields_omits_guardians_for_mentor(db, make_member):
    await make_member(name="Mentor One", role=MemberRole.mentor, team_number=4423, subteam_slug="design")
    fields = build_profile_fields(await _loaded(db, "Mentor One"))

    # Team / focus are sent for everyone...
    assert fields[FIELD_TEAM]["value"] == "MARS' Minions"
    assert fields[FIELD_SUBTEAM]["value"] == "Design"
    # ...but the guardian field IDs are omitted entirely for mentors.
    assert FIELD_PARENT_1 not in fields
    assert FIELD_PARENT_2 not in fields
    # Mentors have no grade -> empty string clears any stale value.
    assert fields[FIELD_SCHOOL_YEAR]["value"] == ""


async def test_build_profile_fields_blank_when_unset(db, make_member):
    await make_member(name="Bare Student", role=MemberRole.student, team_number=None, subteam_slug=None)
    fields = build_profile_fields(await _loaded(db, "Bare Student"))
    assert fields[FIELD_TEAM]["value"] == ""
    assert fields[FIELD_SCHOOL_YEAR]["value"] == ""
    assert fields[FIELD_SUBTEAM]["value"] == ""
    # Still a student, so guardian fields are present (empty -> clears).
    assert fields[FIELD_PARENT_1]["value"] == ""

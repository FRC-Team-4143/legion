"""Slack usergroup membership criteria (DB query only — no Slack/network calls)."""
from app.models import MemberRole
from app.services.slack_usergroups import (
    USERGROUP_ROLE_MENTORS, USERGROUP_ROLE_STUDENTS, USERGROUP_SUBTEAM_BUSINESS,
    USERGROUP_SUBTEAM_DESIGN, USERGROUP_SUBTEAM_SOFTWARE, USERGROUP_TEAM_4143,
    USERGROUP_TEAM_4423, _usergroup_criteria, matching_slack_ids,
)


async def _ids(db, usergroup_id):
    criteria = _usergroup_criteria()
    return await matching_slack_ids(db, criteria[usergroup_id])


async def test_team_criteria(db, make_member):
    await make_member(name="A", team_number=4143, slack="U001")
    await make_member(name="B", team_number=4423, slack="U002", username="b")
    assert await _ids(db, USERGROUP_TEAM_4143) == ["U001"]
    assert await _ids(db, USERGROUP_TEAM_4423) == ["U002"]


async def test_subteam_criteria(db, make_member):
    await make_member(name="A", subteam_slug="software", slack="U001")
    await make_member(name="B", subteam_slug="design", slack="U002", username="b")
    await make_member(name="C", subteam_slug="business", slack="U003", username="c")
    assert await _ids(db, USERGROUP_SUBTEAM_SOFTWARE) == ["U001"]
    assert await _ids(db, USERGROUP_SUBTEAM_DESIGN) == ["U002"]
    assert await _ids(db, USERGROUP_SUBTEAM_BUSINESS) == ["U003"]


async def test_role_criteria(db, make_member):
    await make_member(name="Stu", role=MemberRole.student, slack="U001")
    await make_member(name="Ment", role=MemberRole.mentor, slack="U002", username="ment")
    assert await _ids(db, USERGROUP_ROLE_STUDENTS) == ["U001"]
    assert await _ids(db, USERGROUP_ROLE_MENTORS) == ["U002"]


async def test_excludes_members_without_slack_id(db, make_member):
    await make_member(name="No Slack", team_number=4143, slack=None)
    assert await _ids(db, USERGROUP_TEAM_4143) == []


async def test_excludes_archived_members(db, make_member):
    await make_member(name="Archived", team_number=4143, slack="U001", is_active=False)
    assert await _ids(db, USERGROUP_TEAM_4143) == []

"""Archiving a member (manual archive, or the bump-grades alumni auto-archive) should
immediately resync the Slack usergroups rather than waiting on the next manual "Sync to
Slack" click — see `_resync_slack_usergroups` in routers/admin.py."""
from sqlalchemy import select

from app.config import settings
from app.models import Member, MemberRole, StudentGrade


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


def _fake_sync(calls):
    async def _sync(db):
        calls.append(True)
        return {"sent": 0, "skipped": 0, "failed": 0, "total": 0}
    return _sync


async def test_manual_archive_triggers_usergroup_resync(client, db, make_member, monkeypatch):
    from app.services import slack_usergroups
    calls = []
    monkeypatch.setattr(slack_usergroups, "sync_all_usergroups", _fake_sync(calls))
    settings.slack_bot_token = "xoxp-test"

    target = await make_member(name="Target Member")
    await _login(client)
    resp = await client.post(f"/admin/members/{target.id}/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [True]


async def test_manual_archive_skips_resync_when_slack_unconfigured(client, db, make_member, monkeypatch):
    from app.services import slack_usergroups
    calls = []
    monkeypatch.setattr(slack_usergroups, "sync_all_usergroups", _fake_sync(calls))
    assert settings.slack_bot_token == ""  # reset by the autouse settings fixture

    target = await make_member(name="Target Member")
    await _login(client)
    resp = await client.post(f"/admin/members/{target.id}/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == []


async def test_bump_grades_graduation_triggers_usergroup_resync(client, db, make_member, monkeypatch):
    from app.services import slack_usergroups
    calls = []
    monkeypatch.setattr(slack_usergroups, "sync_all_usergroups", _fake_sync(calls))
    settings.slack_bot_token = "xoxp-test"

    await make_member(name="Graduating Senior", role=MemberRole.student, grade=StudentGrade.senior)
    await _login(client)
    resp = await client.post("/admin/members/bump-grades", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [True]


async def test_bump_grades_without_graduation_skips_resync(client, db, make_member, monkeypatch):
    from app.services import slack_usergroups
    calls = []
    monkeypatch.setattr(slack_usergroups, "sync_all_usergroups", _fake_sync(calls))
    settings.slack_bot_token = "xoxp-test"

    await make_member(name="Sophomore Sam", role=MemberRole.student, grade=StudentGrade.freshman)
    await _login(client)
    resp = await client.post("/admin/members/bump-grades", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == []  # nobody graduated -> no immediate resync needed


async def test_archived_member_excluded_from_matching_slack_ids(db, make_member):
    """End-to-end on the query side: once is_active flips, the member drops out of
    every usergroup's computed membership (matching_slack_ids), which is what makes
    the resync actually remove them from Slack."""
    from app.services.slack_usergroups import USERGROUP_TEAM_4143, _usergroup_criteria, matching_slack_ids

    m = await make_member(name="Soon Archived", team_number=4143, slack="U001")
    criterion = _usergroup_criteria()[USERGROUP_TEAM_4143]
    assert await matching_slack_ids(db, criterion) == ["U001"]

    m.is_active = False
    await db.commit()
    assert await matching_slack_ids(db, criterion) == []

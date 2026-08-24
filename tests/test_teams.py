"""The teams admin list page's member counts — total plus the student/mentor split,
counting active members only."""


async def _login(client):
    # Test admin_password is fixed in conftest.py's _isolate_settings_from_dotenv.
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def test_teams_list_splits_count_by_role(client, make_member):
    from app.models import MemberRole

    await make_member(name="Ada Lovelace", team_number=4143)
    await make_member(name="Grace Hopper", team_number=4143)
    await make_member(name="Alan Turing", team_number=4143, role=MemberRole.mentor)
    await _login(client)

    resp = await client.get("/admin/teams")
    assert resp.status_code == 200
    assert "2 students" in resp.text
    assert "1 mentor" in resp.text


async def test_teams_list_count_excludes_archived_members(client, make_member):
    await make_member(name="Ada Lovelace", team_number=4143)
    await make_member(name="Grace Hopper", team_number=4143, is_active=False)
    await _login(client)

    resp = await client.get("/admin/teams")
    # The archived member is not counted — one student, not two.
    assert "1 student " in resp.text
    assert "2 students" not in resp.text


async def test_teams_list_shows_team_with_no_members(client, make_member):
    """The is_active filter lives in the JOIN condition, not a WHERE — otherwise a team
    whose members are all archived (or has none at all) would vanish off the page."""
    await make_member(name="Ada Lovelace", team_number=4423, is_active=False)
    await _login(client)

    resp = await client.get("/admin/teams")
    assert "4143" in resp.text  # seeded, no members at all
    assert "4423" in resp.text  # seeded, only an archived member
    assert "0 students" in resp.text

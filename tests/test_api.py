"""Read API: auth gating, filtering, incremental sync, serialization."""
from datetime import timedelta

from app.models import MemberRole
from app.utils import isoformat_utc, now_utc


async def test_api_requires_key(client, make_member):
    await make_member(name="Alice")
    resp = await client.get("/api/members")
    assert resp.status_code == 503  # no key configured -> fails closed


async def test_api_rejects_wrong_key(client, api_key, make_member):
    await make_member(name="Alice")
    resp = await client.get("/api/members", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


async def test_list_members_ok(client, api_key, make_member):
    await make_member(name="Alice", role=MemberRole.student)
    await make_member(name="Bob", role=MemberRole.mentor, slack="U0BOB")
    resp = await client.get("/api/members", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()["members"]}
    assert names == {"Alice", "Bob"}


async def test_filter_by_role(client, api_key, make_member):
    await make_member(name="Alice", role=MemberRole.student)
    await make_member(name="Bob", role=MemberRole.mentor, slack="U0BOB")
    resp = await client.get("/api/members?role=mentor", headers={"X-API-Key": api_key})
    members = resp.json()["members"]
    assert [m["name"] for m in members] == ["Bob"]


async def test_filter_by_team_number(client, api_key, make_member):
    await make_member(name="OnA", team_number=4143)
    await make_member(name="OnB", team_number=4423, slack="U0B")
    resp = await client.get("/api/members?team_number=4423", headers={"X-API-Key": api_key})
    assert [m["name"] for m in resp.json()["members"]] == ["OnB"]


async def test_filter_active_excludes_archived(client, api_key, make_member):
    await make_member(name="Active")
    await make_member(name="Gone", is_active=False, slack="U0G")
    all_resp = await client.get("/api/members", headers={"X-API-Key": api_key})
    assert {m["name"] for m in all_resp.json()["members"]} == {"Active", "Gone"}
    active_resp = await client.get("/api/members?active=true", headers={"X-API-Key": api_key})
    assert {m["name"] for m in active_resp.json()["members"]} == {"Active"}


async def test_updated_since_incremental(client, api_key, make_member, db):
    m = await make_member(name="Alice")
    # A cutoff in the future returns nothing.
    future = isoformat_utc(now_utc() + timedelta(hours=1))
    resp = await client.get(f"/api/members?updated_since={future}", headers={"X-API-Key": api_key})
    assert resp.json()["members"] == []
    # A cutoff in the past returns the member.
    past = isoformat_utc(now_utc() - timedelta(hours=1))
    resp = await client.get(f"/api/members?updated_since={past}", headers={"X-API-Key": api_key})
    assert [m["name"] for m in resp.json()["members"]] == ["Alice"]


async def test_get_member_by_code(client, api_key, make_member):
    m = await make_member(name="Alice", code="abcd1234")
    resp = await client.get("/api/members/abcd1234", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alice"
    missing = await client.get("/api/members/00000000", headers={"X-API-Key": api_key})
    assert missing.status_code == 404


async def test_teams_and_subteams(client, api_key):
    teams = await client.get("/api/teams", headers={"X-API-Key": api_key})
    numbers = {t["number"] for t in teams.json()["teams"]}
    assert {4143, 4423} <= numbers

    groups = await client.get("/api/subteams", headers={"X-API-Key": api_key})
    slugs = {g["slug"] for g in groups.json()["subteams"]}
    assert {"software", "design", "business"} <= slugs

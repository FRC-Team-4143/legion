"""
Slack profile sync — pushes each member's roster metadata into their Slack *custom
profile fields* (Team, School Year, Subteam, Parent/Guardian 1 & 2).

Legion is the source of truth, so this is a one-way push out to Slack: a nightly
scheduled job (`services/scheduler.py`) and an on-demand admin button both call
`sync_all_profiles`. Mirrors the cached-client + swallow-and-log discipline of the
sibling apps' `slack_client.py`.

NOTE: `users.profile.set` for *other* users requires an admin *user* token (xoxp-…)
with `users.profile:write`; a normal bot token can only edit its own profile.
"""
import logging
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member, MemberRole, grade_label

log = logging.getLogger(__name__)

# Slack custom profile field IDs (from the workspace profile config).
FIELD_TEAM = "Xf03V78BSQGN"
FIELD_SCHOOL_YEAR = "Xf03VDS8N6CS"
FIELD_SUBTEAM = "Xf040CF3H789"
FIELD_PARENT_1 = "Xf0402A21C12"
FIELD_PARENT_2 = "Xf0BCQJCDW8K"

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


def build_profile_fields(member: Member) -> dict:
    """Map a member onto the Slack custom-profile `fields` payload. Requires `team` and
    `subteam` to be loaded. Team / school year / subteam are sent for everyone
    (empty string clears a stale value); Parent/Guardian fields are sent for students
    only — mentors never have guardians, so those field IDs are omitted entirely."""
    fields: dict[str, dict] = {
        FIELD_TEAM: {"value": member.team.name if member.team else ""},
        FIELD_SCHOOL_YEAR: {"value": grade_label(member.grade) if member.grade else ""},
        FIELD_SUBTEAM: {"value": member.subteam.label if member.subteam else ""},
    }
    if member.role == MemberRole.student:
        fields[FIELD_PARENT_1] = {"value": member.parent_guardian_1 or ""}
        fields[FIELD_PARENT_2] = {"value": member.parent_guardian_2 or ""}
    return fields


async def push_member_profile(member: Member, *, automated: bool = False) -> bool:
    """Push one member's metadata to their Slack profile. Returns True on success,
    False if the member has no Slack id, the sync is disabled, or the call fails
    (never raises — a Slack outage must not crash the caller or the scheduler)."""
    if automated and not settings.updates_enabled:
        return False
    if not member.slack_user_id or not settings.slack_bot_token:
        return False
    try:
        await get_slack_client().users_profile_set(
            user=member.slack_user_id,
            profile={"fields": build_profile_fields(member)},
        )
        return True
    except Exception as e:
        log.error("Slack profile sync failed for %s (%s): %s", member.name, member.slack_user_id, e)
        return False


async def sync_all_profiles(db: AsyncSession, *, automated: bool = False) -> dict:
    """Push every active member with a Slack id. Returns {sent, skipped, failed}."""
    members = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.is_active.is_(True), Member.slack_user_id.is_not(None))
            .order_by(Member.name)
        )
    ).scalars().all()

    sent = failed = 0
    for m in members:
        if await push_member_profile(m, automated=automated):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "skipped": 0, "failed": failed, "total": len(members)}

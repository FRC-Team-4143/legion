"""
Slack usergroup sync — keeps a fixed set of existing Slack *User Groups* (the
`@handle`-style groups used for mentions/channel permissions, distinct from Legion's own
`Group` model) populated to match Legion's roster, based purely on fields Legion already
has: role, team, and subteam.

Legion is the source of truth, so like `slack_profile.py` this is a one-way push out to
Slack: manual-only, triggered by the same "Sync to Slack" admin button
(`POST /admin/members/sync-slack`). Mirrors that module's cached-client + swallow-and-log
discipline; reuses its cached `AsyncWebClient` rather than opening a second one.

NOTE: `usergroups.users.update` REPLACES a usergroup's entire member list per call (no
incremental add/remove) — anyone in one of these usergroups who isn't an active,
Slack-linked, matching Legion member gets removed on sync. Requires the `usergroups:write`
OAuth scope on `settings.slack_bot_token`'s Slack app in addition to the profile-write
scope `slack_profile.py` already needs.
"""
import logging

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Member, MemberRole, Subteam, Team
from app.services.slack_profile import get_slack_client

log = logging.getLogger(__name__)

# Slack usergroup IDs (from the workspace's existing User Groups config).
USERGROUP_TEAM_4143 = "S0BSJ424TJR"
USERGROUP_TEAM_4423 = "S0BSRNPFH6G"
USERGROUP_SUBTEAM_BUSINESS = "S0BSFR5SZJP"
USERGROUP_SUBTEAM_SOFTWARE = "S0BSJ3C9ZFF"
USERGROUP_SUBTEAM_DESIGN = "S0BSQ25V11P"
USERGROUP_ROLE_STUDENTS = "S0BSJ4HRZV3"
USERGROUP_ROLE_MENTORS = "S0BSN4E4FPU"


def _usergroup_criteria() -> dict[str, ColumnElement]:
    """Map each Slack usergroup ID to the Legion membership criterion that determines
    who belongs in it."""
    return {
        USERGROUP_TEAM_4143: Member.team.has(Team.number == 4143),
        USERGROUP_TEAM_4423: Member.team.has(Team.number == 4423),
        USERGROUP_SUBTEAM_BUSINESS: Member.subteam.has(Subteam.slug == "business"),
        USERGROUP_SUBTEAM_SOFTWARE: Member.subteam.has(Subteam.slug == "software"),
        USERGROUP_SUBTEAM_DESIGN: Member.subteam.has(Subteam.slug == "design"),
        USERGROUP_ROLE_STUDENTS: Member.role == MemberRole.student,
        USERGROUP_ROLE_MENTORS: Member.role == MemberRole.mentor,
    }


async def matching_slack_ids(db: AsyncSession, criterion: ColumnElement) -> list[str]:
    """Slack user ids of every active, Slack-linked member matching `criterion`."""
    return list(
        (
            await db.execute(
                select(Member.slack_user_id).where(
                    Member.is_active.is_(True),
                    Member.slack_user_id.is_not(None),
                    criterion,
                )
            )
        ).scalars().all()
    )


async def sync_usergroup(db: AsyncSession, usergroup_id: str, criterion: ColumnElement) -> str:
    """Push one usergroup's membership. Returns "sent", "skipped" (no matching members —
    left alone rather than cleared), or "failed" (Slack call raised)."""
    ids = await matching_slack_ids(db, criterion)
    if not ids:
        return "skipped"
    try:
        await get_slack_client().usergroups_users_update(usergroup=usergroup_id, users=",".join(ids))
        return "sent"
    except Exception as e:
        log.error("Slack usergroup sync failed for %s: %s", usergroup_id, e)
        return "failed"


async def sync_all_usergroups(db: AsyncSession) -> dict:
    """Push every fixed usergroup. Returns {sent, skipped, failed, total}."""
    criteria = _usergroup_criteria()
    sent = skipped = failed = 0
    for usergroup_id, criterion in criteria.items():
        result = await sync_usergroup(db, usergroup_id, criterion)
        if result == "sent":
            sent += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed += 1
    return {"sent": sent, "skipped": skipped, "failed": failed, "total": len(criteria)}

"""
Member helpers: canonical `member_code` generation and the JSON serializers the read
API returns. Kept here (not in the routers) so the admin UI, the API, and tests share
one definition of "what a member looks like on the wire".
"""
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FocusGroup, Member, Team
from app.utils import isoformat_utc


async def generate_member_code(db: AsyncSession) -> str:
    """Mint a fresh, unique 8-hex `member_code`.

    Opaque and random (not derived from the name), so it is stable across renames and
    never collides on duplicate names. Retries on the (astronomically rare) collision.
    """
    for _ in range(20):
        code = secrets.token_hex(4)  # 8 hex chars
        exists = (
            await db.execute(select(Member.id).where(Member.member_code == code))
        ).scalars().first()
        if not exists:
            return code
    raise RuntimeError("Could not generate a unique member_code")


def serialize_member(member: Member) -> dict:
    """The public JSON shape of a member. Requires `team` and `focus_group` loaded."""
    return {
        "member_code": member.member_code,
        "name": member.name,
        "role": member.role.value,
        "team_number": member.team.number if member.team else None,
        "team_name": member.team.name if member.team else None,
        "focus_group": (
            {"slug": member.focus_group.slug, "label": member.focus_group.label}
            if member.focus_group else None
        ),
        "slack_user_id": member.slack_user_id,
        "is_active": member.is_active,
        "is_lead": member.is_lead,
        "updated_at": isoformat_utc(member.updated_at),
    }


def serialize_team(team: Team) -> dict:
    return {"number": team.number, "name": team.name}


def serialize_focus_group(fg: FocusGroup) -> dict:
    return {"slug": fg.slug, "label": fg.label, "is_active": fg.is_active}

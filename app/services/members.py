"""
Member helpers: canonical `member_code` generation and the JSON serializers the read
API returns. Kept here (not in the routers) so the admin UI, the API, and tests share
one definition of "what a member looks like on the wire".
"""
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Member, Subteam, Team
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
    """The public JSON shape of a member. Requires `team` and `subteam` loaded."""
    return {
        "member_code": member.member_code,
        "name": member.name,
        "role": member.role.value,
        "team_number": member.team.number if member.team else None,
        "team_name": member.team.name if member.team else None,
        "subteam": (
            {"slug": member.subteam.slug, "label": member.subteam.label}
            if member.subteam else None
        ),
        "slack_user_id": member.slack_user_id,
        "is_active": member.is_active,
        "is_lead": member.is_lead,
        # School year is useful roster metadata for consumers. Parent/guardian names are
        # intentionally NOT exposed on the API — that PII is only needed by the admin UI
        # (reads the ORM object) and Legion's own Slack push (operates in-process).
        "grade": member.grade.value if member.grade else None,
        "updated_at": isoformat_utc(member.updated_at),
    }


def serialize_team(team: Team) -> dict:
    return {"number": team.number, "name": team.name}


def serialize_subteam(subteam: Subteam) -> dict:
    return {"slug": subteam.slug, "label": subteam.label, "is_active": subteam.is_active}

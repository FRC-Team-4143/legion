"""
Read-only JSON API — the contract Tempus / Munus pull the roster from.

Auth: every request must carry the shared secret in the `X-API-Key` header (matched
against `settings.legion_api_key`). If no key is configured the API fails closed (503),
so a misconfigured deploy never serves member data unauthenticated.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import Member, MemberRole, Subteam, Team
from app.services.members import (
    serialize_member, serialize_subteam, serialize_team,
)
from app.utils import parse_iso_utc

router = APIRouter(prefix="/api")


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Dependency: reject requests without the configured API key."""
    if not settings.legion_api_key:
        raise HTTPException(status_code=503, detail="API is not configured (no API key set).")
    if not hmac.compare_digest(x_api_key, settings.legion_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


@router.get("/members", dependencies=[Depends(require_api_key)])
async def list_members(
    role: str | None = Query(default=None, description="Filter: 'student' or 'mentor'"),
    team_number: int | None = Query(default=None, description="Filter by FRC team number"),
    active: bool | None = Query(
        default=None,
        description="Filter by active status. Omit to include archived members too.",
    ),
    updated_since: str | None = Query(
        default=None,
        description="ISO-8601 timestamp; return only members changed at/after it (incremental sync).",
    ),
    db: AsyncSession = Depends(get_db),
):
    """List members. Includes archived/inactive by default so consumers can deactivate
    their local copies; pass `active=true` to get only the current roster."""
    q = (
        select(Member)
        .options(selectinload(Member.team), selectinload(Member.subteam))
        .order_by(Member.name)
    )

    if role is not None:
        try:
            q = q.where(Member.role == MemberRole(role))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role '{role}'.")
    if team_number is not None:
        q = q.join(Team, Team.id == Member.team_id).where(Team.number == team_number)
    if active is not None:
        q = q.where(Member.is_active.is_(active))
    if updated_since is not None:
        since = parse_iso_utc(updated_since)
        if since is None:
            raise HTTPException(status_code=400, detail="Invalid 'updated_since' timestamp.")
        q = q.where(Member.updated_at >= since)

    members = (await db.execute(q)).scalars().all()
    return {"members": [serialize_member(m) for m in members]}


@router.get("/members/{member_code}", dependencies=[Depends(require_api_key)])
async def get_member(member_code: str, db: AsyncSession = Depends(get_db)):
    member = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.subteam))
            .where(Member.member_code == member_code)
        )
    ).scalars().first()
    if member is None:
        raise HTTPException(status_code=404, detail="No member with that code.")
    return serialize_member(member)


@router.get("/teams", dependencies=[Depends(require_api_key)])
async def list_teams(db: AsyncSession = Depends(get_db)):
    teams = (await db.execute(select(Team).order_by(Team.number))).scalars().all()
    return {"teams": [serialize_team(t) for t in teams]}


@router.get("/subteams", dependencies=[Depends(require_api_key)])
async def list_subteams(db: AsyncSession = Depends(get_db)):
    groups = (
        await db.execute(select(Subteam).order_by(Subteam.sort_order, Subteam.label))
    ).scalars().all()
    return {"subteams": [serialize_subteam(g) for g in groups]}

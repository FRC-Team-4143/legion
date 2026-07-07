"""
SSO username generation — `last.first`, each part truncated to 4 characters and
lowercased (e.g. "Alexander Hamilton" -> "hami.alex"). A single-word name uses just
that word. Collisions get a numeric suffix. Called on member creation (manual +
CSV import) and by the one-time backfill migration for pre-existing rows.
"""
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Member

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _clean(part: str) -> str:
    return _NON_ALNUM.sub("", part.lower())[:4] or "x"


def generate_username(name: str) -> str:
    """Derive the base (pre-collision) username from a full name."""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "user"
    if len(parts) == 1:
        return _clean(parts[0])
    return f"{_clean(parts[-1])}.{_clean(parts[0])}"


async def assign_unique_username(
    db: AsyncSession, name: str, exclude_id: Optional[int] = None
) -> str:
    """Generate a username for `name` and suffix with 2, 3, … until it's unique."""
    base = generate_username(name)
    candidate = base
    suffix = 1
    while True:
        q = select(Member.id).where(Member.username == candidate)
        if exclude_id is not None:
            q = q.where(Member.id != exclude_id)
        exists = (await db.execute(q)).scalars().first()
        if not exists:
            return candidate
        suffix += 1
        candidate = f"{base}{suffix}"

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables and seed initial data (teams + subteams)."""
    from app import models  # noqa: F401 — imported for side-effect (table registration)

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        # Renames run BEFORE create_all so SQLAlchemy sees the already-renamed tables
        # and skips recreating them.
        await conn.run_sync(_migration_rename_focus_groups_to_subteams)
        await conn.run_sync(Base.metadata.create_all)
        # Additive column migrations run after create_all (safe on both fresh + existing).
        await conn.run_sync(_migration_add_member_metadata)

    await _seed_teams()
    await _seed_subteams()


def _migration_rename_focus_groups_to_subteams(conn) -> None:
    """Rename the focus_groups table to subteams and the focus_group_id column to
    subteam_id on members. No-op on a fresh schema (no `members` table yet — create_all()
    will make the current one right after) or an already-migrated database."""
    from sqlalchemy import inspect, text

    tables = inspect(conn).get_table_names()
    if "focus_groups" in tables:
        conn.execute(text("ALTER TABLE focus_groups RENAME TO subteams"))

    if "members" not in tables:
        return
    cols = {c["name"] for c in inspect(conn).get_columns("members")}
    if "focus_group_id" in cols:
        conn.execute(text("ALTER TABLE members RENAME COLUMN focus_group_id TO subteam_id"))


def _migration_add_member_metadata(conn) -> None:
    """Add the student metadata columns (grade + parent/guardian 1 & 2) to an existing
    `members` table. No-op on a freshly created schema, which already has them."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(conn).get_columns("members")}
    if "grade" not in cols:
        conn.execute(text("ALTER TABLE members ADD COLUMN grade VARCHAR(20)"))
    if "parent_guardian_1" not in cols:
        conn.execute(text("ALTER TABLE members ADD COLUMN parent_guardian_1 VARCHAR(200)"))
    if "parent_guardian_2" not in cols:
        conn.execute(text("ALTER TABLE members ADD COLUMN parent_guardian_2 VARCHAR(200)"))


async def _seed_teams() -> None:
    """Insert the two FRC teams if they aren't present yet (idempotent)."""
    from sqlalchemy import select
    from app.models import DEFAULT_TEAMS, Team

    async with AsyncSessionLocal() as session:
        existing = set(
            (await session.execute(select(Team.number))).scalars().all()
        )
        for number, name in DEFAULT_TEAMS:
            if number not in existing:
                session.add(Team(number=number, name=name))
        await session.commit()


async def _seed_subteams() -> None:
    """Insert the default subteams if the table is empty (admins can add more)."""
    from sqlalchemy import select
    from app.models import DEFAULT_SUBTEAMS, Subteam

    async with AsyncSessionLocal() as session:
        existing = set(
            (await session.execute(select(Subteam.slug))).scalars().all()
        )
        for i, (slug, label) in enumerate(DEFAULT_SUBTEAMS):
            if slug not in existing:
                session.add(Subteam(slug=slug, label=label, sort_order=i))
        await session.commit()

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
    """Create all tables and seed initial data (teams + focus groups)."""
    from app import models  # noqa: F401 — imported for side-effect (table registration)

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # (No migrations yet — this is the initial schema. Follow the sibling pattern:
        #  add a `def _migration(conn)` guarded by `inspect(conn)` and call it here.)

    await _seed_teams()
    await _seed_focus_groups()


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


async def _seed_focus_groups() -> None:
    """Insert the default focus groups if the table is empty (admins can add more)."""
    from sqlalchemy import select
    from app.models import DEFAULT_FOCUS_GROUPS, FocusGroup

    async with AsyncSessionLocal() as session:
        existing = set(
            (await session.execute(select(FocusGroup.slug))).scalars().all()
        )
        for i, (slug, label) in enumerate(DEFAULT_FOCUS_GROUPS):
            if slug not in existing:
                session.add(FocusGroup(slug=slug, label=label, sort_order=i))
        await session.commit()

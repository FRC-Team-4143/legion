"""
Shared pytest fixtures.

Every test runs against a fresh in-memory SQLite database. We use a StaticPool so the
single in-memory connection is shared across the session (in-memory DBs are otherwise
per-connection and would appear empty). Mirrors the sibling apps' test setup.
"""
import secrets

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    DEFAULT_FOCUS_GROUPS, DEFAULT_TEAMS, FocusGroup, Member, MemberRole, Team,
)


@pytest_asyncio.fixture
async def engine():
    """A fresh in-memory database engine with all tables created + defaults seeded."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed teams + focus groups the way init_db() would.
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sm() as s:
        for number, name in DEFAULT_TEAMS:
            s.add(Team(number=number, name=name))
        for i, (slug, label) in enumerate(DEFAULT_FOCUS_GROUPS):
            s.add(FocusGroup(slug=slug, label=label, sort_order=i))
        await s.commit()
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def make_member(db):
    """Factory: make_member(name=..., role=..., team_number=..., focus_slug=..., slack=...)."""
    from sqlalchemy import select

    async def _make(
        name: str = "Ada Lovelace",
        role: MemberRole = MemberRole.student,
        team_number: int | None = 4143,
        focus_slug: str | None = "software",
        slack: str | None = None,
        is_active: bool = True,
        is_lead: bool = False,
        code: str | None = None,
    ) -> Member:
        team_id = None
        if team_number is not None:
            t = (await db.execute(select(Team).where(Team.number == team_number))).scalars().first()
            team_id = t.id if t else None
        focus_id = None
        if focus_slug is not None:
            g = (await db.execute(select(FocusGroup).where(FocusGroup.slug == focus_slug))).scalars().first()
            focus_id = g.id if g else None
        m = Member(
            name=name,
            member_code=code or secrets.token_hex(4),
            role=role,
            team_id=team_id,
            focus_group_id=focus_id,
            slack_user_id=slack,
            is_active=is_active,
            is_lead=is_lead,
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m
    return _make


@pytest_asyncio.fixture
async def api_key():
    """Configure a known API key for the duration of a test."""
    from app.config import settings
    original = settings.legion_api_key
    settings.legion_api_key = "test-api-key"
    yield "test-api-key"
    settings.legion_api_key = original


@pytest_asyncio.fixture
async def client(session_factory):
    """An httpx AsyncClient wired to the app with get_db overridden to the test DB."""
    import httpx
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()

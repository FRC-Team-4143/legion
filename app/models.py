import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text,
    ForeignKey, Enum as SAEnum, Table, Column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MemberRole(str, enum.Enum):
    """Whether a member is a student or a mentor. The two sibling apps keep separate
    Student / Mentor tables; Legion unifies them behind one row + this discriminator."""
    student = "student"
    mentor = "mentor"


ROLE_LABELS: dict[MemberRole, str] = {
    MemberRole.student: "Student",
    MemberRole.mentor: "Mentor",
}


def role_label(role: Optional[MemberRole]) -> str:
    return ROLE_LABELS.get(role, "—") if role else "—"


class StudentGrade(str, enum.Enum):
    """A student's school year. Fixed, ordered, and not admin-editable (unlike subteams
    / teams), so it lives in an enum like MemberRole rather than a data table.
    Mentors never have a grade. The order below also drives the yearly grade bump."""
    junior_high = "junior_high"
    freshman = "freshman"
    sophomore = "sophomore"
    junior = "junior"
    senior = "senior"
    alumni = "alumni"


GRADE_LABELS: dict[StudentGrade, str] = {
    StudentGrade.junior_high: "Junior High",
    StudentGrade.freshman: "Freshman",
    StudentGrade.sophomore: "Sophomore",
    StudentGrade.junior: "Junior",
    StudentGrade.senior: "Senior",
    StudentGrade.alumni: "Alumni",
}

# The grade progression, low → high. The yearly bump advances each student to the next
# entry; a senior graduates to alumni (and is archived).
GRADE_ORDER: list[StudentGrade] = [
    StudentGrade.junior_high,
    StudentGrade.freshman,
    StudentGrade.sophomore,
    StudentGrade.junior,
    StudentGrade.senior,
    StudentGrade.alumni,
]


def grade_label(grade: Optional[StudentGrade]) -> str:
    return GRADE_LABELS.get(grade, "—") if grade else "—"


# Subteams seeded on first startup — mirrors Tempus's fixed software/design/business
# set, but here they live in a table so admins can add/rename/archive/purge them.
DEFAULT_SUBTEAMS: list[tuple[str, str]] = [
    ("software", "Software"),
    ("design", "Design"),
    ("business", "Business"),
]

# The two FRC teams, seeded on first startup (as Tempus's _seed_teams does).
DEFAULT_TEAMS: list[tuple[int, str]] = [
    (4143, "MARS/WARS"),
    (4423, "MARS' Minions"),
]

# Authorization groups seeded on first startup. Admin-editable (add/rename/archive/purge)
# like subteams, but exposed to the sibling apps in the SSO cookie + read API so each app can
# gate admin sign-in and render role-specific menus. `legion-admin` is special-cased:
# membership grants access to Legion's own /admin (the migration backfills it from the old
# `is_admin` flag). The rest are just metadata Legion hands down — Tempus/Munus decide what
# they mean.
DEFAULT_GROUPS: list[tuple[str, str]] = [
    ("legion-admin", "Legion Admin"),
    ("legion-manager", "Legion Manager"),
    ("tempus-admin", "Tempus Admin"),
    ("munus-admin", "Munus Admin"),
    ("munus-manager", "Munus Manager"),
]


class AppSetting(Base):
    """Small key/value store for runtime-configurable app settings."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Team(Base):
    """An FRC team (4143 / 4423). Keyed for humans by `number`."""
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    members: Mapped[list["Member"]] = relationship("Member", back_populates="team")


class Subteam(Base):
    """A subteam / focus area (software, design, business, …). Admin-editable."""
    __tablename__ = "subteams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    members: Mapped[list["Member"]] = relationship("Member", back_populates="subteam")


# Many-to-many link between members and user groups. A member can hold several groups at
# once (e.g. both "Munus Admin" and "Legion Admin"), unlike the single-FK subteam.
member_user_groups = Table(
    "member_user_groups",
    Base.metadata,
    Column("member_id", ForeignKey("members.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("user_groups.id", ondelete="CASCADE"), primary_key=True),
)


class Group(Base):
    """An admin-editable authorization group (e.g. "Munus Admin", "Legion Admin").

    Surfaced to sibling apps in the `mw_sso` cookie claims + the read API so each app can
    gate admin sign-in and pick which menus to render. Mirrors the Subteam lookup-table
    pattern (stable `slug` for API consumers, human `label`, `is_active` archive flag that
    can then be permanently purged once archived). Table is named `user_groups` to
    sidestep the SQL `GROUPS` keyword.
    """
    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    members: Mapped[list["Member"]] = relationship(
        "Member", secondary=member_user_groups, back_populates="groups"
    )


class Member(Base):
    """The source-of-truth record for one student or mentor.

    `member_code` is Legion's canonical identity: a stable, opaque 8-hex value minted
    once at creation (NOT derived from the name, so a rename never changes it and two
    people with the same name never collide). Tempus / Munus adopt this code as the key
    they sync on.
    """
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, index=True)
    role: Mapped[MemberRole] = mapped_column(
        SAEnum(MemberRole), nullable=False, default=MemberRole.student
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    team_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True
    )
    subteam_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("subteams.id"), nullable=True
    )

    # Shared Slack link. Unique when present (SQLite allows multiple NULLs), so a Slack
    # account maps to at most one member across both apps.
    slack_user_id: Mapped[Optional[str]] = mapped_column(
        String(50), unique=True, nullable=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    # SSO login name (`last.first`, truncated to 4 chars each — see services/username.py).
    # Minted once at creation and stable afterward (an admin can force a new one via the
    # "regenerate" action, but nothing does so automatically on rename).
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # Student-only metadata (null for mentors, gated by role in app logic).
    grade: Mapped[Optional[StudentGrade]] = mapped_column(
        SAEnum(StudentGrade), nullable=True
    )
    parent_guardian_1: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    parent_guardian_2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    # Bumped on every mutation; powers the API's `updated_since` incremental sync.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    team: Mapped[Optional["Team"]] = relationship("Team", back_populates="members")
    subteam: Mapped[Optional["Subteam"]] = relationship("Subteam", back_populates="members")
    # Authorization groups (many-to-many). Exposed to sibling apps via the SSO token + API.
    groups: Mapped[list["Group"]] = relationship(
        "Group", secondary=member_user_groups, back_populates="members"
    )


class AuthStatus(str, enum.Enum):
    """State machine for one SSO login challenge, driven by `/slack/interact` (approve/
    deny) and the polling `/sso/status` endpoint (expired/consumed)."""
    pending = "pending"
    approved = "approved"
    denied = "denied"
    expired = "expired"
    consumed = "consumed"


class AuthRequest(Base):
    """One SSO login attempt: the Slack Approve/Deny challenge for a `username` submitted
    at `/sso/authorize`. Single-use — `/sso/complete` consumes it exactly once.

    `member_id` is null when the submitted username didn't match any active member. We
    still create a row and show the same "check Slack" page either way (see
    `routers/sso.py`) so the login form can't be used to enumerate valid usernames — a
    row with no member simply sits pending until it expires, since nothing can approve it.
    """
    __tablename__ = "auth_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    member_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("members.id"), nullable=True
    )
    app: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    return_to: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    device_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[AuthStatus] = mapped_column(
        SAEnum(AuthStatus), nullable=False, default=AuthStatus.pending
    )
    # Captured from chat.postMessage so `/slack/interact` can edit the DM in place
    # (removes the Approve/Deny buttons once the challenge is decided).
    slack_channel_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    slack_message_ts: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    member: Mapped[Optional["Member"]] = relationship("Member")


class AuthThrottle(Base):
    """Rate-limit / exponential-backoff bucket for the SSO login prompt. `key` is either
    `device:<device_id>` (an anonymous per-browser cap, checked first) or
    `member:<member_id>` (stops a botnet of devices from all hammering one person's
    Slack). One table covers both so the two caps share identical window/backoff logic
    (`services/throttle.py`)."""
    __tablename__ = "auth_throttles"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    # Number of times this key has tripped the limit — grows the backoff exponentially
    # so repeat spamming is punished harder than a one-off burst.
    lock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Append-only record of admin mutations."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(50), nullable=False, default="admin")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "member.create"
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

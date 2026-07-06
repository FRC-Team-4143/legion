import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text,
    ForeignKey, Enum as SAEnum,
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


# Focus groups seeded on first startup — mirrors Tempus's fixed software/design/business
# set, but here they live in a table so admins can add/rename/retire them.
DEFAULT_FOCUS_GROUPS: list[tuple[str, str]] = [
    ("software", "Software"),
    ("design", "Design"),
    ("business", "Business"),
]

# The two FRC teams, seeded on first startup (as Tempus's _seed_teams does).
DEFAULT_TEAMS: list[tuple[int, str]] = [
    (4143, "MARS/WARS"),
    (4423, "MARS' Minions"),
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


class FocusGroup(Base):
    """A subteam / focus area (software, design, business, …). Admin-editable."""
    __tablename__ = "focus_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    members: Mapped[list["Member"]] = relationship("Member", back_populates="focus_group")


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
    focus_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("focus_groups.id"), nullable=True
    )

    # Shared Slack link. Unique when present (SQLite allows multiple NULLs), so a Slack
    # account maps to at most one member across both apps.
    slack_user_id: Mapped[Optional[str]] = mapped_column(
        String(50), unique=True, nullable=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # Mentor "lead" flag (Tempus uses this for escalation DMs); ignored for students.
    is_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    # Bumped on every mutation; powers the API's `updated_since` incremental sync.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    team: Mapped[Optional["Team"]] = relationship("Team", back_populates="members")
    focus_group: Mapped[Optional["FocusGroup"]] = relationship(
        "FocusGroup", back_populates="members"
    )


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

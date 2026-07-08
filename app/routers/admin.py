"""
Admin routes — password-protected web UI for managing the member roster.

Auth: session cookie signed with itsdangerous (same pattern as Tempus / Munus).
"""
import csv
import hmac
import io
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, GRADE_LABELS, GRADE_ORDER, Group, Member, MemberRole,
    StudentGrade, Subteam, Team, grade_label, member_user_groups, role_label,
)
from app.services import audit, throttle
from app.services.members import generate_member_code
from app.services.sso import sso_identity
from app.services.username import assign_unique_username
from app.utils import utc_to_local

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["rolelabel"] = role_label
templates.env.filters["gradelabel"] = grade_label

_signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
_COOKIE = "admin_session"
_MAX_AGE = 60 * 60 * 12  # 12 hours


def _opt_id(raw: Optional[str]) -> Optional[int]:
    """Parse an optional integer form field (e.g. a dropdown), '' -> None."""
    return int(raw) if raw and str(raw).strip() else None


def _opt_grade(raw: Optional[str]) -> Optional[StudentGrade]:
    """Parse an optional grade dropdown value, '' -> None. Assumes a valid enum value."""
    return StudentGrade(raw.strip()) if raw and raw.strip() else None


async def _active_teams(db: AsyncSession):
    return (await db.execute(select(Team).order_by(Team.number))).scalars().all()


async def _active_subteams(db: AsyncSession):
    return (
        await db.execute(
            select(Subteam).where(Subteam.is_active.is_(True))
            .order_by(Subteam.sort_order, Subteam.label)
        )
    ).scalars().all()




async def _slack_owner(db: AsyncSession, slack_uid: str, exclude_id: Optional[int] = None):
    """Return a member already using this Slack id (excluding exclude_id), or None."""
    q = select(Member).where(Member.slack_user_id == slack_uid)
    if exclude_id is not None:
        q = q.where(Member.id != exclude_id)
    return (await db.execute(q)).scalars().first()


# ── Auth helpers ───────────────────────────────────────────────────────────────
#
# Admin access is normally SSO (`mw_sso` cookie carrying the `legion-admin` group). The
# password login below is a break-glass fallback — bootstrapping the very first admin
# (nobody is in `legion-admin` yet) or recovering if Slack is down — so it's kept working
# alongside SSO rather than replaced by it.

def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    if not token:
        return False
    try:
        _signer.loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


_SECTION_LABELS = [
    ("/admin/groups", "User Groups"),
    ("/admin/teams", "Teams"),
    ("/admin/subteams", "Subteams"),
    ("/admin/import", "Import"),
    ("/admin/api", "API Access"),
    ("/admin/audit", "Audit Log"),
    ("/admin/backup", "Backup"),
    ("/admin/members", "Members"),
    ("/admin", "Dashboard"),
]


def _section_label(path: str) -> str:
    """A human label for the section a denied request was aimed at, for the
    forbidden page's message. Order matters — most-specific prefix first, since
    "/admin" is itself a prefix of every other admin path."""
    for prefix, label in _SECTION_LABELS:
        if path.startswith(prefix):
            return label
    return "this page"


def _require_groups(request: Request, groups: set[str]):
    """Gate a route on the SSO identity holding at least one of `groups`, or the
    break-glass password session. Returns a redirect/403 to short-circuit the route
    with, or None to let it proceed."""
    identity = sso_identity(request)
    if identity is not None:
        if groups & set(identity.get("groups") or []) or _is_authenticated(request):
            return None
        return templates.TemplateResponse(
            "admin/forbidden.html",
            {
                "request": request,
                "name": identity.get("name", ""),
                "section": _section_label(request.url.path),
            },
            status_code=403,
        )
    if _is_authenticated(request):
        return None
    return_to = quote(str(request.url.path), safe="")
    return RedirectResponse(f"/sso/authorize?app=legion&return_to={return_to}", status_code=303)


def _require_auth(request: Request):
    """Full admin access: the `legion-admin` group, or the break-glass password
    session. Gates everything security-sensitive — groups, teams/subteams, CSV import,
    API access info, audit log, backup, and destructive/bulk member actions."""
    return _require_groups(request, {"legion-admin"})


def _require_staff(request: Request):
    """Routine roster upkeep: `legion-admin` OR `legion-manager`, or break-glass.
    Deliberately narrow — day-to-day member CRUD only. Managers can't touch group
    membership (that stays admin-only; see routers/admin.py's "User Groups" section)."""
    return _require_groups(request, {"legion-admin", "legion-manager"})


# ── Login / logout ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, error: str = ""):
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": error})


@router.post("/login")
async def admin_login_post(
    request: Request,
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Same DB-backed throttle as the SSO push, keyed by IP instead of device/member —
    # the break-glass password has no other rate limiting of its own.
    client_ip = request.client.host if request.client else "unknown"
    retry_after = await throttle.check_and_record(db, f"admin_login:{client_ip}", None)
    if retry_after is not None:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": f"Too many attempts. Try again in {retry_after}s."},
            status_code=429,
        )
    if not hmac.compare_digest(password, settings.admin_password):
        await audit.record(db, request, "admin.login_failed", "Failed admin login attempt", actor="anonymous")
        await db.commit()
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    await audit.record(db, request, "admin.login", "Admin signed in")
    await db.commit()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(_COOKIE, _signer.dumps("admin"), httponly=True, samesite="lax", secure=True, max_age=_MAX_AGE)
    return response


@router.get("/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(_COOKIE)
    return response


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_staff(request):
        return redirect

    async def _count(*conditions):
        q = select(func.count()).select_from(Member)
        for c in conditions:
            q = q.where(c)
        return await db.scalar(q) or 0

    stats = {
        "students": await _count(Member.is_active.is_(True), Member.role == MemberRole.student),
        "mentors": await _count(Member.is_active.is_(True), Member.role == MemberRole.mentor),
        "teams": await db.scalar(select(func.count()).select_from(Team)) or 0,
        "subteams": await db.scalar(
            select(func.count()).select_from(Subteam).where(Subteam.is_active.is_(True))
        ) or 0,
    }
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {"request": request, "stats": stats, "api_enabled": bool(settings.tempus_api_key or settings.munus_api_key)},
    )


# ── Members ────────────────────────────────────────────────────────────────────

@router.get("/members", response_class=HTMLResponse)
async def admin_members_list(
    request: Request,
    role: str = "",
    show_archived: int = 0,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_staff(request):
        return redirect

    q = (
        select(Member)
        .options(selectinload(Member.team), selectinload(Member.subteam))
        .order_by(Member.name)
    )
    if not show_archived:
        q = q.where(Member.is_active.is_(True))
    role_filter = role if role in ("student", "mentor") else ""
    if role_filter:
        q = q.where(Member.role == MemberRole(role_filter))
    members = (await db.execute(q)).scalars().all()

    return templates.TemplateResponse(
        "admin/members.html",
        {
            "request": request,
            "members": members,
            "teams": await _active_teams(db),
            "subteams": await _active_subteams(db),
            "roles": list(MemberRole),
            "grades": list(StudentGrade),
            "role_filter": role_filter,
            "show_archived": bool(show_archived),
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
        },
    )


@router.post("/members")
async def admin_members_create(
    request: Request,
    name: str = Form(...),
    role: str = Form(...),
    team_id: Optional[str] = Form(None),
    subteam_id: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    grade: Optional[str] = Form(None),
    parent_guardian_1: Optional[str] = Form(None),
    parent_guardian_2: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_staff(request):
        return redirect

    slack_uid = slack_user_id.strip() if slack_user_id else None
    if slack_uid and await _slack_owner(db, slack_uid):
        return RedirectResponse(
            f"/admin/members?error=Slack+ID+{slack_uid}+is+already+linked+to+another+member",
            status_code=303,
        )

    is_student = role == MemberRole.student.value
    clean_name = name.strip()
    member = Member(
        name=clean_name,
        member_code=await generate_member_code(db),
        username=await assign_unique_username(db, clean_name),
        role=MemberRole(role),
        team_id=_opt_id(team_id),
        subteam_id=_opt_id(subteam_id),
        slack_user_id=slack_uid,
        # Group membership is assigned from the User Groups page, not here.
        # Grade + guardians are student-only.
        grade=_opt_grade(grade) if is_student else None,
        parent_guardian_1=(parent_guardian_1.strip() or None) if is_student and parent_guardian_1 else None,
        parent_guardian_2=(parent_guardian_2.strip() or None) if is_student and parent_guardian_2 else None,
    )
    db.add(member)
    await audit.record(db, request, "member.create", f"Created {role} {member.name}", entity_type="member")
    await db.commit()
    return RedirectResponse("/admin/members", status_code=303)


@router.get("/members/{member_id}/edit", response_class=HTMLResponse)
async def admin_members_edit_get(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_staff(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if not member:
        return RedirectResponse("/admin/members", status_code=303)
    return templates.TemplateResponse(
        "admin/member_edit.html",
        {
            "request": request,
            "member": member,
            "teams": await _active_teams(db),
            "subteams": await _active_subteams(db),
            "roles": list(MemberRole),
            "grades": list(StudentGrade),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/members/{member_id}/edit")
async def admin_members_edit_post(
    member_id: int,
    request: Request,
    name: str = Form(...),
    role: str = Form(...),
    team_id: Optional[str] = Form(None),
    subteam_id: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    grade: Optional[str] = Form(None),
    parent_guardian_1: Optional[str] = Form(None),
    parent_guardian_2: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_staff(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if not member:
        return RedirectResponse("/admin/members", status_code=303)

    slack_uid = slack_user_id.strip() if slack_user_id else None
    # Reassigning slack_user_id is the sole SSO identity-binding for this member — a
    # manager could otherwise re-point a privileged member's Slack ID at themselves and
    # self-approve the SSO push. Only full admin may change it, same as groups.
    if slack_uid != member.slack_user_id and (redirect := _require_auth(request)):
        return redirect
    if slack_uid and await _slack_owner(db, slack_uid, exclude_id=member.id):
        return RedirectResponse(
            f"/admin/members/{member_id}/edit?error=Slack+ID+{slack_uid}+is+already+linked+to+another+member",
            status_code=303,
        )

    # member_code and username are Legion's stable identifiers and are intentionally
    # never recomputed here (username has its own explicit "regenerate" action).
    member.name = name.strip()
    member.role = MemberRole(role)
    member.team_id = _opt_id(team_id)
    member.subteam_id = _opt_id(subteam_id)
    member.slack_user_id = slack_uid
    # Group membership is assigned from the User Groups page, not here.
    # Grade + guardians are student-only; clear them if the member is (now) a mentor.
    is_student = member.role == MemberRole.student
    member.grade = _opt_grade(grade) if is_student else None
    member.parent_guardian_1 = (parent_guardian_1.strip() or None) if is_student and parent_guardian_1 else None
    member.parent_guardian_2 = (parent_guardian_2.strip() or None) if is_student and parent_guardian_2 else None
    await audit.record(db, request, "member.edit", f"Edited {member.name}", entity_type="member", entity_id=member.id)
    await db.commit()
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{member_id}/regenerate-username")
async def admin_members_regenerate_username(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Force a new SSO username — e.g. an admin doesn't like the auto-generated one, or
    it collided oddly. Anyone with the old one bookmarked will need the new one."""
    if redirect := _require_staff(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if member:
        old = member.username
        member.username = await assign_unique_username(db, member.name, exclude_id=member.id)
        await audit.record(
            db, request, "member.regenerate_username",
            f"Regenerated username for {member.name} ({old} -> {member.username})",
            entity_type="member", entity_id=member.id,
        )
        await db.commit()
    return RedirectResponse(f"/admin/members/{member_id}/edit", status_code=303)


@router.post("/members/{member_id}/delete")
async def admin_members_delete(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Archive a member (soft delete) — keeps the record and its member_code on file."""
    if redirect := _require_auth(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if member and member.is_active:
        member.is_active = False
        member.archived_at = datetime.utcnow()
        await audit.record(db, request, "member.archive", f"Archived {member.name}", entity_type="member", entity_id=member.id)
        await db.commit()
    return RedirectResponse("/admin/members?show_archived=1", status_code=303)


@router.post("/members/{member_id}/restore")
async def admin_members_restore(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if member and not member.is_active:
        member.is_active = True
        member.archived_at = None
        await audit.record(db, request, "member.restore", f"Restored {member.name}", entity_type="member", entity_id=member.id)
        await db.commit()
    return RedirectResponse("/admin/members?show_archived=1", status_code=303)


@router.post("/members/{member_id}/purge")
async def admin_members_purge(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete an archived member. Only allowed once archived."""
    if redirect := _require_auth(request):
        return redirect
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if member and not member.is_active:
        name = member.name
        await audit.record(
            db, request, "member.purge",
            f"Permanently deleted archived member {name}",
            entity_type="member", entity_id=member_id,
        )
        await db.execute(delete(Member).where(Member.id == member_id))
        await db.commit()
    return RedirectResponse("/admin/members?show_archived=1", status_code=303)


@router.post("/members/bump-grades")
async def admin_members_bump_grades(request: Request, db: AsyncSession = Depends(get_db)):
    """Yearly grade auto-increase: advance every active student one grade. A senior
    graduates to alumni AND is archived (dropped from active rosters / API syncs).
    Students with no grade set are left untouched."""
    if redirect := _require_auth(request):
        return redirect

    students = (
        await db.execute(
            select(Member).where(
                Member.role == MemberRole.student,
                Member.is_active.is_(True),
                Member.grade.is_not(None),
            )
        )
    ).scalars().all()

    bumped = graduated = 0
    for s in students:
        if s.grade == StudentGrade.alumni:
            continue  # already graduated
        if s.grade == StudentGrade.senior:
            s.grade = StudentGrade.alumni
            s.is_active = False
            s.archived_at = datetime.utcnow()
            graduated += 1
        else:
            s.grade = GRADE_ORDER[GRADE_ORDER.index(s.grade) + 1]
            bumped += 1

    if bumped or graduated:
        await audit.record(
            db, request, "member.bump_grades",
            f"Yearly grade increase: {bumped} advanced, {graduated} graduated + archived",
            entity_type="member",
            detail={"bumped": bumped, "graduated": graduated},
        )
        await db.commit()
    msg = f"Grade increase: {bumped} advanced, {graduated} graduated and archived."
    return RedirectResponse(f"/admin/members?message={quote(msg)}", status_code=303)


@router.post("/members/sync-slack")
async def admin_members_sync_slack(request: Request, db: AsyncSession = Depends(get_db)):
    """Push every active member's roster metadata into their Slack custom profile
    fields on demand (mirrors the nightly scheduled sync)."""
    if redirect := _require_auth(request):
        return redirect
    from app.services import slack_profile

    if not settings.slack_bot_token:
        return RedirectResponse(
            f"/admin/members?message={quote('Slack sync skipped: no SLACK_BOT_TOKEN configured.')}",
            status_code=303,
        )
    result = await slack_profile.sync_all_profiles(db)
    await audit.record(
        db, request, "member.sync_slack",
        f"Slack profile sync: {result['sent']} sent, {result['skipped']} skipped, {result['failed']} failed",
        entity_type="member",
        detail=result,
    )
    await db.commit()
    msg = f"Slack sync: {result['sent']} sent, {result['skipped']} skipped, {result['failed']} failed."
    return RedirectResponse(f"/admin/members?message={quote(msg)}", status_code=303)


# ── Teams ──────────────────────────────────────────────────────────────────────

@router.get("/teams", response_class=HTMLResponse)
async def admin_teams_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    teams = (
        await db.execute(
            select(Team, func.count(Member.id))
            .outerjoin(Member, Member.team_id == Team.id)
            .group_by(Team.id)
            .order_by(Team.number)
        )
    ).all()
    return templates.TemplateResponse(
        "admin/teams.html",
        {
            "request": request,
            "teams": [{"team": t, "count": c} for t, c in teams],
            "error": request.query_params.get("error"),
        },
    )


@router.post("/teams")
async def admin_teams_create(
    request: Request,
    number: str = Form(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    if not number.strip().isdigit():
        return RedirectResponse("/admin/teams?error=Team+number+must+be+numeric", status_code=303)
    num = int(number.strip())
    existing = (await db.execute(select(Team).where(Team.number == num))).scalars().first()
    if existing:
        return RedirectResponse(f"/admin/teams?error=Team+{num}+already+exists", status_code=303)
    db.add(Team(number=num, name=name.strip()))
    await audit.record(db, request, "team.create", f"Created team {num} ({name.strip()})", entity_type="team")
    await db.commit()
    return RedirectResponse("/admin/teams", status_code=303)


@router.post("/teams/{team_id}/edit")
async def admin_teams_edit(
    team_id: int,
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalars().first()
    if team:
        team.name = name.strip()
        await audit.record(db, request, "team.edit", f"Renamed team {team.number} to {team.name}", entity_type="team", entity_id=team.id)
        await db.commit()
    return RedirectResponse("/admin/teams", status_code=303)


# ── Subteams ───────────────────────────────────────────────────────────────────

@router.get("/subteams", response_class=HTMLResponse)
async def admin_subteams_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    groups = (
        await db.execute(
            select(Subteam, func.count(Member.id))
            .outerjoin(Member, Member.subteam_id == Subteam.id)
            .group_by(Subteam.id)
            .order_by(Subteam.sort_order, Subteam.label)
        )
    ).all()
    return templates.TemplateResponse(
        "admin/subteams.html",
        {
            "request": request,
            "groups": [{"group": g, "count": c} for g, c in groups],
            "error": request.query_params.get("error"),
        },
    )


def _slugify(label: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in label.strip().lower()).strip("-")


@router.post("/subteams")
async def admin_subteams_create(
    request: Request,
    label: str = Form(...),
    slug: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    the_slug = (slug.strip().lower() if slug and slug.strip() else _slugify(label))
    if not the_slug:
        return RedirectResponse("/admin/subteams?error=Invalid+slug", status_code=303)
    existing = (await db.execute(select(Subteam).where(Subteam.slug == the_slug))).scalars().first()
    if existing:
        return RedirectResponse(f"/admin/subteams?error=Slug+{the_slug}+already+exists", status_code=303)
    max_order = await db.scalar(select(func.max(Subteam.sort_order))) or 0
    db.add(Subteam(slug=the_slug, label=label.strip(), sort_order=max_order + 1))
    await audit.record(db, request, "subteam.create", f"Created subteam {label.strip()}", entity_type="subteam")
    await db.commit()
    return RedirectResponse("/admin/subteams", status_code=303)


@router.post("/subteams/{group_id}/edit")
async def admin_subteams_edit(
    group_id: int,
    request: Request,
    label: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    group = (await db.execute(select(Subteam).where(Subteam.id == group_id))).scalars().first()
    if group:
        group.label = label.strip()
        await audit.record(db, request, "subteam.edit", f"Renamed subteam to {group.label}", entity_type="subteam", entity_id=group.id)
        await db.commit()
    return RedirectResponse("/admin/subteams", status_code=303)


@router.post("/subteams/{group_id}/toggle")
async def admin_subteams_toggle(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Archive / restore a subteam without deleting it (keeps historical assignments)."""
    if redirect := _require_auth(request):
        return redirect
    group = (await db.execute(select(Subteam).where(Subteam.id == group_id))).scalars().first()
    if group:
        group.is_active = not group.is_active
        state = "restored" if group.is_active else "archived"
        await audit.record(db, request, "subteam.toggle", f"{state.capitalize()} subteam {group.label}", entity_type="subteam", entity_id=group.id)
        await db.commit()
    return RedirectResponse("/admin/subteams", status_code=303)


@router.post("/subteams/{group_id}/purge")
async def admin_subteams_purge(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete an archived subteam. Only allowed once archived. Any members
    still assigned to it are detached (their subteam is cleared, not the member)."""
    if redirect := _require_auth(request):
        return redirect
    group = (await db.execute(select(Subteam).where(Subteam.id == group_id))).scalars().first()
    if group and not group.is_active:
        label = group.label
        await db.execute(update(Member).where(Member.subteam_id == group.id).values(subteam_id=None))
        await audit.record(
            db, request, "subteam.purge", f"Permanently deleted archived subteam {label}",
            entity_type="subteam", entity_id=group_id,
        )
        await db.execute(delete(Subteam).where(Subteam.id == group_id))
        await db.commit()
    return RedirectResponse("/admin/subteams", status_code=303)


# ── User Groups ────────────────────────────────────────────────────────────────

@router.get("/groups", response_class=HTMLResponse)
async def admin_groups_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    rows = (
        await db.execute(
            select(Group, func.count(member_user_groups.c.member_id))
            .outerjoin(member_user_groups, member_user_groups.c.group_id == Group.id)
            .group_by(Group.id)
            .order_by(Group.label)
        )
    ).all()
    return templates.TemplateResponse(
        "admin/groups.html",
        {
            "request": request,
            "groups": [{"group": g, "count": c} for g, c in rows],
            "error": request.query_params.get("error"),
        },
    )


@router.post("/groups")
async def admin_groups_create(
    request: Request,
    label: str = Form(...),
    slug: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    the_slug = (slug.strip().lower() if slug and slug.strip() else _slugify(label))
    if not the_slug:
        return RedirectResponse("/admin/groups?error=Invalid+slug", status_code=303)
    existing = (await db.execute(select(Group).where(Group.slug == the_slug))).scalars().first()
    if existing:
        return RedirectResponse(f"/admin/groups?error=Slug+{the_slug}+already+exists", status_code=303)
    max_order = await db.scalar(select(func.max(Group.sort_order))) or 0
    db.add(Group(slug=the_slug, label=label.strip(), sort_order=max_order + 1))
    await audit.record(db, request, "group.create", f"Created group {label.strip()}", entity_type="group")
    await db.commit()
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/groups/{group_id}/edit")
async def admin_groups_edit(
    group_id: int,
    request: Request,
    label: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    group = (await db.execute(select(Group).where(Group.id == group_id))).scalars().first()
    if group:
        group.label = label.strip()
        await audit.record(db, request, "group.edit", f"Renamed group to {group.label}", entity_type="group", entity_id=group.id)
        await db.commit()
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/groups/{group_id}/toggle")
async def admin_groups_toggle(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Archive / restore a group without deleting it (keeps existing assignments — an
    archived group is only hidden from new member-form checkboxes)."""
    if redirect := _require_auth(request):
        return redirect
    group = (await db.execute(select(Group).where(Group.id == group_id))).scalars().first()
    if group:
        group.is_active = not group.is_active
        state = "restored" if group.is_active else "archived"
        await audit.record(db, request, "group.toggle", f"{state.capitalize()} group {group.label}", entity_type="group", entity_id=group.id)
        await db.commit()
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/groups/{group_id}/purge")
async def admin_groups_purge(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete an archived group. Only allowed once archived. Membership rows
    are cleaned up automatically — an ORM-level delete so the many-to-many `member_user_
    groups` rows are removed too, rather than left dangling."""
    if redirect := _require_auth(request):
        return redirect
    group = (
        await db.execute(
            select(Group).options(selectinload(Group.members)).where(Group.id == group_id)
        )
    ).scalars().first()
    if group and not group.is_active:
        label = group.label
        # Purging drops this group's slug from every member's `groups` — bump each one
        # so sibling apps' incremental sync notices (see add_member's comment above).
        now = datetime.utcnow()
        for member in group.members:
            member.updated_at = now
        await audit.record(
            db, request, "group.purge", f"Permanently deleted archived group {label}",
            entity_type="group", entity_id=group_id,
        )
        await db.delete(group)
        await db.commit()
    return RedirectResponse("/admin/groups", status_code=303)


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def admin_group_detail(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """A group's own page: its current members, plus a form to add another one. This is
    the only place membership is managed — not the member create/edit forms."""
    if redirect := _require_auth(request):
        return redirect
    group = (
        await db.execute(
            select(Group).options(selectinload(Group.members)).where(Group.id == group_id)
        )
    ).scalars().first()
    if not group:
        return RedirectResponse("/admin/groups", status_code=303)
    member_ids = {m.id for m in group.members}
    all_active = (
        await db.execute(
            select(Member).where(Member.is_active.is_(True)).order_by(Member.name)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/group_detail.html",
        {
            "request": request,
            "group": group,
            "members": sorted(group.members, key=lambda m: m.name),
            "addable_members": [m for m in all_active if m.id not in member_ids],
            "error": request.query_params.get("error"),
        },
    )


@router.post("/groups/{group_id}/members")
async def admin_group_add_member(
    group_id: int,
    request: Request,
    member_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    group = (
        await db.execute(
            select(Group).options(selectinload(Group.members)).where(Group.id == group_id)
        )
    ).scalars().first()
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if group and member and member not in group.members:
        group.members.append(member)
        # The member_user_groups association row is what actually changes here, not
        # any column on `members` — so SQLAlchemy's onupdate wouldn't otherwise bump
        # updated_at. Sibling apps' incremental sync (?updated_since=) relies on it
        # to notice a pure group-membership change, so bump it explicitly.
        member.updated_at = datetime.utcnow()
        await audit.record(
            db, request, "group.add_member", f"Added {member.name} to {group.label}",
            entity_type="group", entity_id=group.id,
        )
        await db.commit()
    return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)


@router.post("/groups/{group_id}/members/{member_id}/remove")
async def admin_group_remove_member(
    group_id: int, member_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect
    group = (
        await db.execute(
            select(Group).options(selectinload(Group.members)).where(Group.id == group_id)
        )
    ).scalars().first()
    member = (await db.execute(select(Member).where(Member.id == member_id))).scalars().first()
    if group and member and member in group.members:
        group.members.remove(member)
        member.updated_at = datetime.utcnow()  # see add_member's comment above
        await audit.record(
            db, request, "group.remove_member", f"Removed {member.name} from {group.label}",
            entity_type="group", entity_id=group.id,
        )
        await db.commit()
    return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)


# ── CSV Import ─────────────────────────────────────────────────────────────────

def _grade_key(s: str) -> str:
    return s.strip().lower().replace(" ", "_").replace("-", "_")


# Accept a grade CSV cell as either the enum value ("junior_high") or its label
# ("Junior High"), case- and separator-insensitively.
_GRADE_BY_KEY: dict[str, StudentGrade] = {g.value: g for g in StudentGrade}
_GRADE_BY_KEY.update({_grade_key(v): g for g, v in GRADE_LABELS.items()})


def _parse_grade(value: str) -> Optional[StudentGrade]:
    """Return a StudentGrade for a CSV cell, or None if blank. Raises ValueError if the
    non-empty value isn't a recognized grade."""
    key = _grade_key(value)
    if not key:
        return None
    grade = _GRADE_BY_KEY.get(key)
    if grade is None:
        raise ValueError(value.strip())
    return grade


@router.get("/import", response_class=HTMLResponse)
async def admin_import_get(request: Request):
    if redirect := _require_auth(request):
        return redirect
    return templates.TemplateResponse("admin/import.html", {"request": request})


@router.post("/import", response_class=HTMLResponse)
async def admin_import_post(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    created, updated, errors = [], [], []
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    # Preload lookup maps by natural key.
    teams = {t.number: t for t in (await db.execute(select(Team))).scalars().all()}
    subteams_by_slug = {g.slug: g for g in (await db.execute(select(Subteam))).scalars().all()}

    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=2):  # row 1 = header
        role_str = (row.get("role") or "").strip().lower()
        name = (row.get("name") or "").strip()
        team_str = (row.get("team_number") or "").strip()
        slack_uid = (row.get("slack_user_id") or "").strip() or None
        subteam_str = (row.get("subteam") or "").strip().lower()
        grade_str = (row.get("grade") or "").strip()
        parent1 = (row.get("parent_guardian_1") or "").strip() or None
        parent2 = (row.get("parent_guardian_2") or "").strip() or None

        if not role_str or not name:
            errors.append({"row": i, "reason": "Missing role or name", "data": dict(row)})
            continue
        if role_str not in ("student", "mentor"):
            errors.append({"row": i, "reason": f"Unknown role '{role_str}'", "data": dict(row)})
            continue

        try:
            grade = _parse_grade(grade_str)
        except ValueError:
            errors.append({"row": i, "reason": f"Unknown grade '{grade_str}'", "data": dict(row)})
            continue

        team = None
        if team_str:
            if not team_str.isdigit() or int(team_str) not in teams:
                errors.append({"row": i, "reason": f"Unknown team '{team_str}'", "data": dict(row)})
                continue
            team = teams[int(team_str)]

        st = None
        if subteam_str:
            if subteam_str not in subteams_by_slug:
                errors.append({"row": i, "reason": f"Unknown subteam '{subteam_str}'", "data": dict(row)})
                continue
            st = subteams_by_slug[subteam_str]

        # Slack id must not collide with a different member.
        if slack_uid:
            owner = (await db.execute(select(Member).where(Member.slack_user_id == slack_uid))).scalars().first()
            if owner and owner.name.lower() != name.lower():
                errors.append({"row": i, "reason": f"Slack id {slack_uid} already used by {owner.name}", "data": dict(row)})
                continue

        role = MemberRole(role_str)
        is_student = role == MemberRole.student
        existing = (await db.execute(select(Member).where(func.lower(Member.name) == name.lower()))).scalars().first()
        if existing:
            existing.role = role
            existing.team_id = team.id if team else None
            existing.subteam_id = st.id if st else None
            if slack_uid:
                existing.slack_user_id = slack_uid
            existing.grade = grade if is_student else None
            existing.parent_guardian_1 = parent1 if is_student else None
            existing.parent_guardian_2 = parent2 if is_student else None
            updated.append(name)
        else:
            # Group membership is deliberately not importable from CSV — granting admin
            # access (any group) is a privileged action that always goes through the edit
            # form, so a roster upload can never quietly hand out permissions.
            db.add(Member(
                name=name,
                member_code=await generate_member_code(db),
                username=await assign_unique_username(db, name),
                role=role,
                team_id=team.id if team else None,
                subteam_id=st.id if st else None,
                slack_user_id=slack_uid,
                grade=grade if is_student else None,
                parent_guardian_1=parent1 if is_student else None,
                parent_guardian_2=parent2 if is_student else None,
            ))
            created.append(name)

    if created or updated:
        await audit.record(
            db, request, "import.csv",
            f"CSV import: {len(created)} created, {len(updated)} updated, {len(errors)} error(s)",
            entity_type="import",
            detail={"created": created, "updated": updated, "error_count": len(errors), "filename": file.filename},
        )
    await db.commit()

    return templates.TemplateResponse(
        "admin/import.html",
        {"request": request, "created": created, "updated": updated, "errors": errors},
    )


# ── API info ───────────────────────────────────────────────────────────────────

@router.get("/api", response_class=HTMLResponse)
async def admin_api_info(request: Request):
    if redirect := _require_auth(request):
        return redirect
    return templates.TemplateResponse(
        "admin/api.html",
        {
            "request": request,
            "tempus_api_key": settings.tempus_api_key,
            "munus_api_key": settings.munus_api_key,
            "api_enabled": bool(settings.tempus_api_key or settings.munus_api_key),
        },
    )


# ── Audit log ──────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    per_page = 50
    page = max(1, page)
    total = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    entries = (
        await db.execute(
            select(AuditLog).order_by(AuditLog.timestamp.desc())
            .offset((page - 1) * per_page).limit(per_page)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/audit.html",
        {"request": request, "entries": entries, "total": total, "page": page, "total_pages": total_pages},
    )


# ── Backup ─────────────────────────────────────────────────────────────────────

@router.get("/backup", response_class=HTMLResponse)
async def admin_backup(request: Request):
    if redirect := _require_auth(request):
        return redirect
    from app.services import backup
    return templates.TemplateResponse(
        "admin/backup.html",
        {
            "request": request,
            "is_sqlite": backup.is_sqlite(),
            "backups": backup.list_backups(),
            "message": request.query_params.get("message"),
            "result": request.query_params.get("result"),
        },
    )


@router.get("/backup/download")
async def admin_backup_download(request: Request):
    if redirect := _require_auth(request):
        return redirect
    import os
    import tempfile
    from app.services import backup
    if not backup.is_sqlite():
        return RedirectResponse("/admin/backup", status_code=303)
    tmp = os.path.join(tempfile.gettempdir(), f"legion-{datetime.now():%Y%m%d-%H%M%S}.db")
    backup.create_snapshot(tmp)

    def _iter():
        with open(tmp, "rb") as f:
            yield from f
        try:
            os.remove(tmp)
        except OSError:
            pass

    filename = f"legion-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
    return StreamingResponse(
        _iter(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/restore")
async def admin_backup_restore(
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    from app.services import backup
    if confirm.strip().upper() != "RESTORE":
        return RedirectResponse("/admin/backup?result=error&message=Type+RESTORE+to+confirm", status_code=303)
    ok, message = backup.stage_restore(await file.read())
    if ok:
        await audit.record(db, request, "backup.restore_staged", "Staged a database restore", entity_type="backup")
        await db.commit()
    result = "success" if ok else "error"
    return RedirectResponse(f"/admin/backup?result={result}&message={quote(message)}", status_code=303)

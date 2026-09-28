"""
Outbound graduation-event push — the one place Legion calls forward into a sibling app
instead of waiting to be pulled from. Fired once, at the moment the Yearly Grade
Increase action (`routers/admin.py`) archives a senior; the Alumni app takes it from
there (it owns everything about that person from that point on — see its own
services/survey.py). Legion itself keeps no record that this happened: the "is this
person a graduate" signal lives entirely in the fact that this push fired, not in any
stored field on Member.

Best-effort and non-blocking, matching every other outbound integration in this repo
(Slack sends, profile sync): a failed push must never fail the bump-grades request
itself, since Legion has already committed the archive by the time this runs. The
bump-grades summary banner reports the failure count, and the audit log's `detail` JSON
already carries every field below, so a failed push isn't silently unrecoverable — an
admin can hand-add the person in Alumni from that record.
"""
import logging

import httpx

from app.config import settings
from app.models import Member

log = logging.getLogger(__name__)

# A single long-lived client (reused connections), mirroring services/health.py and
# routers/slack_dispatch.py's module-level clients.
_client = httpx.AsyncClient(timeout=10.0)


async def notify_alumni(member: Member, graduation_year: int) -> bool:
    """POST the newly-graduated member to Alumni's intake endpoint. Returns whether it
    was accepted. Never raises — a delivery failure doesn't undo the graduation Legion
    already committed, any more than a failed Slack DM would.

    `member.team`/`member.subteam` must already be eager-loaded by the caller (this
    runs inside the same async session that did the archiving) — a lazy load here would
    raise under async SQLAlchemy.
    """
    if not settings.alumni_base_url or not settings.alumni_push_api_key:
        log.warning(
            "Alumni push skipped for %s: ALUMNI_BASE_URL/ALUMNI_PUSH_API_KEY not configured.",
            member.name,
        )
        return False
    payload = {
        "member_code": member.member_code,
        "name": member.name,
        "slack_user_id": member.slack_user_id,
        "team_number": member.team.number if member.team else None,
        "subteam_label": member.subteam.label if member.subteam else None,
        "graduation_year": graduation_year,
    }
    try:
        resp = await _client.post(
            f"{settings.alumni_base_url}/api/graduates",
            json=payload,
            headers={"X-API-Key": settings.alumni_push_api_key},
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.error("Failed to push graduate %s to Alumni: %s", member.name, e)
        return False

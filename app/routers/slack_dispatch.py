"""
Shared Slack interactivity dispatcher.

Tempus, Munus, and Legion share one Slack app (see `config.py`'s comment on
`tempus_interact_url` for why), so Slack's single Interactivity Request URL points
here instead of directly at any one app's `/slack/interact`. This route knows nothing
about any app's business logic — it just reads the payload's `action_id` (block
actions) or `callback_id` (modal submissions) and forwards the original request,
unmodified, to whichever app owns that namespace. Each app still verifies the Slack
signature itself on the forwarded copy, so this adds no new trust boundary.

Unmatched payloads (unrecognized action/callback id, or a type we don't route, e.g.
a future shortcut) are swallowed with a 200 — matching the "unknown action -> no-op"
convention already used by every app's own `/slack/interact`.
"""
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Response

from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/slack")

# action_id (block_actions) -> owning app's /slack/interact URL.
_ACTION_ID_EXACT = {
    "edit_contributor": "tempus",
    "edit_present": "tempus",
    "edit_distraction": "tempus",
    "hours_quick": "munus",
    "hours_adjust": "munus",
    "review_edit": "munus",
    "submission_approve": "munus",
    "submission_reject": "munus",
    "opp_dashboard": "munus",
    "sso_approve": "legion",
    "sso_deny": "legion",
}
_ACTION_ID_PREFIXES = {
    "edit_select_": "tempus",
}
# view_submission callback_id -> owning app.
_CALLBACK_ID_EXACT = {
    "log_hours": "munus",
    "review_hours": "munus",
}

_client = httpx.AsyncClient(timeout=10.0)


def _target_url(app_name: str) -> str:
    return {
        "tempus": settings.tempus_interact_url,
        "munus": settings.munus_interact_url,
        "legion": settings.legion_interact_url,
    }[app_name]


def resolve_target(payload: dict) -> Optional[str]:
    """The owning app's `/slack/interact` URL for this payload, or None if unrecognized."""
    ptype = payload.get("type")
    if ptype == "block_actions":
        action_id = (payload.get("actions") or [{}])[0].get("action_id", "")
        if action_id in _ACTION_ID_EXACT:
            return _target_url(_ACTION_ID_EXACT[action_id])
        for prefix, app_name in _ACTION_ID_PREFIXES.items():
            if action_id.startswith(prefix):
                return _target_url(app_name)
        return None
    if ptype == "view_submission":
        callback_id = payload.get("view", {}).get("callback_id", "")
        if callback_id in _CALLBACK_ID_EXACT:
            return _target_url(_CALLBACK_ID_EXACT[callback_id])
        return None
    return None


@router.post("/dispatch")
async def slack_dispatch(request: Request):
    # .body() first: it caches the raw bytes, so the subsequent .form() parses from
    # that cache instead of re-reading (and exhausting) the ASGI request stream.
    body = await request.body()  # exact bytes Slack signed — forwarded unchanged
    form = await request.form()
    try:
        payload = json.loads(form.get("payload", ""))
    except json.JSONDecodeError:
        return Response(status_code=200)

    target = resolve_target(payload)
    if target is None:
        log.warning(
            "No interactivity route for type=%s action_id=%s callback_id=%s",
            payload.get("type"),
            (payload.get("actions") or [{}])[0].get("action_id"),
            payload.get("view", {}).get("callback_id"),
        )
        return Response(status_code=200)

    forward_headers = {
        "Content-Type": request.headers.get("content-type", "application/x-www-form-urlencoded"),
        "X-Slack-Request-Timestamp": request.headers.get("x-slack-request-timestamp", ""),
        "X-Slack-Signature": request.headers.get("x-slack-signature", ""),
    }
    try:
        upstream = await _client.post(target, content=body, headers=forward_headers)
    except httpx.HTTPError as e:
        log.error("Failed forwarding interactivity payload to %s: %s", target, e)
        return Response(status_code=200)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )

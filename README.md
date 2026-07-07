# Legion

Shared **student & mentor metadata** service for FRC teams 4143 (MARS/WARS) and 4423
(MARS' Minions).

Legion is the single **source of truth** for the team roster — each person's name, role
(student or mentor), team, focus group, Slack ID, and active status. The sibling apps
**Tempus** (attendance) and **Munus** (volunteer hours) read this data from Legion's
JSON API instead of each keeping their own drifting copy.

FastAPI + async SQLAlchemy + Jinja2 + SQLite, matching the Tempus/Munus stack. Runs on
port **8002**.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then edit ADMIN_PASSWORD / SESSION_SECRET / LEGION_API_KEY
uvicorn app.main:app --reload --port 8002
```

Open <http://localhost:8002/admin> — sign in with SSO (see below) or, as a break-glass
fallback, `ADMIN_PASSWORD`.

## What's inside

- **Admin UI** (`/admin`) — manage members, teams, and subteams; CSV import; audit
  log; SQLite backup/restore; a yearly "grade increase" action and a Slack profile sync.
  Dark theme shared with Tempus/Munus.
- **Read API** (`/api`, `X-API-Key`-protected) — the contract Tempus/Munus sync from.
- **SSO** (`/sso`, `/slack/interact`) — Legion is the identity provider for the
  MARS/WARS apps: username + a Slack Approve/Deny push, no passwords. Details below.

### API

All endpoints require the `X-API-Key` header (matched against `LEGION_API_KEY`). If no
key is configured the API returns `503` (fails closed).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/members` | List members. Filters: `role`, `team_number`, `active`, `updated_since` |
| GET | `/api/members/{member_code}` | One member by canonical code |
| GET | `/api/teams` | All teams |
| GET | `/api/subteams` | All subteams |

```bash
curl -H "X-API-Key: $LEGION_API_KEY" \
  "http://localhost:8002/api/members?role=student&active=true"
```

`updated_since` (an ISO-8601 timestamp) combined with each member's `updated_at` lets a
consumer pull only what changed since its last sync.

### Canonical member code

Each member gets a stable, opaque 8-hex `member_code` at creation. It is **never**
recomputed from the name, so renames don't break it and duplicate names don't collide —
this is the key Tempus and Munus sync on.

## Testing

```bash
pytest
```

In-memory SQLite, real queries (no DB mocking).

## Deployment

Deployed with Tempus/Munus from the `apps-infra` repo (Docker Compose + nginx) on
container port 8002. See that repo's `docker-compose.yml`, `nginx.conf`, and `deploy.sh`.

## Student metadata

Students carry a **Grade** (Junior High → Freshman → Sophomore → Junior → Senior →
Alumni) and up to two **Parent/Guardian** names. Grade + guardians are student-only —
they're ignored for mentors. Guardian names are **not** exposed on the read API; `grade`
is (as `grade`). The Members page has a **Yearly Grade Increase** button that advances
every active student one grade; seniors graduate to **Alumni** and are archived.

## Slack profile sync

Legion can push a member's **Team**, **School Year** (grade), **Subteam**, and
**Parent/Guardian 1 & 2** into their Slack custom profile fields — a nightly scheduled
job plus a **Sync Slack Profiles** button on the Members page. Guardian fields are only
sent for students. Configure `SLACK_BOT_TOKEN` (blank = sync disabled) and, optionally,
`SLACK_SYNC_TIME` / `SLACK_SYNC_DAY`.

> **Token:** editing *another* user's profile via `users.profile.set` requires an admin
> **user** token (`xoxp-…`) with `users.profile:write`. A normal bot token can only edit
> its own profile, so `SLACK_BOT_TOKEN` must be an admin user token.

## CSV import format

Columns: `role` (student|mentor, required), `name` (required), `team_number` (optional,
must be an existing team), `subteam` (optional, a subteam slug), `slack_user_id`
(optional, unique), `is_lead` (optional, mentors only), `grade` (optional, students only —
a grade name like `Sophomore`), `parent_guardian_1` / `parent_guardian_2` (optional,
students only). Existing members are matched by name (case-insensitive) and updated; new
members get a fresh `member_code` and `username`. `is_admin` is deliberately not
importable — granting Legion admin access always goes through the edit form.

## Single sign-on

Legion is the SSO provider for the MARS/WARS apps. Every member gets an auto-generated,
stable `username` (`last.first`, each part truncated to 4 characters and lowercased —
e.g. "Alexander Hamilton" -> `hami.alex`; collisions get a numeric suffix). There are no
member passwords: signing in means entering your `username`, then approving a Slack DM
push. Legion's own `/admin` runs on this too (gated on the member's `is_admin` flag),
with the original `ADMIN_PASSWORD` login kept as a break-glass fallback for bootstrapping
the first admin or recovering if Slack is down.

**Flow:** `GET/POST /sso/authorize?app=<id>&return_to=<url>` (username form) ->
`GET /sso/status/{nonce}` (the "check Slack" page polls this) -> `GET /sso/complete/{nonce}`
(consumes the approval, sets the cookie, redirects to `return_to`). The Approve/Deny tap
itself lands on `POST /slack/interact`. If a valid session cookie already exists,
`/sso/authorize` skips straight to redirecting back — real single sign-on across apps.

**The cookie:** `mw_sso`, an `itsdangerous`-signed token (see `services/sso.py`) carrying
`member_code`, `username`, `name`, `role`, `team_number`, `is_lead`, `is_admin`, and
`slack_user_id`. Set with `Domain=SSO_COOKIE_DOMAIN` (e.g. `.marswars.org`) so one login
covers every subdomain; `GET /sso/logout` clears it (single logout). A sibling app
verifies it **locally** with the shared `SSO_SECRET` — no callback to Legion needed:

```python
from itsdangerous import URLSafeTimedSerializer
signer = URLSafeTimedSerializer(SSO_SECRET, salt="mw-sso")
claims = signer.loads(request.cookies["mw_sso"], max_age=SSO_SESSION_TTL)  # raises if invalid/expired
```

On failure, redirect to `f"{LEGION_BASE_URL}/sso/authorize?app=<id>&return_to=<current-url>"`.
(Tempus/Munus don't implement this client side yet — this is the contract for when they do.)

**Abuse protection:** login attempts are rate-limited per browser (`mw_device` cookie)
and per matched member — `SSO_RATE_MAX` attempts per `SSO_RATE_WINDOW` seconds, then a
lockout of `SSO_BACKOFF_BASE` seconds that multiplies by `SSO_BACKOFF_MULTIPLIER` on each
repeat offense (`services/throttle.py`). Submitting an unknown username gets the exact
same "check Slack" response as a real one (no DM is sent) so the login form can't be used
to enumerate valid usernames.

**Slack app setup:** SSO needs a bot token distinct from `SLACK_BOT_TOKEN` (which holds
an admin *user* token, only good for the profile-sync's `users.profile.set`). Configure
`SLACK_AUTH_BOT_TOKEN` (`xoxb-…`, scopes `chat:write` + `im:write`) and `SLACK_SIGNING_SECRET`.

In practice this is the **same Slack app Tempus and Munus already use** — despite each
app's own README saying to create a separate one, all three actually share one bot
token today. That's fine for *sending* messages (any number of services can use the
same token), but Slack allows only **one Interactivity Request URL per app**, and all
three services have their own `/slack/interact` wanting real button clicks. Rather than
fight that, the shared app's Interactivity Request URL should point at Legion's
**`/slack/dispatch`** (`routers/slack_dispatch.py`) — a stateless relay with no
business logic of its own that reads each payload's `action_id`/`callback_id` and
forwards the original, unmodified request to whichever app's own `/slack/interact`
actually owns it (`tempus_interact_url` / `munus_interact_url` / `legion_interact_url`
in `config.py`). Each app still verifies the Slack signature itself on the forwarded
copy, so the dispatcher adds no new trust boundary. Slash commands don't need this —
each slash command has its own independently configurable Request URL already.

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
cp .env.example .env            # then edit ADMIN_PASSWORD / SESSION_SECRET / TEMPUS_API_KEY / MUNUS_API_KEY
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

All endpoints require the `X-API-Key` header, matched against either `TEMPUS_API_KEY` or
`MUNUS_API_KEY` — each consumer has its own key, so leaking or rotating one never affects
the other. If neither is configured the API returns `503` (fails closed).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/members` | List members. Filters: `role`, `team_number`, `active`, `updated_since` |
| GET | `/api/members/{member_code}` | One member by canonical code |
| GET | `/api/teams` | All teams |
| GET | `/api/subteams` | All subteams |
| GET | `/api/groups` | All authorization groups (slug → label, active flag) |

```bash
curl -H "X-API-Key: $TEMPUS_API_KEY" \
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

Deployed with Tempus/Munus from the `apps-infra` repo (Docker Compose + Nginx Proxy Manager) on
container port 8002. See that repo's `docker-compose.yml` and `deploy.sh`.

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
(optional, unique), `grade` (optional, students only —
a grade name like `Sophomore`), `parent_guardian_1` / `parent_guardian_2` (optional,
students only). Existing members are matched by name (case-insensitive) and updated; new
members get a fresh `member_code` and `username`. Group membership is deliberately not
importable — granting admin access (any group) always goes through the edit form, so a
roster upload can never quietly hand out permissions.

## Single sign-on

Legion is the SSO provider for the MARS/WARS apps. Every member gets an auto-generated,
stable `username` (`last.first`, each part truncated to 4 characters and lowercased —
e.g. "Alexander Hamilton" -> `hami.alex`; collisions get a numeric suffix). There are no
member passwords: signing in means entering your `username`, then approving a Slack DM
push. Legion's own `/admin` runs on this too (gated on membership in the `legion-admin`
group — see **User groups** below), with the original `ADMIN_PASSWORD` login kept as a
break-glass fallback for bootstrapping the first admin (before anyone is in `legion-admin`)
or recovering if Slack is down.

**Flow:** `GET/POST /sso/authorize?app=<id>&return_to=<url>` (username form) ->
`GET /sso/status/{nonce}` (the "check Slack" page polls this) -> `GET /sso/complete/{nonce}`
(consumes the approval, sets the cookie, redirects to `return_to`). The Approve/Deny tap
itself lands on `POST /slack/interact`. If a valid session cookie already exists,
`/sso/authorize` skips straight to redirecting back — real single sign-on across apps.

**One-tap variant for a sibling app that already knows the member:** a Slack slash
command or button click already tells the calling app *who* it is (the Slack user id in
the payload) — making them type their Legion username too, just to re-derive an identity
the caller already has, is pure friction. `POST /sso/challenge` (`X-API-Key`-authenticated,
same trust boundary as the read API) skips the form: given a `member_code`, it creates the
`AuthRequest` and sends the Slack push directly, returning `{nonce, expires_at}` (a longer
`SSO_API_CHALLENGE_TTL` than the form flow's `SSO_CHALLENGE_TTL`, since there's a
human-reads-a-Slack-message delay before any browser starts polling). The caller then sends
its own user to `GET /sso/pending/{nonce}` — the same "check Slack" page `POST
/sso/authorize` renders, just addressable by nonce alone; `/sso/status` and
`/sso/complete` are unchanged and shared by both flows. See Munus's `services/legion_auth.py`
+ `GET /enter` for a full consumer-side implementation.

**The cookie:** `mw_sso`, an `itsdangerous`-signed token (see `services/sso.py`) carrying
`member_code`, `username`, `name`, `role`, `team_number`, `groups` (a list of
group slugs — see below), and `slack_user_id`. Set with `Domain=SSO_COOKIE_DOMAIN`
(e.g. `.marswars.org`) so one login
covers every subdomain; `GET /sso/logout` clears it (single logout). A sibling app
verifies it **locally** with the shared `SSO_SECRET` — no callback to Legion needed:

```python
from itsdangerous import URLSafeTimedSerializer
signer = URLSafeTimedSerializer(SSO_SECRET, salt="mw-sso")
claims = signer.loads(request.cookies["mw_sso"], max_age=SSO_SESSION_TTL)  # raises if invalid/expired
```

On failure, redirect to `f"{LEGION_BASE_URL}/sso/authorize?app=<id>&return_to=<current-url>"`.
Both Tempus (`/admin` only) and Munus (`/admin` **and** the student portal) implement this
client-side already — see their `services/sso.py`.

### User groups

Legion carries admin-editable **user groups** — e.g. `Tempus Admin`, `Munus Admin`,
`Munus Manager`, `Legion Admin`, `Legion Manager` — that a member can hold several of at
once. They're managed entirely under **Admin → User Groups**: create / rename / archive a
group there (exactly like Subteams — a stable `slug` the apps check plus a human `label`;
archiving a group only hides it from new assignments and keeps it on existing members),
then open a group's own page to see its members and add/remove people. Once archived, a
group (or subteam) can be **permanently deleted** — any members still holding it just lose
it (subteams clear the member's `subteam`; groups drop the membership row) rather than
being blocked or themselves deleted. Membership is **not** set from the member create/edit
forms — a member's groups only ever change from the group's page. The five groups above
are seeded on first start; add your own freely.

A member's group **slugs** are handed to every app on two surfaces so each app can decide
what to allow and what to show:

- **The `mw_sso` cookie** — the `groups` claim, read live on every request.
- **The read API** — `groups` on each `/api/members` entry, with `/api/groups` resolving
  a slug to its label + active flag.

Two intended uses: **gate admin sign-in** (an app allows its admin area only for members
whose `groups` contains its slug — e.g. Munus checks `"munus-admin"`) and **render
different menus** per user. Legion eats its own dog food with two tiers on its *own*
`/admin`: `legion-admin` gets everything, while `legion-manager` is deliberately narrow —
routine roster upkeep (the dashboard, and member list/create/edit/regenerate-username)
but nothing security-sensitive. A manager **cannot** touch group membership, create/edit
groups or subteams/teams, run a CSV import, view the API-access/audit-log/backup pages, or
perform destructive/bulk member actions (delete, restore, purge, bump grades, Slack sync)
— all of those stay `legion-admin`-only. There is no separate `is_admin` flag — group
membership is the one authorization concept, with the break-glass `ADMIN_PASSWORD` login
as the only bypass (and it always has full admin access, matching `legion-admin`).

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
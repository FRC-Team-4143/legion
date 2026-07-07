# Legion — Codebase Guide

Shared **student & mentor metadata** service for FRC teams 4143 (MARS/WARS) and 4423
(MARS' Minions). Legion is the **source of truth** for who's on the team: name, role
(student/mentor), team, focus group, Slack ID, active status. Its sibling apps —
**Tempus** (attendance) and **Munus** (volunteer hours) — read this roster over a
read-only JSON API instead of each maintaining their own copy.

FastAPI + SQLAlchemy (async) + Jinja2 + SQLite. Intentionally mirrors the Tempus/Munus
stack, dark styling, and conventions, but is a fully separate app with its own DB and
systemd/Docker service (**port 8002**). Nothing is imported across the three projects.

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8002
```

Requires a `.env` file (see `.env.example`). Key vars: `ADMIN_PASSWORD`, `SESSION_SECRET`,
`LEGION_API_KEY` (the shared secret Tempus/Munus present as `X-API-Key`; blank = API off),
`SSO_SECRET`, `SLACK_AUTH_BOT_TOKEN` + `SLACK_SIGNING_SECRET` (SSO login — see below).

## Testing

```bash
pytest
```

In-memory SQLite with async fixtures via `pytest-asyncio`. **Do not mock the database** —
tests hit a real (in-memory) DB to catch query bugs.

## Project Layout

```
app/
  main.py            # FastAPI app, router wiring, lifespan (init_db + scheduler)
  config.py          # Settings (pydantic-settings, reads .env)
  database.py        # Engine, session, init_db(), seed teams + focus groups
  models.py          # ORM models + MemberRole labels/defaults
  utils.py           # Timezone helpers + ISO datetime parse/format
  routers/
    admin.py         # SSO(+break-glass password)-protected management UI
    api.py           # Read-only JSON API (X-API-Key protected) — the sync contract
    sso.py           # SSO endpoints: authorize / status / complete / logout
    slack.py         # Inbound Slack interactivity — SSO Approve/Deny button clicks
  services/
    members.py       # member_code generation + JSON serializers (shared by API + admin)
    username.py      # SSO username generation (last.first) + collision handling
    sso.py           # mw_sso cookie mint/verify + device cookie + return_to allow-list
    slack_auth.py    # Outbound SSO challenge DM (Approve/Deny) + message update
    throttle.py      # SSO login rate limit / exponential backoff
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    scheduler.py     # APScheduler: nightly backup
    audit.py         # Append-only mutation log
  templates/admin/   # Jinja templates (extend admin/base.html; dark theme)
  templates/sso/     # Standalone SSO pages (username entry, "check Slack" polling)
```

## Key Conventions

### Datetimes
All datetimes in the database are **naive UTC** (`app/utils.py`): `utc_to_local(dt)` for
display, `local_to_utc(dt)` for DB queries, `now_utc()` for "now". `parse_iso_utc` /
`isoformat_utc` handle the API's `updated_since` filter and `updated_at` serialization.

### Canonical identity — `member_code`
Every member has a **stable, opaque 8-hex `member_code`** minted once at creation
(`services/members.generate_member_code`, `secrets.token_hex(4)`). It is **never**
recomputed from the name — so a rename never changes it and duplicate names never
collide. This is the key Tempus/Munus sync on. (Contrast the siblings' legacy
`sha256(name)[:8]` codes, which break on rename.) `slack_user_id` is the other shared
link and is **unique when set**.

### Members are unified
Students and mentors are one `members` table discriminated by `role` (`MemberRole`).
Team and focus group are nullable FKs. `is_lead` is a mentor-only flag. `grade`
(`StudentGrade` enum) and `parent_guardian_1/2` are student-only — like `is_lead`, they
live on every row but app logic gates them to the right role (clears them for mentors).
Soft-delete via `is_active` + `archived_at`, matching the siblings. The **Yearly Grade
Increase** admin action (`/admin/members/bump-grades`) walks `GRADE_ORDER`; a senior
graduates to `alumni` and is archived. `grade` is exposed on the read API; guardian
names are deliberately **not** (PII, and no consumer needs them).

### Focus groups & teams are data, not enums
`focus_groups` and `teams` are admin-editable tables (unlike Tempus's hardcoded
`FocusCategory` enum), seeded on first startup (`_seed_teams`, `_seed_focus_groups`)
with 4143/4423 and software/design/business. Focus groups are retired (not deleted) to
preserve historical assignments and keep slugs stable for API consumers.

### Read API (`routers/api.py`)
Read-only, `X-API-Key`-gated (fails closed with 503 if no key configured). Endpoints:
`/api/members` (filters `role`, `team_number`, `active`, `updated_since`),
`/api/members/{member_code}`, `/api/teams`, `/api/focus-groups`. `updated_since` +
`Member.updated_at` (bumped on every mutation) enable incremental pull-sync. Serializers
live in `services/members.py` so admin and API agree on the wire shape.

### Auth
Legion is the SSO provider for the MARS/WARS apps (see the dedicated section below).
`/admin` is gated on a valid `mw_sso` cookie with `is_admin: true`; the original
password login (`admin_session` itsdangerous cookie, 12h) still works as a break-glass
fallback — see `routers/admin.py`'s `_require_auth`. There is still no *sibling-app*
consumption of the SSO cookie yet (Tempus/Munus) — this repo only provides the
provider side and the documented contract (README "Single sign-on" section).

### Single sign-on (`routers/sso.py`, `routers/slack.py`, `services/sso.py`)
Passwordless: a member enters their auto-generated `username` (`services/username.py`,
`last.first` truncated to 4 chars each, collisions suffixed); Legion DMs their Slack an
Approve/Deny push (`services/slack_auth.py`, needs `SLACK_AUTH_BOT_TOKEN` — a real bot
token, distinct from the profile-sync's admin *user* token `SLACK_BOT_TOKEN`) and, once
approved via `POST /slack/interact`, sets the `mw_sso` cookie (`services/sso.py`,
`itsdangerous`-signed, `Domain=SSO_COOKIE_DOMAIN`). Every sibling app is meant to verify
that cookie locally with the shared `SSO_SECRET` — no callback to Legion. Login attempts
are rate-limited + exponentially backed off per browser and per member
(`services/throttle.py`); an unmatched username gets the identical "check Slack"
response as a real one (no enumeration). See `AuthRequest` / `AuthThrottle` in
`models.py` for the storage shape and `AuthStatus` for the challenge state machine.

### Database migrations
No Alembic. Add a `def _migration(conn)` guarded by `inspect(conn)` in `database.py` and
call it from `init_db()`, mirroring the siblings. First example:
`_migration_add_member_metadata` (adds `grade` + `parent_guardian_1/2`). New columns
that don't need to survive existing data (e.g. `username`/`is_admin` on `Member`) can
just be declared on the model and picked up by `create_all()` — no migration needed
until there's a real deployed database to preserve.

### Slack profile sync (`services/slack_profile.py`)
One-way push of member metadata into Slack **custom profile fields** (Team, School Year,
Focus Group, Parent/Guardian 1 & 2 — guardians for students only). Mirrors the siblings'
cached `AsyncWebClient` + swallow-and-log pattern. Driven by a nightly APScheduler job
(`job_sync_slack_profiles`) and a manual `/admin/members/sync-slack` button. Gated on
`slack_bot_token` + `updates_enabled`. Field IDs are constants in the service. Requires
an **admin user token** (`xoxp-…`) — a bot token can only edit its own profile.

## UI Conventions
Single dark theme shared with Tempus/Munus (`#0a0a0a` bg, `#111111` panels, accent red
`#cc2200`, borders `#2a1a1a`). Admin pages extend `admin/base.html` (Bootstrap 5 with
kiosk-color overrides). Don't add Bootstrap default light classes.

## Deployment
Deployed alongside Tempus/Munus from the `apps-infra` repo (Docker Compose + nginx).
Legion runs on container port **8002**; see `apps-infra/docker-compose.yml`, `nginx.conf`,
and `deploy.sh`.

## Consuming Legion (Tempus / Munus — future work)
Each app keeps its local `Student`/`Mentor` tables (preserving FKs to sessions/signups/
submissions) and adds a `member_code`/`legion_id` link plus a sync job that pulls
`/api/members?updated_since=…` and upserts. Not yet implemented — this repo only provides
the source of truth and the API. Separately, once an app wants to consume **SSO**, it
verifies the `mw_sso` cookie locally with the shared `SSO_SECRET` and redirects to
Legion's `/sso/authorize` on a miss — no code from this repo is imported, just the
cookie contract documented in README.md's "Single sign-on" section.

## Architecture decision: data flows one way (down)

Legion pushes **metadata** down to the apps; the apps own their **domain data**
(Tempus = attendance/hours, Munus = volunteer hours/submissions) and never write it
back. Do **not** add write-back — i.e. don't let Tempus/Munus push aggregates (hour
totals, attendance counts, requirement progress) *up* into Legion. That was considered
and rejected because it:
- reverses the clean one-directional flow and makes Legion a write target for two
  independent writers (needs write auth, conflict/staleness handling);
- forces Legion to store **derived, duplicated** numbers that go stale the instant the
  owning app changes, creating a "which number is right?" ambiguity;
- grows Legion's schema per-metric and blurs ownership — reintroducing exactly the
  duplication Legion exists to eliminate;
- strips the app-side context (Tempus status multipliers, Munus approval state) that
  gives those numbers meaning.

**If a unified per-person profile is wanted later, aggregate at read-time, not
write-time.** A profile view fans out and queries each app's *live* read endpoint by
`member_code` when the page loads — nothing is stored in or written back to Legion, so
every app stays authoritative and no number is ever stale. The only new work that needs
is a small read endpoint on Tempus/Munus that returns a person's aggregate by
`member_code`.

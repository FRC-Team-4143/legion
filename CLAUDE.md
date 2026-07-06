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
`LEGION_API_KEY` (the shared secret Tempus/Munus present as `X-API-Key`; blank = API off).

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
    admin.py         # Password-protected management UI (members/teams/focus/import/backup)
    api.py           # Read-only JSON API (X-API-Key protected) — the sync contract
  services/
    members.py       # member_code generation + JSON serializers (shared by API + admin)
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    scheduler.py     # APScheduler: nightly backup
    audit.py         # Append-only mutation log
  templates/admin/   # Jinja templates (extend admin/base.html; dark theme)
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
Team and focus group are nullable FKs. `is_lead` is a mentor-only flag. Soft-delete via
`is_active` + `archived_at`, matching the siblings.

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
Admin only — password login setting the `admin_session` itsdangerous cookie (12h), same
pattern as Tempus/Munus. There is no per-member login; Legion is a back-office tool.

### Database migrations
No Alembic. Add a `def _migration(conn)` guarded by `inspect(conn)` in `database.py` and
call it from `init_db()`, mirroring the siblings.

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
the source of truth and the API.

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

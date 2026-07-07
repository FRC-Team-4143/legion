# Legion — Codebase Guide

Shared **student & mentor metadata** service for FRC teams 4143 (MARS/WARS) and 4423
(MARS' Minions). Legion is the **source of truth** for who's on the team: name, role
(student/mentor), team, focus group, Slack ID, active status. Its sibling apps —
**Tempus** (attendance) and **Munus** (volunteer hours) — read this roster over a
read-only JSON API instead of each maintaining their own copy.

FastAPI + SQLAlchemy (async) + Jinja2 + SQLite. Intentionally mirrors the Tempus/Munus
stack, dark styling, and conventions, but is a fully separate app with its own DB and
Docker service (**port 8002**). Nothing is imported across the three projects.

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
  database.py        # Engine, session, init_db(), seed teams + subteams + groups
  models.py          # ORM models + MemberRole labels/defaults
  utils.py           # Timezone helpers + ISO datetime parse/format
  routers/
    admin.py         # SSO(+break-glass password)-protected management UI
    api.py           # Read-only JSON API (X-API-Key protected) — the sync contract
    sso.py           # SSO endpoints: authorize / status / complete / logout
    slack.py         # Inbound Slack interactivity — SSO Approve/Deny button clicks
    slack_dispatch.py # /slack/dispatch — shared interactivity relay (see below)
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
Team and focus group are nullable FKs. `grade` (`StudentGrade` enum) and
`parent_guardian_1/2` are student-only — they live on every row but app logic gates them
to the right role (clears them for mentors). There is no mentor "lead" flag (removed —
Tempus has its own `is_lead` for escalation DMs, but it's local to Tempus's own Mentor
table, not synced from Legion). Soft-delete via `is_active` + `archived_at`, matching the
siblings. The **Yearly Grade
Increase** admin action (`/admin/members/bump-grades`) walks `GRADE_ORDER`; a senior
graduates to `alumni` and is archived. `grade` is exposed on the read API; guardian
names are deliberately **not** (PII, and no consumer needs them).

### Subteams & teams are data, not enums
`subteams` and `teams` are admin-editable tables (unlike Tempus's hardcoded
`FocusCategory` enum), seeded on first startup (`_seed_teams`, `_seed_subteams`) with
4143/4423 and software/design/business. A subteam is archived (`is_active=False`), not
deleted, while it's still in use — preserves historical assignments and keeps slugs
stable for API consumers. Once archived it can be permanently purged
(`admin_subteams_purge`); any members still assigned to it are detached (their
`subteam_id` is cleared), not deleted themselves.

### Read API (`routers/api.py`)
Read-only, `X-API-Key`-gated (fails closed with 503 if no key configured). Endpoints:
`/api/members` (filters `role`, `team_number`, `active`, `updated_since`),
`/api/members/{member_code}`, `/api/teams`, `/api/subteams`, `/api/groups`. `updated_since` +
`Member.updated_at` (bumped on every mutation) enable incremental pull-sync. Serializers
live in `services/members.py` so admin and API agree on the wire shape.

### Auth
Legion is the SSO provider for the MARS/WARS apps (see the dedicated section below).
`/admin` has two tiers, both gated by `_require_groups(request, {…})` in
`routers/admin.py` (checks the `mw_sso` cookie's `groups` claim, or falls back to the
break-glass `admin_session` password cookie, 12h): `_require_auth` needs `legion-admin`
and covers everything, while `_require_staff` accepts `legion-admin` **or**
`legion-manager` and is used only on the dashboard and member list/create/edit/
regenerate-username routes. Every other route (groups — including membership,
teams/subteams, CSV import, API-access/audit-log/backup pages, and destructive/bulk
member actions like delete/restore/purge/bump-grades/sync-slack) stays on
`_require_auth`, i.e. `legion-manager`-only members get a 403 there. There is no
`is_admin` boolean — it was replaced by the `legion-admin` group (a one-time
`database.py` migration folds any old `is_admin=1` rows in, then drops the column).
There is still no *sibling-app* consumption of the SSO cookie yet (Tempus/Munus) — this
repo only provides the provider side and the documented contract (README "Single
sign-on" section).

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

**One-tap variant for sibling apps (`POST /sso/challenge`, `GET /sso/pending/{nonce}`):**
a caller that already knows *which* member it's dealing with (e.g. Munus resolving a
Slack slash command's user id locally) skips the username form entirely —
`X-API-Key`-authenticated (`require_api_key` from `routers/api.py`, same trust boundary
as the roster API), it creates the `AuthRequest` and fires the Slack push directly,
keyed by `member_code` instead of `username`. Uses `SSO_API_CHALLENGE_TTL` (longer than
the form flow's `SSO_CHALLENGE_TTL`) since there's a human-reads-a-Slack-message delay
before any browser starts polling. `GET /sso/pending/{nonce}` just renders the existing
`sso/pending.html` addressed by nonce alone — `/sso/status` and `/sso/complete` are
unchanged and shared by both flows. See Munus's `services/legion_auth.py` for the
consumer side.

### User groups (`models.Group`, `member_user_groups`, `routers/admin.py`)
Admin-editable authorization groups (`legion-admin`, `munus-admin`, `tempus-admin`, …),
many-to-many with `Member` (a person can hold several). Same lookup-table pattern as
`Subteam` — stable `slug` + human `label` + `sort_order` + `is_active` archive flag —
create/rename/archive under `/admin/groups`, seeded from `DEFAULT_GROUPS`. An archived
group can then be permanently purged (`admin_groups_purge`) — an ORM-level `db.delete`
so the `member_user_groups` join rows go with it (no dangling references left behind).
Subteam purge (`admin_subteams_purge`) does the equivalent for the plain FK case: it
nulls out `Member.subteam_id` for anyone still assigned before deleting the row. Both
purges require the row to already be archived — same "archive first, then delete" two-step
as members' own delete/restore/purge. Membership itself is managed on a group's own page
(`GET /admin/groups/{id}`, `group_detail.html`):
lists current members with a per-row "Remove" and a select-and-add form for anyone not
already in it (`POST /admin/groups/{id}/members` / `.../members/{member_id}/remove`).
The member create/edit forms have **no** group controls — this is the only place
membership changes, and the members list intentionally shows no per-group badges (keeps
that table from getting noisy as more groups are added). A member's group **slugs** are
handed to the apps on two surfaces so each can gate admin sign-in and render
role-specific menus: the `mw_sso` cookie's `groups` claim (`services/sso.py`) and
`serialize_member`'s `groups` list on the read API (with `/api/groups` resolving
slug→label). All assigned slugs are emitted regardless of `is_active` — retiring a group
only blocks *new* assignment. Group membership is Legion's single authorization concept
(no `is_admin`); it is deliberately **not** importable from CSV — granting any admin
group always goes through `/admin/groups`. `legion-admin` governs Legion's own `/admin`.

### Shared Slack interactivity dispatch (`routers/slack_dispatch.py`)
Tempus, Munus, and Legion actually share one Slack app in production (identical bot
token + signing secret across all three, despite each README saying to create a
separate one) — outbound sends are fine to share, but Slack allows only **one**
Interactivity Request URL per app, and all three want real button clicks. The shared
app points that one URL at Legion's `POST /slack/dispatch` instead, which holds no
business logic — it reads `action_id` (block actions) or `callback_id` (modal
submissions) and forwards the original, byte-for-byte request to whichever app's own
`/slack/interact` owns that namespace (`tempus_interact_url` / `munus_interact_url` /
`legion_interact_url` in `config.py`; Legion's own `sso_*` actions loop back to its own
`/slack/interact`). Each app still verifies the Slack signature itself on the forwarded
copy — the dispatcher adds no new trust boundary and needs no signing secret of its
own. Unrecognized action/callback ids are swallowed with a 200, matching every app's
own "unknown action → no-op" convention. Slash commands don't route through this —
each slash command has its own independently configurable Request URL already.

### Database migrations
No Alembic. Add a `def _migration(conn)` guarded by `inspect(conn)` in `database.py` and
call it from `init_db()`, mirroring the siblings. Examples:
`_migration_add_member_metadata` (adds `grade` + `parent_guardian_1/2`) and
`_migration_move_is_admin_to_group` (folds the retired `is_admin` column into the
`legion-admin` group, then `DROP COLUMN`s it). Brand-new **tables** (e.g. `user_groups`,
`member_user_groups`) are created automatically by `create_all()` — no migration needed;
only *altering* an existing table (add/rename/drop column) needs a `_migration`. New
columns on `Member` that don't need to survive existing data can just be declared on the
model and picked up by `create_all()`.

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
Deployed alongside Tempus/Munus from the `apps-infra` repo (Docker Compose + Nginx Proxy Manager).
Legion runs on container port **8002**; see `apps-infra/docker-compose.yml` and `deploy.sh`.

## Consuming Legion (Tempus / Munus)
Both sibling apps keep their local `Student`/`Mentor` tables (preserving FKs to
sessions/signups/submissions) with a `member_code` link plus a sync job (`services/
legion_sync.py` in each) that pulls `/api/members?updated_since=…` and upserts — see
their own CLAUDE.md files for the details. No code from this repo is imported by either;
they consume the documented API/cookie contract only (README.md's "Single sign-on"
section). SSO consumption: verify the `mw_sso` cookie locally with the shared
`SSO_SECRET`, redirect to `/sso/authorize` on a miss. Tempus only gates `/admin` this
way; Munus additionally puts its whole student portal on `mw_sso` (no portal-specific
cookie at all) and uses the one-tap `POST /sso/challenge` / `GET /sso/pending/{nonce}`
pair (see `routers/sso.py`) so a Slack-originated click doesn't need a typed username.

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

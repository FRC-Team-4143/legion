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
`TEMPUS_API_KEY` + `MUNUS_API_KEY` (each consumer's own secret for the `X-API-Key` header;
both blank = API off), `SSO_SECRET`, `SLACK_AUTH_BOT_TOKEN` + `SLACK_SIGNING_SECRET` (SSO
login — see below).

## Testing

```bash
pytest
```

In-memory SQLite with async fixtures via `pytest-asyncio`. **Do not mock the database** —
tests hit a real (in-memory) DB to catch query bugs.

## Manual / visual verification (screenshots)

This sandboxed environment has no seeded dev database and blocks outbound access to
`cdn.jsdelivr.net` (Bootstrap/Bootstrap Icons) — check `curl -sS
$HTTPS_PROXY/__agentproxy/status` if requests through it fail; a `connect_rejected` for
that host is a policy denial, not a bug. Getting an actual screenshot of an admin page
(Playwright's Chromium is pre-installed at `/opt/pw-browsers/chromium`; `pip install
playwright` into the venv if the Python package isn't there yet) needs a few workarounds:

1. **Local Bootstrap assets, served via Playwright route interception** —
   `registry.npmjs.org` *is* reachable. Download once:
   ```bash
   mkdir -p /tmp/cdn_assets && cd /tmp/cdn_assets
   curl -sSL https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.3.tgz | tar xz
   curl -sSL https://registry.npmjs.org/bootstrap-icons/-/bootstrap-icons-1.11.3.tgz | tar xz
   ```
   (both extract into `package/` and merge — `dist/css`, `dist/js`, and `font/` all end
   up under the same directory). In the Playwright script, `context.route(re.compile(r"cdn\.jsdelivr\.net"),
   handler)` and `route.fulfill(...)` the CSS/JS/`.woff`/`.woff2` requests from those
   local files. Skipping this makes every page render unstyled (no dark theme, no
   icons) even though the HTML/JS is completely correct — don't mistake that for a
   real bug.

2. **A seeded temp DB** — point `DATABASE_URL` at a scratch sqlite file
   (`sqlite+aiosqlite:////tmp/legion_demo.db`) and run a short script, with
   `PYTHONPATH=/home/user/legion` (a bare `python script.py` doesn't put the repo root
   on `sys.path`), that calls `Base.metadata.create_all` and inserts a few rows.

3. **A valid `mw_sso` cookie, minted directly** — no need to walk the real SSO flow
   (or the break-glass password), even for Legion's own `/admin`:
   ```python
   from itsdangerous import URLSafeTimedSerializer
   signer = URLSafeTimedSerializer("<same secret as SSO_SECRET below>", salt="mw-sso")
   cookie = signer.dumps({"member_code": "test0001", "username": "test.admin",
       "name": "Test Admin", "role": "mentor", "team_number": 4143,
       "groups": ["legion-admin"], "slack_user_id": None})
   ```
   `services/sso.py` builds its `itsdangerous` signer **at import time** from
   `settings.sso_secret` — mutating `settings.sso_secret` after the app/server has
   already started has no effect. Set `SSO_SECRET` in the server process's own
   environment *before* it starts, not by patching the already-imported `settings`
   object in-process.

4. **Run and drive it**: `DATABASE_URL=... SSO_SECRET=... uvicorn app.main:app --port
   8002` in the background, then Playwright `add_cookies([{"name": "mw_sso", "value":
   cookie, "domain": "127.0.0.1", "path": "/"}])` before navigating. Talk to
   `127.0.0.1` directly, not `localhost`, and don't route local traffic through the
   session's `HTTPS_PROXY` — a plain-HTTP request through it 405s ("non-CONNECT
   request"); only the CDN-lookalike requests in step 1 need interception.

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
    slack.py         # Inbound Slack interactivity — SSO Approve/Deny clicks, graduation
                     # survey button + modal submission
    slack_dispatch.py # /slack/dispatch — shared interactivity relay (see below)
  services/
    members.py       # member_code generation + JSON serializers (shared by API + admin)
    username.py      # SSO username generation (last.first) + collision handling
    sso.py           # mw_sso cookie mint/verify + device cookie + return_to allow-list
    slack_auth.py    # Outbound SSO challenge DM (Approve/Deny) + message update/delete
    graduation_survey.py # Outbound post-graduation survey DM + its Block Kit modal
    throttle.py      # SSO login rate limit / exponential backoff
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    scheduler.py     # APScheduler: nightly backup, SSO DM cleanup sweep
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
Team and focus group are nullable FKs. `grade` (`StudentGrade` enum), `parent_guardian_1/2`,
and `graduation_year` live on every row but app logic gates them to the right role.
`parent_guardian_1/2` are strictly student-only — cleared the moment a member's role
becomes mentor (in the create/edit routes and CSV import) — and hold the guardian's own
Slack user ID (e.g. `U01ABC123`), not their name; see "Slack profile sync" below for why.
`grade` and `graduation_year`, by contrast, are **not** cleared on a student->mentor role
switch: a former student who becomes a mentor (a common FRC pattern — alumni returning to
mentor) keeps their grade/graduation history rather than losing it the instant their role
flips. They're also directly settable on a mentor row (form or CSV) for the same reason —
e.g. backfilling a longtime mentor who's also a program alum. The member detail modal
(`templates/admin/members.html`) shows Grade/Graduation Year for a mentor only when
actually set, so an ordinary mentor's card isn't cluttered with empty rows. There is no
mentor "lead" flag (removed —
Tempus has its own `is_lead` for escalation DMs, but it's local to Tempus's own Mentor
table, not synced from Legion). Soft-delete via `is_active` + `archived_at`, matching the
siblings. The **Yearly Grade
Increase** admin action (`/admin/members/bump-grades`) walks `GRADE_ORDER`; a senior
graduates to `alumni`, is archived, and gets `graduation_year` set to the current
calendar year — deliberately **not** auto-backfilled for alumni who graduated before
this field existed (no reliable record of when past bumps ran), though an admin can
enter it by hand via the edit form or CSV import. `grade` and `graduation_year` are
exposed on the read API; guardian IDs are deliberately **not** (PII, and no consumer
needs them).

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
`legion-manager` and is used on the dashboard, member list/create/edit/
regenerate-username routes, and member archive (`POST /members/{id}/delete` — soft
delete, sets `is_active=False`). Every other route (groups — including membership,
teams/subteams, CSV import, API-access/audit-log/backup pages, and restore/purge/
bump-grades/sync-slack) stays on `_require_auth`, i.e. `legion-manager`-only members get
a 403 there — a manager can archive a member but never permanently delete one. Within
the archive route itself, a `legion-manager` without full `legion-admin` additionally
can't archive another member who holds `legion-admin` or `legion-manager`
(`_is_privileged_member` in `routers/admin.py`) — otherwise a manager could sideline a
peer or an admin's own account. A full admin (or break-glass session) is exempt from
that check. There is no `is_admin` boolean — it was replaced by the `legion-admin` group
(a one-time `database.py` migration folds any old `is_admin=1` rows in, then drops the
column).
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
A background sweep (`scheduler.job_purge_challenge_dms` →
`slack_auth.purge_old_challenge_dms`) deletes each challenge DM **and** its
`AuthRequest` row once older than `SSO_DM_RETENTION_MINUTES` (default 15), so the auth
bot's DM thread doesn't fill up and `auth_requests` stays bounded; the delete needs no
scope beyond the `chat:write` the bot already uses to post/edit the DM.

**Magic links (`GET /sso/link`) — how Slack-delivered links sign people in.** Slack's
in-app browser uses ephemeral cookie storage, on iOS *and* Android, and cannot be
detected server-side (it sends a stock mobile Safari UA — a UA-sniffing "bounce them to
the real browser" attempt was tried and reverted). So `mw_sso` never survives from one
Slack tap to the next, and every tap used to cost a fresh Approve/Deny push. Instead, a
sibling app signs a token naming the member (`services/sso.make_link_token`, salt
`mw-sso-link` — **deliberately distinct from the cookie's `mw-sso` salt** so the two can
never be swapped) into the links it puts in DMs and ephemeral replies; `/sso/link`
verifies it, **re-resolves the member live** (so archiving someone kills every link
already sent to them), re-validates `return_to` through `allowed_return_to`, and mints
the cookie. Reusable until `SSO_LINK_TTL` — **deliberately equal to `SSO_SESSION_TTL`
(12h)**, since a link left in a shared computer's browser history must not outlive the
session it created, or the next person at that machine can replay it the next day
(there's a test pinning `sso_link_ttl <= sso_session_ttl`). Expiring is cheap: the route
falls back to the normal sign-in page, carrying the original destination across via
`expired_link_return_to` so the user still lands where they meant to. Reusable rather
than single-use, since the
same DM link gets tapped repeatedly and Slack's own link-unfurl fetcher would otherwise
burn it before the human ever tapped. This is sound because Slack already authenticated
the recipient of a DM/ephemeral reply; it is **only** valid for per-person channels, as
a link is a bearer credential. The minted cookie is non-privileged by construction —
`make_link_sso_token` emits `groups: []` plus `via: "link"`, and every app's admin gate
(including `_require_groups` here) treats `via == "link"` as a step-up-to-real-sign-in
rather than a 403 — so a leaked link can never reach any `/admin`.

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

### Post-graduation survey (`models.GraduationSurvey`, `services/graduation_survey.py`)
The Yearly Grade Increase action (`/admin/members/bump-grades`) sends a graduating
senior (one with a `slack_user_id` on file — students with none are just skipped and
counted, since Legion has no other contact info for them) a DM asking where they're
headed after high school, what they're studying/their job title, and whether they'd
like to stay in touch (with an email if so). Answers are collected via a Slack **modal**
(`graduation_survey.survey_modal_view`), not free-text replies — nothing in this
workspace listens to Slack message events, so a modal reuses existing interactivity
plumbing instead of standing up new infrastructure. This is Legion's first use of
`view_submission` (previously `/slack/interact` only ever handled `block_actions`, for
the SSO buttons above), so **both** the button click (`grad_survey_start`) and the modal
submit (`grad_survey_submit`) are registered in `slack_dispatch.py`'s routing tables —
skipping either makes Slack's click/submit silently no-op. The email field is always
shown (Block Kit can't conditionally reveal it based on the Yes/No answer without an
extra round trip) but is required only when "stay in touch" is Yes, enforced server-side
via Slack's `response_action: errors` shape so an incomplete submission reopens the
modal with an inline error instead of silently dropping it. `GraduationSurvey` mirrors
`AuthRequest`'s `slack_channel_id`/`slack_message_ts` shape (edits the DM to a "Thanks!"
once answered) but is a single best-effort round trip with no nonce/expiry. Answers are
DB-only for now — no admin page yet; the bump-grades summary banner shows send/skip
counts as its only visibility.

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

### `/legion` slash command (`routers/slack.py`)
A bare one-tap magic link to Legion's own home page (`/`), mirroring Tempus's
`/tempus` and Munus's `/munus`. Legion mints the token locally
(`services/sso.make_link_url`, wrapping `make_link_token`) rather than over HTTP like
the sibling apps' `legion_auth.make_link_url` do — Legion doesn't need the round trip
since it's the token issuer. Redemption is the same `GET /sso/link` every magic link
uses regardless of which app minted it. Listed in `services/home.py`'s
`_APP_COMMANDS["Legion"]`, so it only shows on the home page's "Slack Commands"
section for someone who already gets a Legion tile — i.e. `legion-admin`/
`legion-manager` today, since `tiles_for` has no personal (non-staff) Legion tile.
The command itself has no such restriction — any active member can run it.

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
Focus Group, Parent/Guardian 1 & 2 — guardians for students only). The guardian fields
carry the guardian's own Slack user ID rather than a name, sent as-is into a Slack
"person" custom field so it renders as a linked profile. Mirrors the siblings'
cached `AsyncWebClient` + swallow-and-log pattern. Manual-only — triggered solely by the
`/admin/members/sync-slack` button, no scheduled job. Gated on `slack_bot_token`. Field
IDs are constants in the service. Requires an **admin user token** (`xoxp-…`) — a bot
token can only edit its own profile.

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

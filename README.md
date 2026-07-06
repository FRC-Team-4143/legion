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

Open <http://localhost:8002/admin> and sign in with `ADMIN_PASSWORD`.

## What's inside

- **Admin UI** (`/admin`) — manage members, teams, and focus groups; CSV import; audit
  log; SQLite backup/restore. Dark theme shared with Tempus/Munus.
- **Read API** (`/api`, `X-API-Key`-protected) — the contract Tempus/Munus sync from.

### API

All endpoints require the `X-API-Key` header (matched against `LEGION_API_KEY`). If no
key is configured the API returns `503` (fails closed).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/members` | List members. Filters: `role`, `team_number`, `active`, `updated_since` |
| GET | `/api/members/{member_code}` | One member by canonical code |
| GET | `/api/teams` | All teams |
| GET | `/api/focus-groups` | All focus groups |

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

## CSV import format

Columns: `role` (student|mentor, required), `name` (required), `team_number` (optional,
must be an existing team), `focus_group` (optional, a focus-group slug), `slack_user_id`
(optional, unique), `is_lead` (optional, mentors only). Existing members are matched by
name (case-insensitive) and updated; new members get a fresh `member_code`.

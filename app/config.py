from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Admin web UI password.
    admin_password: str = "changeme"
    session_secret: str = "dev-secret-change-in-production"

    # Shared secret that Tempus / Munus present (as the `X-API-Key` header) to read
    # the member roster from the JSON API. Blank = the API is disabled (returns 503),
    # so a misconfigured deploy fails closed rather than serving data to anyone.
    legion_api_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./legion.db"

    timezone: str = "America/New_York"

    # Slack: pushes member metadata into each person's custom profile fields.
    # NOTE: setting *another* user's profile requires an admin *user* token (xoxp-…)
    # with users.profile:write — a normal bot token can only edit its own profile.
    # Blank = the Slack sync (nightly job + manual button) is disabled.
    slack_bot_token: str = ""
    slack_sync_time: str = "01:00"  # HH:MM 24h local time for the nightly profile sync
    slack_sync_day: str = "*"  # day(s) of week to sync (cron style; * = every day)

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the weekly snapshot
    backup_day: str = "sun"  # day of week for the weekly backup (mon-sun)

    # Global toggle for scheduled jobs (currently just the backup snapshot).
    updates_enabled: bool = True

    # ── SSO ──────────────────────────────────────────────────────────────────────
    # Legion is the identity provider for the MARS/WARS apps: a member enters their
    # `username`, approves a Slack DM push, and Legion sets a signed `mw_sso` cookie
    # every sibling app can verify locally. Separate from `session_secret` so rotating
    # this one doesn't also invalidate the admin break-glass password session.
    sso_secret: str = "dev-secret-change-in-production"
    # Cookie `Domain=`, e.g. ".marswars.org", so one login covers every subdomain.
    # Blank = host-only cookie (fine for local dev across ports on one host).
    sso_cookie_domain: str = ""
    sso_session_ttl: int = 60 * 60 * 12  # how long the mw_sso cookie is trusted (seconds)
    sso_challenge_ttl: int = 30  # how long an Approve/Deny prompt stays valid (seconds)

    # Login rate limit: `sso_rate_max` attempts per `sso_rate_window` seconds, per
    # browser (device cookie) and per matched member. Exceeding it locks the key for
    # `sso_backoff_base` seconds, multiplying by `sso_backoff_multiplier` on each
    # repeat offense so sustained spamming gets throttled harder over time.
    sso_rate_max: int = 3
    sso_rate_window: int = 300
    sso_backoff_base: int = 30
    sso_backoff_multiplier: int = 4

    # Comma-separated hostnames `/sso/authorize?return_to=` is allowed to redirect back
    # to (open-redirect guard). A bare path ("/admin") is always allowed regardless.
    sso_allowed_return_hosts: str = "localhost,127.0.0.1"

    # Absolute base URL of this Legion instance (used for links, e.g. in Slack DMs).
    base_url: str = "http://localhost:8002"

    # Slack app used for inbound interactivity (Approve/Deny buttons) and outbound
    # challenge DMs. Deliberately a *different* credential from `slack_bot_token`
    # above (which holds an admin *user* token, `xoxp-…`, only good for profile
    # writes): sending DMs and receiving button clicks needs a real *bot* token
    # (`xoxb-…`, scopes `chat:write` + `im:write`) plus the app's signing secret to
    # verify inbound requests. Blank = SSO Slack challenges are disabled.
    slack_auth_bot_token: str = ""
    slack_signing_secret: str = ""

    # ── Shared Slack interactivity dispatch ─────────────────────────────────────
    # Tempus, Munus, and Legion currently share ONE Slack app (same bot token +
    # signing secret across all three .env files) — Slack allows only one
    # Interactivity Request URL per app, so it's pointed at Legion's `/slack/dispatch`,
    # which inspects each payload's action_id/callback_id and forwards the request,
    # byte-for-byte, to whichever app's own `/slack/interact` actually owns it. Each
    # app still verifies the Slack signature itself; the dispatcher is a stateless
    # relay, not a new trust boundary. Slash commands don't need this — each slash
    # command has its own independently configurable Request URL already.
    tempus_interact_url: str = "http://tempus:8000/slack/interact"
    munus_interact_url: str = "http://munus:8001/slack/interact"
    legion_interact_url: str = "http://localhost:8002/slack/interact"


settings = Settings()

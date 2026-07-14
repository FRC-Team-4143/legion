from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder values shipped in .env.example — accepting these silently would let a
# deploy that forgot to replace them run with a known, guessable secret.
_INSECURE_DEFAULTS = {
    "changeme",
    "dev-secret-change-in-production",
    "replace-with-a-long-random-string",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Admin web UI password. Required (no default) — a deploy that forgets to set
    # this fails to start instead of silently running with a guessable value.
    admin_password: str
    session_secret: str

    # Per-consumer secrets Tempus / Munus each present (as the `X-API-Key` header) to
    # read the member roster from the JSON API and to hit /sso/challenge. Separate
    # keys so a leak from one app's .env doesn't expose the other's, and either can be
    # rotated independently. Both blank = the API is disabled (returns 503), so a
    # misconfigured deploy fails closed rather than serving data to anyone.
    tempus_api_key: str = ""
    munus_api_key: str = ""

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
    sso_secret: str
    # Cookie `Domain=`, e.g. ".marswars.org", so one login covers every subdomain.
    # Blank = host-only cookie (fine for local dev across ports on one host).
    sso_cookie_domain: str = ""
    sso_session_ttl: int = 60 * 60 * 12  # how long the mw_sso cookie is trusted (seconds)
    sso_challenge_ttl: int = 30  # how long an Approve/Deny prompt stays valid (seconds)
    # Separate, longer TTL for challenges started server-to-server via POST /sso/challenge
    # (e.g. Munus's /vhours one-tap link): unlike the browser-form flow above, there's a
    # human-reads-a-Slack-message delay before the browser ever starts polling, so the
    # challenge needs to survive longer than the 30s tuned for an already-open, already-
    # polling tab.
    sso_api_challenge_ttl: int = 300  # 5 min

    # Auto-delete the Approve/Deny challenge DMs (and their AuthRequest rows) once
    # they're this old, so a member's DM thread with the auth bot doesn't fill up
    # with stale sign-in prompts. 15 min is well past the max challenge TTL above,
    # so a live sign-in is never reaped mid-flow. A background job sweeps every
    # `sso_dm_cleanup_interval_minutes`, so a DM disappears ~retention+interval
    # minutes after it's sent. See services/slack_auth.purge_old_challenge_dms.
    sso_dm_retention_minutes: int = 15
    sso_dm_cleanup_interval_minutes: int = 5

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

    # ── Home page app launcher ──────────────────────────────────────────────────
    # Public URLs for the sibling apps' tiles on Legion's signed-in home page ("/").
    # Deliberately separate from tempus_interact_url/munus_interact_url above — those
    # are internal Docker-network addresses for server-to-server calls, not something
    # a member's own browser can resolve. Blank = that app's tile is simply omitted.
    tempus_public_url: str = ""
    munus_public_url: str = ""

    @field_validator("admin_password", "session_secret", "sso_secret")
    @classmethod
    def _reject_insecure_secret(cls, v: str, info) -> str:
        if not v or v in _INSECURE_DEFAULTS:
            raise ValueError(
                f"{info.field_name} must be set to a real secret in .env — "
                "it is blank or still the placeholder value from .env.example"
            )
        return v


settings = Settings()

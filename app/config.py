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

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the weekly snapshot
    backup_day: str = "sun"  # day of week for the weekly backup (mon-sun)

    # Global toggle for scheduled jobs (currently just the backup snapshot).
    updates_enabled: bool = True


settings = Settings()

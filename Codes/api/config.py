"""
config.py -- typed settings for the FastAPI app, read once from environment
variables (never a secret in this repo -- see .env.example). Mirrors
Codes/config.py's DatabaseSettings for the synchronous ETL side, but builds
an asyncpg URL instead: this app's DB-bound endpoints are genuinely async
(see db.py), so they need the async driver, not psycopg2.
"""

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus


class ConfigurationError(RuntimeError):
    """Raised when required Postgres connection environment variables are missing."""


@dataclass(frozen=True)
class Settings:
    database_url: Optional[str]
    pg_host: Optional[str]
    pg_port: str
    pg_database: Optional[str]
    pg_user: Optional[str]
    pg_password: Optional[str]
    pg_schema: Optional[str]
    session_idle_timeout_hours: int
    session_absolute_max_days: int
    session_cookie_secure: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            pg_host=os.environ.get("PG_HOST"),
            pg_port=os.environ.get("PG_PORT", "5432"),
            pg_database=os.environ.get("PG_DATABASE"),
            pg_user=os.environ.get("PG_USER"),
            pg_password=os.environ.get("PG_PASSWORD"),
            pg_schema=os.environ.get("PG_SCHEMA"),
            session_idle_timeout_hours=int(os.environ.get("SESSION_IDLE_TIMEOUT_HOURS", "12")),
            session_absolute_max_days=int(os.environ.get("SESSION_ABSOLUTE_MAX_DAYS", "14")),
            # Defaults to secure (HTTPS-only cookies) -- must be explicitly
            # opted out of, never opted in, so a misconfigured/missing env
            # var fails safe. Only ever set to "false" for local http-only
            # development (see docker-compose.yml's local-http profile) --
            # this used to be a hardcoded True that had to be hand-edited
            # and hand-reverted for local testing, which is exactly the
            # kind of change that's easy to forget to undo.
            session_cookie_secure=os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower()
            not in ("false", "0", "no", ""),
        )

    def asyncpg_url(self) -> str:
        if self.database_url:
            # DATABASE_URL is documented (.env.example, README) using the
            # sync driver scheme (postgresql+psycopg2://...) since that's
            # what the ETL CLI needs -- normalize it to asyncpg's dialect
            # here so the SAME env var works for both without the operator
            # needing to set two different connection strings.
            url = self.database_url
            if url.startswith("postgresql+psycopg2://"):
                return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://"):]
            if url.startswith("postgresql://"):
                return "postgresql+asyncpg://" + url[len("postgresql://"):]
            return url

        missing = [
            name for name, value in (
                ("PG_HOST", self.pg_host), ("PG_USER", self.pg_user),
                ("PG_PASSWORD", self.pg_password), ("PG_DATABASE", self.pg_database),
            ) if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Missing required Postgres connection environment variable(s): {missing}. "
                f"Set DATABASE_URL, or all of PG_HOST/PG_USER/PG_PASSWORD/PG_DATABASE."
            )
        return (
            f"postgresql+asyncpg://{quote_plus(self.pg_user)}:{quote_plus(self.pg_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

"""
config.py -- typed connection settings for the synchronous ETL side
(Codes/postgres_pipeline.py, Codes/api/create_user.py's CLI use case if it
ever needs the sync path). Centralizes what used to be scattered
os.environ.get() calls duplicated inside multiple get_engine() functions --
one class, one place that knows the variable names, still entirely env-var
driven (nothing here is hardcoded; see .env.example for what to set).
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine, event

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """Raised when required Postgres connection environment variables are missing."""


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: Optional[str]
    pg_host: Optional[str]
    pg_port: str
    pg_database: Optional[str]
    pg_user: Optional[str]
    pg_password: Optional[str]
    pg_schema: Optional[str]

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            pg_host=os.environ.get("PG_HOST"),
            pg_port=os.environ.get("PG_PORT", "5432"),
            pg_database=os.environ.get("PG_DATABASE"),
            pg_user=os.environ.get("PG_USER"),
            pg_password=os.environ.get("PG_PASSWORD"),
            pg_schema=os.environ.get("PG_SCHEMA"),
        )

    def sqlalchemy_url(self) -> str:
        """postgresql+psycopg2://... -- the synchronous driver URL used by
        the batch ETL pipeline (see Codes/api/config.py's Settings.
        asyncpg_url() for the API's async equivalent)."""
        if self.database_url:
            return self.database_url

        missing = [
            name for name, value in (
                ("PG_HOST", self.pg_host), ("PG_USER", self.pg_user),
                ("PG_PASSWORD", self.pg_password), ("PG_DATABASE", self.pg_database),
            ) if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Missing required Postgres connection environment variable(s): {missing}. "
                f"Set DATABASE_URL, or all of PG_HOST/PG_USER/PG_PASSWORD/PG_DATABASE "
                f"(+ optional PG_PORT, default 5432)."
            )
        return (
            f"postgresql+psycopg2://{quote_plus(self.pg_user)}:{quote_plus(self.pg_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    def create_engine(self) -> Engine:
        """
        The one place that builds a sync SQLAlchemy engine from these
        settings, PG_SCHEMA search_path handling included -- used by
        PostgresETLPipeline (the ETL) and Codes/run_migrations.py (schema
        migrations) alike, so there's a single source of truth for how
        that engine gets built rather than two near-duplicate
        implementations drifting apart.

        See Codes/postgres_pipeline.py's original _apply_schema_search_path
        docstring for the two things this deliberately gets right: a real
        `SET search_path TO "..."` (not a `-c search_path=...` libconnect
        option, which silently lowercases a mixed-case schema name) plus an
        explicit commit() right after (psycopg2 defaults to
        autocommit=False, so an uncommitted SET sits inside an open
        transaction that a later read-only rollback -- e.g.
        pandas.read_sql's connection close -- silently reverts).
        """
        engine = create_engine(self.sqlalchemy_url(), pool_pre_ping=True)
        self._apply_schema_search_path(engine)
        logger.info("Postgres engine created (schema=%s)", self.pg_schema or "public")
        return engine

    def _apply_schema_search_path(self, engine: Engine) -> None:
        if not self.pg_schema:
            return
        quoted = self.pg_schema.replace('"', '""')

        @event.listens_for(engine, "connect")
        def _set_search_path(dbapi_connection, connection_record):  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{quoted}", public')
            cursor.close()
            dbapi_connection.commit()

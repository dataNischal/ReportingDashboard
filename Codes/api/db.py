"""
db.py -- async SQLAlchemy engine/session for the FastAPI app. Uses asyncpg
(not psycopg2) so DB-bound endpoints (see routers/) genuinely don't block
the event loop while a query is in flight -- a `def` endpoint would still
avoid blocking via Starlette's threadpool, but with real concurrent load an
async engine gives materially higher throughput per worker, which is the
actual point of the async/await conversion this app went through.
"""

import asyncio
import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from config import Settings

logger = logging.getLogger(__name__)


class Database:
    """
    Owns exactly one AsyncEngine/sessionmaker pair for the app's lifetime --
    created once in main.py's lifespan startup, disposed on shutdown (see
    main.py), and handed to routers via FastAPI dependency injection
    (deps.get_database) rather than a lazily-initialized module global.

    PG_SCHEMA (optional) is applied via asyncpg's `server_settings` at
    connection startup time -- NOT a post-connect `SET search_path`. This
    matters: a `SET` issued after connecting sits inside an open
    transaction until something commits it, and a later rollback (e.g. a
    read-only request that never explicitly commits) silently reverts it --
    confirmed the hard way against Codes/postgres_pipeline.py's synchronous
    equivalent, which needed an explicit extra commit() to work around
    exactly that. asyncpg's server_settings are sent in the connection's
    startup packet, applied before any client transaction exists, so
    there's nothing for a later rollback to undo.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        connect_args = {}
        if settings.pg_schema:
            quoted_schema = settings.pg_schema.replace('"', '""')
            connect_args["server_settings"] = {"search_path": f'"{quoted_schema}", public'}

        self.engine: AsyncEngine = create_async_engine(
            settings.asyncpg_url(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        # Same pool as self.engine (execution_options() returns a shallow
        # copy sharing the connection pool, not a second engine/pool) --
        # just with every statement auto-committed at the DBAPI level
        # instead of wrapped in an explicit BEGIN/COMMIT. Used by
        # deps.get_current_user, which runs on EVERY authenticated request
        # (including trivial ones like GET /api/me) and only ever executes
        # one already-atomic statement (security.validate_and_refresh_
        # session's single UPDATE...RETURNING CTE) -- an explicit
        # transaction there bought nothing but two extra network round
        # trips (~280ms each on this RDS instance's real latency).
        self.autocommit_engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        logger.info("Async database engine created (schema=%s)", settings.pg_schema or "public")

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def warm_up(self, queries) -> None:
        """
        Runs each query in `queries` once, concurrently (each on its own
        session/connection), before the app starts serving real traffic.

        Confirmed directly against the live RDS instance: a fresh
        connection's FIRST execution of one of the dashboard's bulk
        SELECTs (against mv_dashboard_rows etc., see
        sql/004_dashboard_payload_mvs.sql) takes meaningfully longer than
        later executions on an already-used connection (e.g. one run
        measured 22s cold, dropping to ~7s by the 4th call on a
        newly-created engine) -- some combination of Postgres-side query
        planning and TLS/connection-setup cost that isn't eliminated by
        the pre-aggregated views themselves, only amortized once paid.
        Paying that cost here, concurrently, means the first real user
        doesn't have to.
        """
        start = time.monotonic()

        async def _run_one(query: str) -> None:
            async with self.session() as session:
                await session.execute(text(query))

        await asyncio.gather(*(_run_one(q) for q in queries), return_exceptions=False)
        logger.info(
            "Database connection pool warmed up (%d quer%s, %.1fs).",
            len(queries), "y" if len(queries) == 1 else "ies", time.monotonic() - start,
        )

    async def warm_up_auth_check(self, security, token: str, times: int) -> None:
        """
        Companion to warm_up() above, but for security.validate_and_
        refresh_session -- the query deps.get_current_user runs on EVERY
        authenticated request, via self.autocommit_engine, not
        self.session(). warm_up() never touches this query at all (it
        only runs the 4 dashboard-view SELECTs), and confirmed directly:
        asyncpg caches a prepared statement PER PHYSICAL CONNECTION, not
        once for the whole pool, so without this, a real user's first few
        authenticated requests after startup each land on a pooled
        connection that has never seen this exact query text before and
        pay an extra network round trip (prepare, on top of execute)
        until enough of the pool happens to have cycled through it.

        `token` must be one that can never match a real session (an
        all-zero UUID works -- app_sessions.id is a real gen_random_uuid(),
        never all zeroes) so this can never mutate a real session's
        expiry. `times` should be >= pool_size so this actually reaches
        (most of) the pool, not just one connection repeatedly.
        """
        start = time.monotonic()

        async def _run_one() -> None:
            async with self.autocommit_engine.connect() as conn:
                await security.validate_and_refresh_session(conn, token)

        await asyncio.gather(*(_run_one() for _ in range(times)), return_exceptions=False)
        logger.info(
            "Auth session-check warmed up (%d call%s, %.1fs).",
            times, "" if times == 1 else "s", time.monotonic() - start,
        )

    async def dispose(self) -> None:
        await self.engine.dispose()
        logger.info("Database engine disposed")

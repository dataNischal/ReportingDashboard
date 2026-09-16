"""
security.py -- password hashing and session management (async).

Password hashing: argon2id (via argon2-cffi), OWASP's current default
recommendation -- memory-hard (resists GPU/ASIC cracking far better than
bcrypt), and the reference implementation from the Password Hashing
Competition. Never store or log a plaintext password anywhere, including in
request logs -- routers/auth.py accepts it only in a request body, never a
query string.

Sessions: server-side, opaque random tokens (NOT JWTs). A session row in
Postgres (app_sessions) is the single source of truth, so it can be revoked
immediately (DELETE or revoked=true) and its expiry SLIDES forward on every
authenticated request instead of being fixed at issue time -- this is the
deliberate fix for the project's original problem (S3 presigned URLs that
died on a fixed timer with no way to refresh). idle_timeout governs how
long a session survives with no activity; absolute_max caps how long it can
be extended even with continuous use, so a stolen/forgotten session cookie
doesn't stay valid forever.
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session_id"


class SecurityService:
    """
    Argon2 hashing/verification is CPU-bound BY DESIGN (that expense is the
    security property) -- run via asyncio.to_thread so one password check
    can't stall the event loop for every other concurrent request while it
    hashes. Session methods take an AsyncSession/AsyncConnection the CALLER
    owns/commits (see deps.py, routers/auth.py) so each call site controls
    its own transaction boundary rather than this service silently
    auto-committing -- validate_and_refresh_session in particular is called
    with an AUTOCOMMIT AsyncConnection (deps.py), since it's already a
    single atomic statement with nothing else to group into a transaction.
    """

    def __init__(self, idle_timeout_hours: int, absolute_max_days: int, cookie_secure: bool = True):
        self._ph = PasswordHasher()
        self.idle_timeout = timedelta(hours=idle_timeout_hours)
        self.absolute_max = timedelta(days=absolute_max_days)
        # Read by routers/auth.py when setting the session cookie -- see
        # config.py's Settings.session_cookie_secure for why this defaults
        # to True and must be explicitly opted out of, never in.
        self.cookie_secure = cookie_secure

    async def hash_password(self, plaintext: str) -> str:
        return await asyncio.to_thread(self._ph.hash, plaintext)

    async def verify_password(self, plaintext: str, password_hash: str) -> bool:
        try:
            return await asyncio.to_thread(self._ph.verify, password_hash, plaintext)
        except VerifyMismatchError:
            return False

    async def create_session(self, conn: AsyncSession, user_id: int) -> str:
        """Returns the new session's opaque token (the row's UUID PK, as a str)."""
        now = datetime.now(timezone.utc)
        result = await conn.execute(
            text("""
                INSERT INTO app_sessions (user_id, created_at, last_seen_at, expires_at)
                VALUES (:user_id, :now, :now, :expires_at)
                RETURNING id
            """),
            {"user_id": user_id, "now": now, "expires_at": now + self.idle_timeout},
        )
        session_id = result.scalar_one()
        await conn.execute(
            text("UPDATE dashboard_credential SET last_login_at = :now WHERE id = :user_id"),
            {"now": now, "user_id": user_id},
        )
        return str(session_id)

    async def validate_and_refresh_session(
        self, conn: Union[AsyncSession, AsyncConnection], session_token: str,
    ) -> Optional[dict]:
        """
        Returns the user row (id, email, name, role) if the session is
        valid, else None. On success, slides expires_at forward by
        idle_timeout from now -- capped so a session can never outlive
        created_at + absolute_max regardless of how continuously it's used.

        Deliberately a single statement (a validate-and-UPDATE CTE, joined
        straight to dashboard_credential) rather than a separate SELECT
        then UPDATE. Confirmed directly against the live RDS instance that
        each round trip here costs a fixed ~270-300ms (this app's DB host
        is geographically distant from where it's served), so the old
        two-statement version cost this every-single-request dependency
        (get_current_user, i.e. EVERY authenticated call including trivial
        ones like GET /api/me) two full network round trips where one
        suffices -- the idle_timeout/absolute_max cap logic below has to
        be expressed in SQL instead of Python for that to work, since
        there's no longer a Python-side read of created_at before the
        UPDATE runs.
        """
        now = datetime.now(timezone.utc)
        idle_seconds = self.idle_timeout.total_seconds()
        absolute_seconds = self.absolute_max.total_seconds()
        result = await conn.execute(
            text("""
                WITH refreshed AS (
                    UPDATE app_sessions s
                    SET last_seen_at = :now,
                        expires_at = LEAST(
                            :now + make_interval(secs => :idle_seconds),
                            created_at + make_interval(secs => :absolute_seconds)
                        )
                    WHERE s.id = :sid AND NOT s.revoked AND s.expires_at > :now
                      -- matches the old SELECT-then-UPDATE's behavior of never
                      -- sliding expiry forward for an inactive user's session
                      AND EXISTS (
                          SELECT 1 FROM dashboard_credential u
                          WHERE u.id = s.user_id AND u.is_active
                      )
                    RETURNING user_id
                )
                SELECT u.id, u.email, u.name, u.role, u.is_active
                FROM refreshed r
                JOIN dashboard_credential u ON u.id = r.user_id
            """),
            {
                "sid": session_token, "now": now,
                "idle_seconds": idle_seconds, "absolute_seconds": absolute_seconds,
            },
        )
        row = result.first()
        if row is None or not row.is_active:
            return None
        return {"id": row.id, "email": row.email, "name": row.name, "role": row.role}

    async def revoke_session(self, conn: AsyncSession, session_token: str) -> None:
        await conn.execute(
            text("UPDATE app_sessions SET revoked = true WHERE id = :sid"), {"sid": session_token},
        )

    @staticmethod
    def new_csrf_token() -> str:
        """Not currently wired in -- see main.py's docstring note on same-site
        cookie scope being the primary CSRF mitigation for this app's own
        same-origin fetch() calls; kept here as a documented extension point if
        a future integration needs cross-site form posts."""
        return secrets.token_urlsafe(32)

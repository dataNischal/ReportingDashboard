"""
deps.py -- FastAPI dependencies shared across routers (async). Pulls the
long-lived Database/SecurityService instances off app.state (set once in
main.py's lifespan) rather than re-creating them per request.
"""

import logging

from fastapi import Depends, HTTPException, Request, status

from db import Database
from security import SESSION_COOKIE_NAME, SecurityService

logger = logging.getLogger(__name__)


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_security_service(request: Request) -> SecurityService:
    return request.app.state.security


async def get_current_user(
    request: Request,
    db: Database = Depends(get_database),
    security: SecurityService = Depends(get_security_service),
) -> dict:
    """
    Reads the session cookie, validates it against app_sessions (sliding
    the expiry forward on success -- see security.py), and returns the
    authenticated user dict. Raises 401 if missing/expired/revoked, which
    the frontend's fetch() wrapper treats as "redirect to /login".
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        # AUTOCOMMIT connection, not the ORM's transactional session: see
        # db.py's autocommit_engine docstring -- this dependency fires on
        # every authenticated request and the query it runs is already a
        # single atomic statement, so an explicit BEGIN/COMMIT here was
        # pure round-trip overhead.
        async with db.autocommit_engine.connect() as conn:
            user = await security.validate_and_refresh_session(conn, token)
    except Exception:
        logger.exception("Session validation failed unexpectedly.")
        raise

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid",
        )

    return user


def require_role(*allowed_roles: str):
    """
    Dependency factory: require_role("admin", "business") -- for endpoints
    that should be visible to more than one role. Roles are the same four
    values dashboard_credential.role is CHECK-constrained to (see
    sql/001_auth_and_watermark.sql): admin, stakeholder, business, accounts.
    """
    async def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed_roles:
            needed = ", ".join(allowed_roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires one of role(s): {needed}",
            )
        return user
    return _check


require_admin = require_role("admin")

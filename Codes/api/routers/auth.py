"""
routers/auth.py -- login/logout/whoami. No self-service signup endpoint:
business-stakeholder accounts are provisioned directly in dashboard_credential
(see Codes/api/create_user.py), matching this dashboard's existing "known,
named stakeholder list" access model rather than open registration.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text

from db import Database
from deps import get_current_user, get_database, get_security_service
from security import SESSION_COOKIE_NAME, SecurityService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class StatusResponse(BaseModel):
    status: str


class MeResponse(BaseModel):
    id: int
    email: str
    name: Optional[str] = None
    role: str


@router.post("/login", response_model=StatusResponse, responses={401: {"model": None}})
async def login(
    body: LoginRequest,
    response: Response,
    db: Database = Depends(get_database),
    security: SecurityService = Depends(get_security_service),
) -> StatusResponse:
    try:
        async with db.session() as session, session.begin():
            user = (await session.execute(
                text(
                    "SELECT id, password_hash, is_active FROM dashboard_credential "
                    "WHERE email = :email"
                ),
                {"email": body.email},
            )).first()

            # Same generic error whether the email doesn't exist or the
            # password is wrong -- never reveal which one to an
            # unauthenticated caller (standard login-enumeration mitigation).
            password_ok = user is not None and await security.verify_password(
                body.password, user.password_hash
            )
            if user is None or not user.is_active or not password_ok:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password",
                )

            token = await security.create_session(session, user.id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Login failed unexpectedly for %s", body.email)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Login failed",
        )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=security.cookie_secure,  # env-driven -- see Settings.session_cookie_secure
        samesite="lax",
        max_age=int(security.idle_timeout.total_seconds()),
        path="/",
    )
    logger.info("User %s logged in.", body.email)
    return StatusResponse(status="ok")


@router.post("/logout", response_model=StatusResponse)
async def logout(
    request: Request,
    response: Response,
    db: Database = Depends(get_database),
    security: SecurityService = Depends(get_security_service),
    user: dict = Depends(get_current_user),
) -> StatusResponse:
    # get_current_user already validated the cookie is present/valid; revoke
    # only this exact session (not every session for this user, in case
    # they're also logged in on another device).
    token = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        async with db.session() as session, session.begin():
            await security.revoke_session(session, token)
    except Exception:
        logger.exception("Logout failed unexpectedly for user_id=%s", user.get("id"))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Logout failed",
        )

    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return StatusResponse(status="ok")


@router.get("/me", response_model=MeResponse)
async def me(user: dict = Depends(get_current_user)) -> MeResponse:
    return MeResponse(**user)

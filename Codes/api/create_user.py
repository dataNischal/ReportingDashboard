"""
create_user.py -- one-off CLI to provision a business-stakeholder account.
No self-service signup endpoint exists on purpose (see routers/auth.py) --
accounts are created directly by whoever administers this dashboard.

Usage:
    python create_user.py --email jane@isend.com.sg --name "Jane Doe" --role admin
    (--role defaults to "stakeholder"; choices are admin/stakeholder/business/
    accounts -- see dashboard_credential's CHECK constraint in
    sql/001_auth_and_watermark.sql. Prompts for a password; never pass one
    as a CLI argument -- it would land in shell history / process listings.)

Reuses the same async Database/SecurityService classes the FastAPI app
uses (see db.py/security.py) rather than a separate sync connection --
one Postgres access pattern for the whole api/ package, not two.
"""

import argparse
import asyncio
import getpass
import logging
import os
import sys

from sqlalchemy import text

# See main.py's identical note: Codes/ and Codes/api/ each have their own
# config.py -- api/ must be searched first (insert 0) so `import config`
# resolves here, not to Codes/config.py; Codes/ is appended last, as a
# fallback for logging_config (which only exists there).
_API_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _API_DIR)
sys.path.append(os.path.dirname(_API_DIR))
from config import Settings  # noqa: E402
from db import Database  # noqa: E402
from logging_config import configure_logging  # noqa: E402
from security import SecurityService  # noqa: E402

logger = logging.getLogger(__name__)

ROLES = ("admin", "stakeholder", "business", "accounts")


class UserProvisioningService:
    """Wraps the INSERT ... ON CONFLICT DO UPDATE upsert into
    dashboard_credential -- idempotent, so re-running with the same email
    updates that account's password/name/role rather than erroring."""

    def __init__(self, db: Database, security: SecurityService):
        self.db = db
        self.security = security

    async def create_or_update(
        self, email: str, password: str, name: str | None, role: str,
    ) -> None:
        password_hash = await self.security.hash_password(password)
        try:
            async with self.db.session() as session, session.begin():
                await session.execute(
                    text("""
                        INSERT INTO dashboard_credential (email, password_hash, name, role)
                        VALUES (:email, :password_hash, :name, :role)
                        ON CONFLICT (email) DO UPDATE
                        SET password_hash = EXCLUDED.password_hash,
                            name = EXCLUDED.name,
                            role = EXCLUDED.role
                    """),
                    {"email": email, "password_hash": password_hash, "name": name, "role": role},
                )
        except Exception:
            logger.exception("Failed to create/update user %s", email)
            raise


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    db = Database(settings)
    security = SecurityService(
        idle_timeout_hours=settings.session_idle_timeout_hours,
        absolute_max_days=settings.session_absolute_max_days,
    )
    try:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)

        service = UserProvisioningService(db, security)
        await service.create_or_update(args.email, password, args.name, args.role)
        print(f"User {args.email} created/updated with role '{args.role}'.")
    finally:
        await db.dispose()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default=None)
    parser.add_argument("--role", choices=ROLES, default="stakeholder")
    args = parser.parse_args()

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

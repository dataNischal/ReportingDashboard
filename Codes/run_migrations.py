"""
run_migrations.py -- applies sql/001_*.sql through sql/00N_*.sql, in
filename order, against the database DatabaseSettings.from_env() points
at. Every migration in this project is written idempotently (CREATE
TABLE/INDEX/EXTENSION ... IF NOT EXISTS, CREATE MATERIALIZED VIEW ... IF
NOT EXISTS), so re-running this against an already-migrated database is
safe -- it just does nothing for files whose objects already exist.

This exists mainly so `docker compose run --rm etl python run_migrations.py`
is the one command that gets a fresh database (or an existing one that's
missing the newest migration) fully up to date, without needing a `psql`
client installed anywhere -- this project's own psycopg2 dependency is
enough.

Usage:
    python Codes/run_migrations.py
"""

import glob
import logging
import os

from sqlalchemy import text

from config import DatabaseSettings
from logging_config import configure_logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    settings = DatabaseSettings.from_env()
    # Same engine/schema-search_path construction PostgresETLPipeline uses
    # (DatabaseSettings.create_engine()) -- migrations need the exact same
    # PG_SCHEMA handling as everything else, from one shared place.
    engine = settings.create_engine()

    migration_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "sql", "*.sql")))
    if not migration_files:
        logger.warning("No migration files found under sql/.")
        return

    for path in migration_files:
        name = os.path.basename(path)
        logger.info("Applying %s ...", name)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("%s applied.", name)

    logger.info("All %d migration file(s) applied.", len(migration_files))


if __name__ == "__main__":
    main()

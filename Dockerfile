# Single image, two roles: the always-on FastAPI dashboard (Codes/api/)
# and the batch ETL/migrations jobs (Codes/postgres_pipeline.py,
# Codes/run_migrations.py, Codes/api/create_user.py) -- one requirements.txt
# already covers both (see requirements.txt's own comments on why psycopg2
# and asyncpg coexist), so one image is simpler to build/maintain than two
# that would otherwise be near-identical. docker-compose.yml's services
# pick which role to play via `command:`.

# ---- builder: compile/collect dependencies only, nothing app-specific ----
FROM python:3.12-slim AS builder
WORKDIR /build

# build-essential/libpq-dev: only needed if a dependency has no prebuilt
# wheel for this platform and falls back to compiling from source (most of
# requirements.txt ships wheels; this is cheap insurance, not a given need).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime: slim, no compilers, non-root ----
FROM python:3.12-slim
WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /install /usr/local

COPY cleaning_config.yml ./
# WORKFLOW.md/DASHBOARD_GUIDE.md: read at request time by Codes/api/main.py's
# GET /help (see that route's docstring) -- README.md/CLAUDE.md aren't
# needed at runtime (nothing serves them) so they're deliberately left out.
COPY WORKFLOW.md DASHBOARD_GUIDE.md ./
COPY sql ./sql
COPY Codes ./Codes

RUN chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Default role: the API. docker-compose.yml overrides `command:` for the
# etl/migrations/create-user one-off services.
WORKDIR /app/Codes/api
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

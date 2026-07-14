#!/usr/bin/env bash
# Render start command: run migrations, then boot uvicorn.
set -euo pipefail

echo "→ Running alembic migrations…"
alembic upgrade head

echo "→ Starting uvicorn on port ${PORT:-10000}…"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"

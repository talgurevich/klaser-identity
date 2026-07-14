# klaser-identity

Klaser's shared identity service. Owns users, tenants, sessions, and per-tenant product subscriptions. Both Takanon and Klaser Meetings authenticate against this service — no product carries its own users table.

Full architecture in [`docs/klaser-platform-infra.md`](https://github.com/talgurevich/elrom-platform/blob/main/docs/klaser-platform-infra.md) on the Takanon repo.

## Runs at

- Production: `https://auth.klaser.co.il`
- Dev: `http://localhost:8001`

## What it exposes

- `POST /api/auth/google` — Google OAuth callback (single client, single redirect URI).
- `POST /api/auth/register` — invite-token → set password → sign in.
- `POST /api/auth/login` — email + password.
- `POST /api/auth/logout` — clear session.
- `GET  /api/auth/me` — current user + tenant + `entitlements: string[]` (e.g. `["takanon", "meetings"]`).
- `POST /api/auth/forgot-password` / `POST /api/auth/reset-password` — reset flow.
- `GET  /api/auth/tenants` / `POST /api/auth/switch-tenant` — super-admin tenant switching.
- `GET  /api/introspect` — product backends call this with the caller's session cookie, get back `{user, tenant, entitlements}`. This is the primary auth path for both Takanon and Meetings backends.
- `POST /api/service/*` — service-token endpoints for product backends outside a request context (background jobs, cron).

## Local dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in DATABASE_URL etc.
alembic upgrade head
uvicorn app.main:app --reload --port 8001
```

## Migration order (one-time)

See §9.6 of the Klaser roadmap. High-level:

1. Deploy this service against a fresh `klaser_identity` Postgres.
2. Run the migration script that dumps `users` / `tenants` / `auth_tokens` from the Takanon DB into this one.
3. Point Takanon's backend at this service (swap local auth code for HTTP calls to `/introspect`).
4. Widen session cookie to `Domain=.klaser.co.il`.
5. Drop the auth tables from Takanon's DB (last, irreversible step).

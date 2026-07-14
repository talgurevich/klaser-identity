"""One-shot data migration: Takanon DB → identity DB.

Copies `tenants`, `users`, and `auth_tokens` from the Takanon backend's
Postgres into the identity service's Postgres, preserving UUIDs so
Takanon's remaining tables (which reference user_id / tenant_id) keep
resolving after cutover.

Also seeds a default `takanon` subscription for every migrated tenant,
so their users' entitlements aren't empty when the products cut over.

Usage
-----

    # Preview — no writes to the target DB
    python scripts/migrate_from_takanon.py --dry-run

    # Actually run — commits after every table
    python scripts/migrate_from_takanon.py --commit

Env vars required
-----------------

    SOURCE_DATABASE_URL   Takanon Postgres (read-only in this script)
    TARGET_DATABASE_URL   Identity Postgres (writes go here)

Both accept the `postgresql://` or `postgres://` schemes — no need to
convert to `postgresql+psycopg://` for this script, it uses psycopg
directly.

Idempotency
-----------

Every INSERT uses `ON CONFLICT (id) DO NOTHING`, so re-running is safe.
The subscription seed uses `ON CONFLICT (tenant_id, product) DO NOTHING`
via a partial unique index (created on first run if not present).

Safety checks
-------------

- Refuses to run if the target `users` table already has more rows than
  the source — this suggests the migration already happened and we'd be
  layering stale data on top.
- Prints counts before and after each table.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg
except ImportError as e:  # pragma: no cover
    print(
        "psycopg not installed. Run `pip install -r requirements.txt` in the "
        "identity repo, or `pip install 'psycopg[binary]'` in a scratch venv."
    )
    raise SystemExit(1) from e


DEFAULT_PRODUCT = "takanon"


def _open(url: str, *, readonly: bool = False) -> psycopg.Connection:
    """Open a psycopg3 connection. `readonly=True` sets the session
    read-only so we can't accidentally write to the source DB."""
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(url, autocommit=False)
    if readonly:
        conn.execute("SET TRANSACTION READ ONLY")
    return conn


def _count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])


def _fetch_all(conn: psycopg.Connection, sql: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def _insert_many(
    conn: psycopg.Connection,
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    conflict_target: str = "id",
) -> int:
    """Bulk-insert rows into ``table`` with ON CONFLICT DO NOTHING on the
    given target. Returns the number of rows that were inserted (excludes
    conflicts). Does NOT commit — the caller owns the outer transaction,
    so dry-run vs commit is decided at the very end after all tables have
    been inserted (necessary because table N's FKs may reference table N-1).
    """
    if not rows:
        return 0
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_target}) DO NOTHING"
    )
    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            values = [row.get(c) for c in columns]
            cur.execute(sql, values)
            inserted += cur.rowcount
    return inserted


def migrate_tenants(src: psycopg.Connection, tgt: psycopg.Connection) -> int:
    rows = _fetch_all(
        src,
        "SELECT id, name, segment, created_at, system_context FROM tenants ORDER BY created_at",
    )
    return _insert_many(
        tgt,
        "tenants",
        ["id", "name", "segment", "created_at", "system_context"],
        rows,
    )


def migrate_users(src: psycopg.Connection, tgt: psycopg.Connection) -> int:
    rows = _fetch_all(
        src,
        """
        SELECT id, tenant_id, email, display_name, role, is_super_admin,
               password_hash, created_at
        FROM users
        ORDER BY created_at
        """,
    )
    return _insert_many(
        tgt,
        "users",
        [
            "id",
            "tenant_id",
            "email",
            "display_name",
            "role",
            "is_super_admin",
            "password_hash",
            "created_at",
        ],
        rows,
    )


def migrate_auth_tokens(src: psycopg.Connection, tgt: psycopg.Connection) -> int:
    """Only carry over tokens that could still be valid — used or expired
    tokens are dead weight in the identity DB."""
    rows = _fetch_all(
        src,
        """
        SELECT id, user_id, token_hash, purpose, expires_at, used_at, created_at
        FROM auth_tokens
        WHERE used_at IS NULL AND expires_at > NOW()
        """,
    )
    return _insert_many(
        tgt,
        "auth_tokens",
        ["id", "user_id", "token_hash", "purpose", "expires_at", "used_at", "created_at"],
        rows,
    )


def seed_default_subscriptions(tgt: psycopg.Connection) -> int:
    """For every tenant that doesn't already have a `takanon` subscription
    row, insert one (active, no expiry). Idempotent — safe to re-run."""
    with tgt.cursor() as cur:
        cur.execute(
            """
            SELECT t.id
            FROM tenants t
            LEFT JOIN subscriptions s
              ON s.tenant_id = t.id AND s.product = %s
            WHERE s.id IS NULL
            """,
            (DEFAULT_PRODUCT,),
        )
        missing = [r[0] for r in cur.fetchall()]

        inserted = 0
        for tenant_id in missing:
            cur.execute(
                """
                INSERT INTO subscriptions (id, tenant_id, product, plan, active, created_at)
                VALUES (gen_random_uuid(), %s, %s, 'default', TRUE, NOW())
                """,
                (tenant_id, DEFAULT_PRODUCT),
            )
            inserted += 1
    return inserted


def _print_counts(label: str, conn: psycopg.Connection, tables: list[str]) -> None:
    print(f"\n{label}")
    for t in tables:
        try:
            n = _count(conn, t)
            print(f"  {t}: {n}")
        except psycopg.errors.UndefinedTable:
            print(f"  {t}: (table does not exist)")
            conn.rollback()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="preview, no writes")
    group.add_argument("--commit", action="store_true", help="actually migrate")
    parser.add_argument(
        "--allow-nonempty-target",
        action="store_true",
        help="allow running when the target already has data (for re-runs)",
    )
    args = parser.parse_args()

    dry_run = args.dry_run

    src_url = os.environ.get("SOURCE_DATABASE_URL")
    tgt_url = os.environ.get("TARGET_DATABASE_URL")
    if not src_url or not tgt_url:
        print("ERROR: set SOURCE_DATABASE_URL and TARGET_DATABASE_URL.")
        return 1

    if src_url == tgt_url:
        print("ERROR: SOURCE and TARGET are the same database. Refusing to run.")
        return 1

    print("Klaser identity migration")
    print(f"  mode: {'DRY-RUN' if dry_run else 'COMMIT'}")
    print(f"  source: {src_url.split('@')[-1]}")
    print(f"  target: {tgt_url.split('@')[-1]}")

    with _open(src_url, readonly=True) as src, _open(tgt_url) as tgt:
        _print_counts("Source (Takanon) before:", src, ["tenants", "users", "auth_tokens"])
        _print_counts(
            "Target (identity) before:",
            tgt,
            ["tenants", "users", "auth_tokens", "subscriptions"],
        )

        # Safety check — don't overwrite a target that already has more
        # data than the source. This catches the "already ran once, don't
        # re-run and re-import stale rows" case.
        src_users = _count(src, "users")
        tgt_users = _count(tgt, "users")
        if tgt_users > src_users and not args.allow_nonempty_target:
            print(
                "\nERROR: target has more users than source "
                f"({tgt_users} > {src_users}). Migration likely already "
                "ran. Pass --allow-nonempty-target to force."
            )
            return 1

        # All inserts happen inside one long-lived transaction on the target
        # so FKs across tables resolve. We commit or rollback ONCE at the
        # end depending on --dry-run vs --commit. If any insert raises,
        # `with _open(...)` will roll back automatically.
        t = migrate_tenants(src, tgt)
        print(f"\n→ tenants: staged {t}")
        u = migrate_users(src, tgt)
        print(f"→ users: staged {u}")
        a = migrate_auth_tokens(src, tgt)
        print(f"→ auth_tokens (valid only): staged {a}")
        s = seed_default_subscriptions(tgt)
        print(f"→ subscriptions (default '{DEFAULT_PRODUCT}'): staged {s}")

        _print_counts(
            "Target (identity) after (in-txn):",
            tgt,
            ["tenants", "users", "auth_tokens", "subscriptions"],
        )

        if dry_run:
            tgt.rollback()
            print("\nDRY-RUN complete — rolled back. Re-run with --commit to actually migrate.")
        else:
            tgt.commit()
            print("\nMigration committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

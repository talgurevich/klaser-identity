"""Diff `users` between the source (Takanon) and target (identity) DBs.

Run after a migration to find rows that didn't cross over cleanly, or
whose UUIDs diverged between the two databases. Useful whenever
post-cutover behavior suggests drift (deletes 404, invites appear on
one side and not the other, etc.).

Usage
-----

    export SOURCE_DATABASE_URL='postgresql://...takanon...'
    export TARGET_DATABASE_URL='postgresql://...identity...'
    python scripts/diff_users.py

Output
------

Four buckets:
  - MATCH — email + id both agree on both sides
  - ID MISMATCH — email in both, id differs (the migration didn't preserve UUIDs)
  - SOURCE ONLY — email in source only (migration missed it)
  - TARGET ONLY — email in target only (created after migration, e.g. new invite)
"""
from __future__ import annotations

import os
import sys

try:
    import psycopg
except ImportError:  # pragma: no cover
    print("Install psycopg first: pip install 'psycopg[binary]'")
    raise SystemExit(1)


def fetch_users(url: str) -> dict[str, str]:
    """Return {email: id_str}. Assumes emails are unique (they are in
    both source and target schemas)."""
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    out: dict[str, str] = {}
    with psycopg.connect(url) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor() as cur:
            cur.execute("SELECT id::text, email FROM users")
            for uid, email in cur.fetchall():
                out[email] = uid
    return out


def main() -> int:
    src_url = os.environ.get("SOURCE_DATABASE_URL")
    tgt_url = os.environ.get("TARGET_DATABASE_URL")
    if not src_url or not tgt_url:
        print("Set SOURCE_DATABASE_URL and TARGET_DATABASE_URL.")
        return 1

    src = fetch_users(src_url)
    tgt = fetch_users(tgt_url)

    src_emails = set(src)
    tgt_emails = set(tgt)

    both = sorted(src_emails & tgt_emails)
    source_only = sorted(src_emails - tgt_emails)
    target_only = sorted(tgt_emails - src_emails)

    matches: list[str] = []
    id_mismatches: list[tuple[str, str, str]] = []
    for email in both:
        if src[email] == tgt[email]:
            matches.append(email)
        else:
            id_mismatches.append((email, src[email], tgt[email]))

    print(f"Source ({src_url.split('@')[-1]}): {len(src)} users")
    print(f"Target ({tgt_url.split('@')[-1]}): {len(tgt)} users")
    print()

    print(f"MATCH (email + id agree): {len(matches)}")
    for e in matches:
        print(f"  ✓  {e}")

    print()
    print(f"ID MISMATCH (email in both, id differs): {len(id_mismatches)}")
    for email, sid, tid in id_mismatches:
        print(f"  ✗  {email}")
        print(f"       source: {sid}")
        print(f"       target: {tid}")

    print()
    print(f"SOURCE ONLY (migration missed these): {len(source_only)}")
    for e in source_only:
        print(f"  ←  {e}  ({src[e]})")

    print()
    print(f"TARGET ONLY (created post-migration): {len(target_only)}")
    for e in target_only:
        print(f"  →  {e}  ({tgt[e]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())

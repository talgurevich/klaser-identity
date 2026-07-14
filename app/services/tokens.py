"""Lifecycle helpers for AuthToken rows (invite-registration + password-reset).

Callers: app.routes.auth (validate/consume), app.routes.service (issue on
invite-user calls from product backends).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuthToken
from app.services.security import generate_raw_token, hash_token

PURPOSE_REGISTRATION = "registration"
PURPOSE_PASSWORD_RESET = "password_reset"


def issue_token(
    db: Session,
    *,
    user_id: UUID,
    purpose: str,
    ttl: timedelta,
    invalidate_existing: bool = True,
) -> str:
    """Create a new token for (user_id, purpose) and return the raw
    (unhashed) value to embed in an email link. By default, any other
    unused token of the same purpose for this user is invalidated first —
    only the most recently issued link should work."""
    if invalidate_existing:
        now = datetime.now(timezone.utc)
        (
            db.query(AuthToken)
            .filter(
                AuthToken.user_id == user_id,
                AuthToken.purpose == purpose,
                AuthToken.used_at.is_(None),
            )
            .update({AuthToken.used_at: now}, synchronize_session=False)
        )

    raw = generate_raw_token()
    row = AuthToken(
        user_id=user_id,
        token_hash=hash_token(raw),
        purpose=purpose,
        expires_at=datetime.now(timezone.utc) + ttl,
    )
    db.add(row)
    db.commit()
    return raw


def find_valid_token(db: Session, *, raw_token: str, purpose: str) -> AuthToken | None:
    """Look up a token by its raw value, returning it only if it matches
    the expected purpose, hasn't been used, and hasn't expired."""
    row = (
        db.query(AuthToken)
        .filter(
            AuthToken.token_hash == hash_token(raw_token),
            AuthToken.purpose == purpose,
        )
        .first()
    )
    if row is None:
        return None
    if row.used_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    return row


def consume_token(db: Session, token: AuthToken) -> None:
    """Mark a token used. Caller must have already validated it via
    find_valid_token in the same request."""
    token.used_at = datetime.now(timezone.utc)
    db.commit()

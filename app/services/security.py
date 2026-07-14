"""Password hashing + auth-token helpers.

Tokens: a random URL-safe string is generated and sent to the user by
email; only its sha256 hash is ever persisted (app.models.AuthToken).
Lookups hash the incoming token and compare, so a DB read alone can't
yield a usable token.
"""
from __future__ import annotations

import hashlib
import secrets

import bcrypt

from app.config import settings

# bcrypt silently truncates past 72 bytes — enforce a sane range so a very
# long password doesn't quietly collapse to a shorter effective one.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72


def validate_password_strength(password: str) -> str | None:
    """Return an error message (Hebrew, for direct display) or None if OK."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"הסיסמה חייבת להכיל לפחות {MIN_PASSWORD_LENGTH} תווים"
    if len(password.encode("utf-8")) > MAX_PASSWORD_LENGTH:
        return "הסיסמה ארוכה מדי"
    return None


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_raw_token() -> str:
    """32 bytes URL-safe — the value that goes in the email link."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Deterministic hash for DB storage/lookup. Plain sha256 is fine
    here — the raw tokens are already high-entropy random strings, not
    human-chosen secrets, so bcrypt would just add cost."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

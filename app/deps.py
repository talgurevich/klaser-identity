"""Shared FastAPI dependencies: current_user + service_auth."""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Tenant, User


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Return the authenticated User. Super-admin switch-mode support
    ported from Takanon: while `session.viewing_tenant_id` is set, mutate
    `user.tenant_id` in memory so the /me response reflects the viewed
    tenant. The DB row is never written back — purely per-request."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")

    user._home_tenant_id = user.tenant_id  # type: ignore[attr-defined]
    user._in_switch_mode = False  # type: ignore[attr-defined]

    if user.is_super_admin:
        viewing = request.session.get("viewing_tenant_id")
        if viewing and viewing != str(user.tenant_id):
            try:
                viewing_uuid = UUID(viewing)
            except (ValueError, TypeError):
                viewing_uuid = None
            if viewing_uuid is not None and db.get(Tenant, viewing_uuid) is not None:
                user.tenant_id = viewing_uuid  # type: ignore[assignment]
                user._in_switch_mode = True  # type: ignore[attr-defined]

    return user


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """Guard for /api/service/* — product backends call with a shared
    bearer token. Rotate by adding the new token to SERVICE_TOKENS,
    redeploying every consumer, then removing the old one."""
    tokens = settings.service_tokens_set
    if not tokens:
        raise HTTPException(status_code=503, detail="Service tokens not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token not in tokens:
        raise HTTPException(status_code=401, detail="Invalid service token")

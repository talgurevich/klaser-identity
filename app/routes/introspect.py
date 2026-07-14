"""Session introspection — the primary auth path for product backends.

A product backend (Takanon, Meetings, …) receives a browser request with
the shared `klaser_session` cookie attached. It forwards the cookie value
to this endpoint, and gets back the user, tenant, and entitlements —
without ever having to decode the session itself. This keeps session
format changes contained to the identity service.

Design note: we deliberately do NOT expose a service-token variant of
this endpoint. If a product backend needs to look up a user outside a
session context (background job, cron), it uses `/api/service/users/{id}`
under `service_token` auth — that path is separate on purpose.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Subscription, Tenant, User
from app.routes.auth import _entitlements_for_tenant

router = APIRouter()


class IntrospectResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    role: str
    is_super_admin: bool
    tenant_id: str
    tenant_name: str | None
    entitlements: list[str]
    # True when the caller is a super-admin currently viewing a tenant
    # that isn't their home tenant. Product backends use this to enforce
    # read-only semantics on non-whitelisted write routes.
    viewing_other_tenant: bool = False
    # For product backends that want to render a debug string
    session_source: str = "cookie"


@router.get("/introspect", response_model=IntrospectResponse)
def introspect(
    request: Request,
    db: Session = Depends(get_db),
) -> IntrospectResponse:
    """Return the authenticated user + tenant + entitlements based on the
    current `klaser_session` cookie. 401 if there's no session or the user
    referenced by the session no longer exists."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No session")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session references unknown user")

    # Honor super-admin switch-mode — the product backend sees the tenant
    # the super-admin is viewing, not their home tenant.
    effective_tenant_id = user.tenant_id
    viewing_other = False
    if user.is_super_admin:
        viewing = request.session.get("viewing_tenant_id")
        if viewing:
            try:
                from uuid import UUID as _UUID

                viewing_uuid = _UUID(viewing)
            except (ValueError, TypeError):
                viewing_uuid = None
            if viewing_uuid is not None and db.get(Tenant, viewing_uuid) is not None:
                effective_tenant_id = viewing_uuid
                viewing_other = viewing_uuid != user.tenant_id

    tenant = db.get(Tenant, effective_tenant_id)

    return IntrospectResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_super_admin=user.is_super_admin,
        tenant_id=str(effective_tenant_id),
        tenant_name=tenant.name if tenant else None,
        entitlements=_entitlements_for_tenant(db, effective_tenant_id),
        viewing_other_tenant=viewing_other,
    )

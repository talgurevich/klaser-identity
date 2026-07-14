"""Service-token endpoints — product backends call these outside a
request context (background jobs, cron, admin scripts).

All routes here require `Authorization: Bearer <service-token>`. Tokens
are configured via the `SERVICE_TOKENS` env var (comma-separated); each
product backend gets its own so revocation is granular.

What's here:
- `POST /api/service/users` — a product backend can create (invite) a
  user in a tenant. Issues a registration token and emails the invite.
- `GET /api/service/users/{id}` — look up a user by ID.
- `GET /api/service/tenants/{id}` — look up a tenant by ID.
- `GET /api/service/tenants/{id}/subscriptions` — list a tenant's product
  entitlements (active + inactive).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_service_token
from app.models import Subscription, Tenant, User
from app.services.mail import send_registration_invite
from app.services.tokens import PURPOSE_REGISTRATION, issue_token

log = structlog.get_logger()
router = APIRouter(dependencies=[Depends(require_service_token)])


# ─────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────


class ServiceUserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    is_super_admin: bool
    tenant_id: str


class InviteUserRequest(BaseModel):
    email: str
    tenant_id: str
    role: str  # admin | reviewer | secretary
    display_name: str | None = None
    invited_by: str | None = None  # display name of the inviter, purely for the email


@router.post("/users", response_model=ServiceUserOut, status_code=201)
def invite_user(
    payload: InviteUserRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ServiceUserOut:
    email = payload.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email")

    try:
        tenant_uuid = UUID(payload.tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    tenant = db.get(Tenant, tenant_uuid)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        raise HTTPException(409, "User already exists")

    user = User(
        email=email,
        tenant_id=tenant_uuid,
        role=payload.role,
        display_name=(payload.display_name or "").strip() or None,
    )
    db.add(user)
    db.flush()

    raw_token = issue_token(
        db,
        user_id=user.id,
        purpose=PURPOSE_REGISTRATION,
        ttl=timedelta(days=settings.registration_token_ttl_days),
    )
    invite_url = f"{settings.post_auth_redirect_url.rstrip('/')}/register?token={raw_token}"

    background_tasks.add_task(
        send_registration_invite,
        to_email=user.email,
        display_name=user.display_name,
        tenant_name=tenant.name,
        role=user.role,
        invited_by=payload.invited_by,
        invite_url=invite_url,
    )

    log.info("service.user_invited", user_id=str(user.id), tenant_id=str(tenant_uuid))
    return ServiceUserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_super_admin=user.is_super_admin,
        tenant_id=str(user.tenant_id),
    )


@router.get("/users/{user_id}", response_model=ServiceUserOut)
def get_user(user_id: str, db: Session = Depends(get_db)) -> ServiceUserOut:
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid user_id") from e
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "User not found")
    return ServiceUserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_super_admin=user.is_super_admin,
        tenant_id=str(user.tenant_id),
    )


# ─────────────────────────────────────────────────────────────────────────
# Tenants + subscriptions
# ─────────────────────────────────────────────────────────────────────────


class ServiceTenantOut(BaseModel):
    id: str
    name: str
    segment: str
    system_context: str | None


@router.get("/tenants/{tenant_id}", response_model=ServiceTenantOut)
def get_tenant(tenant_id: str, db: Session = Depends(get_db)) -> ServiceTenantOut:
    try:
        tid = UUID(tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    tenant = db.get(Tenant, tid)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    return ServiceTenantOut(
        id=str(tenant.id),
        name=tenant.name,
        segment=tenant.segment,
        system_context=tenant.system_context,
    )


class SubscriptionOut(BaseModel):
    id: str
    product: str
    plan: str
    active: bool
    expires_at: datetime | None
    created_at: datetime


@router.get("/tenants/{tenant_id}/subscriptions", response_model=list[SubscriptionOut])
def list_tenant_subscriptions(
    tenant_id: str, db: Session = Depends(get_db)
) -> list[SubscriptionOut]:
    try:
        tid = UUID(tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    if db.get(Tenant, tid) is None:
        raise HTTPException(404, "Tenant not found")
    rows = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tid)
        .order_by(Subscription.product)
        .all()
    )
    return [
        SubscriptionOut(
            id=str(r.id),
            product=r.product,
            plan=r.plan,
            active=r.active,
            expires_at=r.expires_at,
            created_at=r.created_at,
        )
        for r in rows
    ]

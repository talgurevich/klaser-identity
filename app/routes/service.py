"""Service-token endpoints — product backends call these outside a
request context (background jobs, cron, admin scripts).

All routes here require `Authorization: Bearer <service-token>`. Tokens
are configured via the `SERVICE_TOKENS` env var (comma-separated); each
product backend gets its own so revocation is granular.

What's here:
- Users
  - `POST /api/service/users` — invite (creates user + emails token).
  - `GET  /api/service/users` — list, optional `tenant_id` filter.
  - `GET  /api/service/users/{id}` — lookup.
  - `PATCH /api/service/users/{id}` — update role/name/tenant/super-admin.
  - `DELETE /api/service/users/{id}` — hard-delete.
  - `POST /api/service/users/{id}/resend-invite` — new token + email.
- Tenants
  - `POST /api/service/tenants` — create (auto-seeds `takanon` sub).
  - `GET  /api/service/tenants` — list.
  - `GET  /api/service/tenants/{id}` — lookup.
  - `GET  /api/service/tenants/{id}/subscriptions` — list entitlements.
  - `POST /api/service/tenants/{id}/subscriptions` — grant entitlement
    (idempotent: reactivates an existing row if one exists).
  - `DELETE /api/service/subscriptions/{id}` — revoke (hard-delete).
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
    # True once the user has completed invite-registration or set a password
    # via reset. Google-only users stay False; drives the "resend invite"
    # affordance in product admin panels.
    has_password: bool = False
    created_at: datetime | None = None


def _user_to_out(u: User) -> ServiceUserOut:
    return ServiceUserOut(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        is_super_admin=u.is_super_admin,
        tenant_id=str(u.tenant_id),
        has_password=u.password_hash is not None,
        created_at=u.created_at,
    )


class InviteUserRequest(BaseModel):
    email: str
    tenant_id: str
    role: str  # admin | reviewer | secretary
    display_name: str | None = None
    invited_by: str | None = None  # display name of the inviter, purely for the email
    is_super_admin: bool = False  # promoted at invite time; skips a follow-up PATCH


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
        is_super_admin=payload.is_super_admin,
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
    return _user_to_out(user)


@router.get("/users/{user_id}", response_model=ServiceUserOut)
def get_user(user_id: str, db: Session = Depends(get_db)) -> ServiceUserOut:
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid user_id") from e
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "User not found")
    return _user_to_out(user)


@router.get("/users", response_model=list[ServiceUserOut])
def list_users(
    tenant_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[ServiceUserOut]:
    """List every user. Optional `tenant_id` filter. Ordered by email
    for stable pagination if we ever need it — small dataset today."""
    q = db.query(User)
    if tenant_id:
        try:
            tid = UUID(tenant_id)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, "Invalid tenant_id") from e
        q = q.filter(User.tenant_id == tid)
    rows = q.order_by(User.email).all()
    return [_user_to_out(u) for u in rows]


class UpdateUserRequest(BaseModel):
    role: str | None = None
    display_name: str | None = None
    tenant_id: str | None = None
    is_super_admin: bool | None = None


@router.patch("/users/{user_id}", response_model=ServiceUserOut)
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    db: Session = Depends(get_db),
) -> ServiceUserOut:
    """Update mutable fields on a user. Fields absent from the payload
    are left untouched (`display_name` set to empty string is treated as
    explicit clear, not "leave alone")."""
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid user_id") from e
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "User not found")

    if payload.role is not None:
        user.role = payload.role
    if payload.display_name is not None:
        user.display_name = payload.display_name or None
    if payload.tenant_id is not None:
        try:
            new_tid = UUID(payload.tenant_id)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, "Invalid tenant_id") from e
        if db.get(Tenant, new_tid) is None:
            raise HTTPException(404, "Tenant not found")
        user.tenant_id = new_tid
    if payload.is_super_admin is not None:
        user.is_super_admin = payload.is_super_admin

    db.commit()
    db.refresh(user)
    log.info("service.user_updated", user_id=str(user.id))
    return _user_to_out(user)


@router.delete("/users/{user_id}")
def delete_user(user_id: str, db: Session = Depends(get_db)) -> dict:
    """Hard-delete a user. Cascades to auth_tokens via the FK's
    ON DELETE CASCADE. Callers are responsible for guarding against
    self-deletion — the caller here has no session context.

    Idempotent: a missing user is treated as "already gone" (200 +
    `already_absent=True`) rather than 404. This lets product admin
    panels drift out of sync with identity without exploding on the
    cleanup path — the more common case in the wild than a genuinely
    invalid ID."""
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid user_id") from e
    user = db.get(User, uid)
    if user is None:
        log.info("service.user_delete_missing", user_id=user_id)
        return {"status": "ok", "already_absent": True}
    db.delete(user)
    db.commit()
    log.info("service.user_deleted", user_id=user_id)
    return {"status": "ok", "already_absent": False}


class ResendInviteResponse(BaseModel):
    status: str
    email: str


@router.post("/users/{user_id}/resend-invite", response_model=ResendInviteResponse)
def resend_invite(
    user_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ResendInviteResponse:
    """Re-issue a registration token for a user who hasn't set a
    password yet, and email a fresh invite. Refuses once the user
    already has a password — use forgot-password for that case."""
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid user_id") from e
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.password_hash is not None:
        raise HTTPException(
            409, "המשתמש כבר הגדיר סיסמה — יש להשתמש באיפוס סיסמה במקום."
        )
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

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
        invited_by=None,  # service-token call has no acting user context
        invite_url=invite_url,
    )
    log.info("service.invite_resent", user_id=str(user.id))
    return ResendInviteResponse(status="ok", email=user.email)


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


class UpdateTenantSystemContextRequest(BaseModel):
    """Empty string / whitespace-only is treated as explicit clear
    (system_context set to NULL). Absent field is not allowed — use
    None inside the JSON if you mean clear."""

    system_context: str | None


@router.patch(
    "/tenants/{tenant_id}/system-context", response_model=ServiceTenantOut
)
def update_tenant_system_context(
    tenant_id: str,
    payload: UpdateTenantSystemContextRequest,
    db: Session = Depends(get_db),
) -> ServiceTenantOut:
    """Set the per-tenant system_context used by product answer-paths
    (currently Takanon's LLM). Identity is the source of truth; product
    backends read via /api/service/tenants/{id} + a short-lived cache."""
    try:
        tid = UUID(tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    tenant = db.get(Tenant, tid)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    val = (payload.system_context or "").strip()
    tenant.system_context = val if val else None
    db.commit()
    log.info(
        "service.tenant_context_updated",
        tenant_id=str(tenant.id),
        length=len(val),
    )
    return ServiceTenantOut(
        id=str(tenant.id),
        name=tenant.name,
        segment=tenant.segment,
        system_context=tenant.system_context,
    )


@router.get("/tenants", response_model=list[ServiceTenantOut])
def list_tenants(db: Session = Depends(get_db)) -> list[ServiceTenantOut]:
    """List every tenant. Ordered by name."""
    rows = db.query(Tenant).order_by(Tenant.name).all()
    return [
        ServiceTenantOut(
            id=str(t.id),
            name=t.name,
            segment=t.segment,
            system_context=t.system_context,
        )
        for t in rows
    ]


class CreateTenantRequest(BaseModel):
    name: str
    segment: str
    seed_default_subscription: bool = True


VALID_SEGMENTS = {"kibbutz_shitufi", "kibbutz_mitchadesh", "moshav"}


@router.post("/tenants", response_model=ServiceTenantOut, status_code=201)
def create_tenant(
    payload: CreateTenantRequest,
    db: Session = Depends(get_db),
) -> ServiceTenantOut:
    """Create a new tenant. Auto-seeds a `takanon` subscription unless
    the caller opts out — that's the default entitlement every tenant
    starts with today. Meetings entitlements are granted separately once
    that product goes live."""
    if payload.segment not in VALID_SEGMENTS:
        raise HTTPException(400, f"Invalid segment. Allowed: {sorted(VALID_SEGMENTS)}")
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    if db.query(Tenant).filter(Tenant.name == name).first():
        raise HTTPException(409, f"Tenant with name {name!r} already exists")

    tenant = Tenant(name=name, segment=payload.segment)
    db.add(tenant)
    db.flush()

    if payload.seed_default_subscription:
        db.add(
            Subscription(
                tenant_id=tenant.id,
                product="takanon",
                plan="default",
                active=True,
            )
        )
    db.commit()
    db.refresh(tenant)
    log.info("service.tenant_created", tenant_id=str(tenant.id), name=name)
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


# Product IDs identity knows about. Kept small on purpose — adding a new
# product to the platform is a deliberate act, not something an admin
# should be able to typo into existence from a UI.
VALID_PRODUCTS = {"takanon", "meetings"}


class GrantSubscriptionRequest(BaseModel):
    product: str
    plan: str = "default"
    expires_at: datetime | None = None


@router.post(
    "/tenants/{tenant_id}/subscriptions",
    response_model=SubscriptionOut,
    status_code=201,
)
def grant_subscription(
    tenant_id: str,
    payload: GrantSubscriptionRequest,
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    """Grant a product entitlement to a tenant. Idempotent: if a row for
    (tenant, product) already exists, reactivate it and update plan/expiry
    rather than inserting a duplicate."""
    try:
        tid = UUID(tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    if db.get(Tenant, tid) is None:
        raise HTTPException(404, "Tenant not found")
    product = payload.product.strip().lower()
    if product not in VALID_PRODUCTS:
        raise HTTPException(
            400, f"Unknown product {product!r}. Allowed: {sorted(VALID_PRODUCTS)}"
        )

    existing = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tid, Subscription.product == product)
        .one_or_none()
    )
    if existing is not None:
        existing.active = True
        existing.plan = payload.plan
        existing.expires_at = payload.expires_at
        sub = existing
    else:
        sub = Subscription(
            tenant_id=tid,
            product=product,
            plan=payload.plan,
            active=True,
            expires_at=payload.expires_at,
        )
        db.add(sub)
    db.commit()
    db.refresh(sub)
    log.info(
        "service.subscription_granted",
        tenant_id=str(tid),
        product=product,
        reactivated=existing is not None,
    )
    return SubscriptionOut(
        id=str(sub.id),
        product=sub.product,
        plan=sub.plan,
        active=sub.active,
        expires_at=sub.expires_at,
        created_at=sub.created_at,
    )


@router.delete("/subscriptions/{subscription_id}", status_code=204)
def revoke_subscription(subscription_id: str, db: Session = Depends(get_db)) -> None:
    """Hard-delete a subscription row. Product access stops on the next
    request (identity is not cached at the entitlement level)."""
    try:
        sid = UUID(subscription_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid subscription_id") from e
    sub = db.get(Subscription, sid)
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    tenant_id = str(sub.tenant_id)
    product = sub.product
    db.delete(sub)
    db.commit()
    log.info(
        "service.subscription_revoked",
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        product=product,
    )

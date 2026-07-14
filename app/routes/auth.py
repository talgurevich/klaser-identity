"""Google OAuth + email/password auth + tenant switching.

Every endpoint here reads / writes the shared `klaser_session` cookie
(scoped to `.klaser.co.il` in production). Product backends do not call
these routes directly — they call `GET /api/introspect` (see
`app.routes.introspect`) to read the session on behalf of a browser request.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.models import Subscription, Tenant, User
from app.services.mail import send_password_reset_email, send_welcome_email
from app.services.security import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.services.tokens import (
    PURPOSE_PASSWORD_RESET,
    PURPOSE_REGISTRATION,
    consume_token,
    find_valid_token,
    issue_token,
)

log = structlog.get_logger()
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────
# Shape of the /me response — mirrors Takanon's for backwards compat, with
# `entitlements` added so product frontends can render a switcher.
# ─────────────────────────────────────────────────────────────────────────


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    tenant_id: str
    tenant_name: str | None = None
    is_super_admin: bool = False
    home_tenant_id: str | None = None
    home_tenant_name: str | None = None
    viewing_other_tenant: bool = False
    entitlements: list[str] = []


def _entitlements_for_tenant(db: Session, tenant_id: UUID) -> list[str]:
    """Return the list of product IDs the tenant currently has access to.
    An entitlement counts if `active=True` and `expires_at` is null or in
    the future. Result is deterministically ordered so the frontend can
    render a stable switcher."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rows = (
        db.query(Subscription.product)
        .filter(
            Subscription.tenant_id == tenant_id,
            Subscription.active.is_(True),
        )
        .all()
    )
    out: list[str] = []
    for (product,) in rows:
        row = (
            db.query(Subscription)
            .filter(
                Subscription.tenant_id == tenant_id,
                Subscription.product == product,
                Subscription.active.is_(True),
            )
            .first()
        )
        if row is None:
            continue
        if row.expires_at is None or row.expires_at > now:
            out.append(product)
    return sorted(set(out))


def _user_to_response(user: User, db: Session) -> MeResponse:
    effective_tenant_id = user.tenant_id
    home_tenant_id = getattr(user, "_home_tenant_id", None) or effective_tenant_id
    viewing_other = bool(getattr(user, "_in_switch_mode", False))

    effective_tenant = db.get(Tenant, effective_tenant_id)
    home_tenant = (
        db.get(Tenant, home_tenant_id)
        if home_tenant_id != effective_tenant_id
        else effective_tenant
    )

    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(effective_tenant_id),
        tenant_name=effective_tenant.name if effective_tenant else None,
        is_super_admin=user.is_super_admin,
        home_tenant_id=str(home_tenant_id),
        home_tenant_name=home_tenant.name if home_tenant else None,
        viewing_other_tenant=viewing_other,
        entitlements=_entitlements_for_tenant(db, effective_tenant_id),
    )


# ─────────────────────────────────────────────────────────────────────────
# Google OAuth
# ─────────────────────────────────────────────────────────────────────────


class GoogleLoginRequest(BaseModel):
    credential: str  # Google ID token (JWT) from GIS


@router.post("/google", response_model=MeResponse)
def google_login(
    payload: GoogleLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MeResponse:
    if not settings.google_client_id:
        raise HTTPException(500, "Google OAuth not configured on server")
    try:
        info = google_id_token.verify_oauth2_token(
            payload.credential,
            google_requests.Request(),
            settings.google_client_id,
        )
    except ValueError as e:
        raise HTTPException(401, f"Invalid Google credential: {e}") from e

    email = (info.get("email") or "").lower().strip()
    if not email or not info.get("email_verified"):
        raise HTTPException(401, "Google account email not verified")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            403,
            "המשתמש לא קיים במערכת. פנה למנהל לקבלת הרשאה.",
        )

    if not user.display_name and info.get("name"):
        user.display_name = info["name"]
        db.commit()

    request.session["user_id"] = str(user.id)
    request.session["is_super_admin"] = bool(user.is_super_admin)
    request.session.pop("viewing_tenant_id", None)
    return _user_to_response(user, db)


# ─────────────────────────────────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=MeResponse)
def me(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> MeResponse:
    return _user_to_response(user, db)


@router.post("/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────
# Email/password registration (invite-only — the token is the invite)
# ─────────────────────────────────────────────────────────────────────────


class RegistrationInfo(BaseModel):
    email: str
    display_name: str | None
    tenant_name: str
    role: str


@router.get("/registration/{token}", response_model=RegistrationInfo)
def get_registration_info(token: str, db: Session = Depends(get_db)) -> RegistrationInfo:
    auth_token = find_valid_token(db, raw_token=token, purpose=PURPOSE_REGISTRATION)
    if auth_token is None:
        raise HTTPException(400, "קישור ההרשמה אינו תקף או שפג תוקפו")
    user = db.get(User, auth_token.user_id)
    if user is None:
        raise HTTPException(404, "המשתמש לא נמצא")
    tenant = db.get(Tenant, user.tenant_id)
    return RegistrationInfo(
        email=user.email,
        display_name=user.display_name,
        tenant_name=tenant.name if tenant else "",
        role=user.role,
    )


class RegisterRequest(BaseModel):
    token: str
    password: str
    display_name: str | None = None


@router.post("/register", response_model=MeResponse)
def register(
    payload: RegisterRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> MeResponse:
    auth_token = find_valid_token(db, raw_token=payload.token, purpose=PURPOSE_REGISTRATION)
    if auth_token is None:
        raise HTTPException(400, "קישור ההרשמה אינו תקף או שפג תוקפו")
    user = db.get(User, auth_token.user_id)
    if user is None:
        raise HTTPException(404, "המשתמש לא נמצא")
    if user.password_hash is not None:
        # Token should already be single-use, but guard against a race
        # rather than silently overwrite an existing password.
        raise HTTPException(409, "החשבון כבר מוגדר. נסה להתחבר או לאפס סיסמה.")

    error = validate_password_strength(payload.password)
    if error:
        raise HTTPException(400, error)

    if payload.display_name and payload.display_name.strip():
        user.display_name = payload.display_name.strip()

    user.password_hash = hash_password(payload.password)
    consume_token(db, auth_token)  # commits password_hash + display_name too
    db.refresh(user)

    tenant = db.get(Tenant, user.tenant_id)
    background_tasks.add_task(
        send_welcome_email,
        to_email=user.email,
        display_name=user.display_name,
        tenant_name=tenant.name if tenant else "",
    )

    request.session["user_id"] = str(user.id)
    request.session["is_super_admin"] = bool(user.is_super_admin)
    request.session.pop("viewing_tenant_id", None)
    log.info("auth.registered", user_id=str(user.id))
    return _user_to_response(user, db)


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login", response_model=MeResponse)
def password_login(
    payload: PasswordLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MeResponse:
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if (
        user is None
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
    ):
        # Same error whether the email doesn't exist, has no password
        # set, or the password is wrong.
        raise HTTPException(401, "אימייל או סיסמה שגויים")

    request.session["user_id"] = str(user.id)
    request.session["is_super_admin"] = bool(user.is_super_admin)
    request.session.pop("viewing_tenant_id", None)
    return _user_to_response(user, db)


# ─────────────────────────────────────────────────────────────────────────
# Password reset
# ─────────────────────────────────────────────────────────────────────────


class ForgotPasswordRequest(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Always returns the same generic response — doesn't leak whether
    the email belongs to an account."""
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if user is not None:
        raw_token = issue_token(
            db,
            user_id=user.id,
            purpose=PURPOSE_PASSWORD_RESET,
            ttl=timedelta(hours=settings.password_reset_token_ttl_hours),
        )
        reset_url = (
            f"{settings.post_auth_redirect_url.rstrip('/')}/reset-password?token={raw_token}"
        )
        background_tasks.add_task(
            send_password_reset_email,
            to_email=user.email,
            display_name=user.display_name,
            reset_url=reset_url,
            ttl_hours=settings.password_reset_token_ttl_hours,
        )
    return {"status": "ok"}


class ResetPasswordInfo(BaseModel):
    email: str


@router.get("/reset-password/{token}", response_model=ResetPasswordInfo)
def get_reset_password_info(token: str, db: Session = Depends(get_db)) -> ResetPasswordInfo:
    auth_token = find_valid_token(db, raw_token=token, purpose=PURPOSE_PASSWORD_RESET)
    if auth_token is None:
        raise HTTPException(400, "קישור איפוס הסיסמה אינו תקף או שפג תוקפו")
    user = db.get(User, auth_token.user_id)
    if user is None:
        raise HTTPException(404, "המשתמש לא נמצא")
    return ResetPasswordInfo(email=user.email)


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/reset-password", response_model=MeResponse)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MeResponse:
    auth_token = find_valid_token(db, raw_token=payload.token, purpose=PURPOSE_PASSWORD_RESET)
    if auth_token is None:
        raise HTTPException(400, "קישור איפוס הסיסמה אינו תקף או שפג תוקפו")
    user = db.get(User, auth_token.user_id)
    if user is None:
        raise HTTPException(404, "המשתמש לא נמצא")

    error = validate_password_strength(payload.password)
    if error:
        raise HTTPException(400, error)

    user.password_hash = hash_password(payload.password)
    consume_token(db, auth_token)
    db.refresh(user)

    # Auto-login, matching registration.
    request.session["user_id"] = str(user.id)
    request.session["is_super_admin"] = bool(user.is_super_admin)
    request.session.pop("viewing_tenant_id", None)
    log.info("auth.password_reset", user_id=str(user.id))
    return _user_to_response(user, db)


# ─────────────────────────────────────────────────────────────────────────
# Super-admin tenant switching
# ─────────────────────────────────────────────────────────────────────────


class TenantItem(BaseModel):
    id: str
    name: str
    segment: str


class SwitchTenantRequest(BaseModel):
    tenant_id: str


@router.get("/tenants", response_model=list[TenantItem])
def list_tenants_for_switcher(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[TenantItem]:
    """List every tenant — super-admin only. Drives the UI tenant-switcher."""
    if not user.is_super_admin:
        raise HTTPException(403, "Forbidden")
    rows = db.query(Tenant).order_by(Tenant.name).all()
    return [TenantItem(id=str(t.id), name=t.name, segment=t.segment) for t in rows]


@router.post("/switch-tenant", response_model=MeResponse)
def switch_tenant(
    req: SwitchTenantRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> MeResponse:
    if not user.is_super_admin:
        raise HTTPException(403, "Forbidden")
    try:
        tid = UUID(req.tenant_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid tenant_id") from e
    tenant = db.get(Tenant, tid)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    home_id = getattr(user, "_home_tenant_id", user.tenant_id)
    if str(tid) == str(home_id):
        request.session.pop("viewing_tenant_id", None)
    else:
        request.session["viewing_tenant_id"] = str(tid)

    fresh_user = db.query(User).filter(User.id == user.id).first()
    fresh_user._home_tenant_id = home_id  # type: ignore[attr-defined]
    fresh_user._in_switch_mode = (str(tid) != str(home_id))  # type: ignore[attr-defined]
    if fresh_user._in_switch_mode:
        fresh_user.tenant_id = tid  # type: ignore[assignment]
    return _user_to_response(fresh_user, db)


@router.post("/exit-switch", response_model=MeResponse)
def exit_switch(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> MeResponse:
    request.session.pop("viewing_tenant_id", None)
    user._in_switch_mode = False  # type: ignore[attr-defined]
    user.tenant_id = getattr(user, "_home_tenant_id", user.tenant_id)  # type: ignore[assignment]
    return _user_to_response(user, db)

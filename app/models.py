"""SQLAlchemy models for the identity service.

Ported from the Takanon backend (see docs/klaser-platform-infra.md §9.3
data-ownership rule): this service owns users, tenants, auth tokens, and
subscriptions. Nothing else lives here.
"""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy import UUID as SQLUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    segment: Mapped[str] = mapped_column(String, nullable=False)  # kibbutz_shitufi | kibbutz_mitchadesh | moshav
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Free-text block injected into per-product answerer prompts. Product
    # backends fetch this via /api/service/tenants/{id} — the field lives
    # here because it's shared across products and is tenant-scoped.
    system_context: Mapped[str | None] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    # Primary tenant. Multi-tenant memberships can be added later via a
    # user_tenants join table; for now every user belongs to exactly one.
    tenant_id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, nullable=False)  # admin | reviewer | secretary
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Bcrypt hash — NULL until the user completes invite-registration or
    # sets a password via reset. Google sign-in never touches this column.
    password_hash: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthToken(Base):
    """Single-use, expiring token backing invite-registration and
    password-reset links. Raw token only ever leaves the DB inside an
    email link; we store its sha256 hash so a DB leak alone can't
    complete a registration or reset."""

    __tablename__ = "auth_tokens"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        SQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)  # registration | password_reset
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """Per-tenant product entitlement. `product` is one of the string
    IDs (`takanon`, `meetings`). `plan` is free-form so business tier
    naming can evolve without a schema change. `active=True` and a null
    or future `expires_at` means the tenant currently has access."""

    __tablename__ = "subscriptions"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SQLUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    product: Mapped[str] = mapped_column(String, nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String, nullable=False, default="default", server_default="default")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

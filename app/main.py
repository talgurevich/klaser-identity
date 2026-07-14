"""FastAPI application entry point — Klaser identity service."""
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.routes import auth, health, introspect, service

log = structlog.get_logger()

app = FastAPI(
    title="Klaser identity",
    description="Shared identity service for Klaser products (Takanon, Meetings, …).",
    version="0.1.0",
)

# CORS — product frontends live on different subdomains under klaser.co.il
# and call this service directly for login/registration. allow_credentials
# is required for cross-site cookie use.
_origins = settings.allowed_frontends_list or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In production, every product frontend and this service live on subdomains
# of klaser.co.il — so the session cookie is scoped to `.klaser.co.il` and
# shared across all of them. Locally, SESSION_COOKIE_DOMAIN is empty so the
# cookie is host-only on localhost (works for same-origin dev calls).
_is_dev = settings.app_env == "development"
_cookie_domain = settings.session_cookie_domain or None
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie_name,
    domain=_cookie_domain,
    same_site="lax" if _is_dev else "none",
    https_only=not _is_dev,
    max_age=60 * 60 * 24 * 30,  # 30 days
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(introspect.router, prefix="/api", tags=["introspect"])
app.include_router(service.router, prefix="/api/service", tags=["service"])


@app.on_event("startup")
async def startup() -> None:
    log.info(
        "identity.startup",
        env=settings.app_env,
        cookie_domain=_cookie_domain or "(host-only)",
        allowed_frontends=settings.allowed_frontends_list,
    )

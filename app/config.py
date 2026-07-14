"""Environment-driven config for the identity service."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    identity_url: str = "http://localhost:8001"
    post_auth_redirect_url: str = "http://localhost:5173"
    allowed_frontends: str = "http://localhost:5173"

    # Session
    session_secret: str = "dev-secret-change-me"
    # In prod, set to ".klaser.co.il" so the cookie is readable by every product frontend.
    # Leave empty for local dev (host-only cookie on localhost).
    session_cookie_domain: str = ""
    session_cookie_name: str = "klaser_session"

    # Database
    database_url: str = "postgresql+psycopg://localhost/klaser_identity"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Render exposes Postgres as postgresql:// — convert to the psycopg3 driver scheme."""
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        if isinstance(v, str) and v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        return v

    # Google OAuth — single client for all Klaser products
    google_client_id: str = ""

    # Password / token config
    bcrypt_rounds: int = 12
    registration_token_ttl_days: int = 7
    password_reset_token_ttl_hours: int = 1

    # Mail — Resend
    resend_api_key: str = ""
    mail_from_email: str = "noreply@klaser.co.il"
    mail_from_name: str = "Klaser"

    # Service tokens for product-backend calls to /api/service/*
    # Comma-separated. Each product gets its own; rotate by adding new then removing old.
    service_tokens: str = ""

    @property
    def allowed_frontends_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_frontends.split(",") if o.strip()]

    @property
    def service_tokens_set(self) -> set[str]:
        return {t.strip() for t in self.service_tokens.split(",") if t.strip()}


settings = Settings()

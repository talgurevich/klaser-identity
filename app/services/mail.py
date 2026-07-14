"""Transactional email via Resend.

Every template routes through ``_wrap_html`` — Gmail and Outlook strip
``<html>``/``<body>`` and their CSS on render, so ``dir="rtl"`` there is
lost. The wrapper repeats ``dir="rtl"`` and inline direction/text-align
on every wrapper div so RTL survives the strip in every client. Do NOT
build a new wrapper without those.

If ``RESEND_API_KEY`` is empty (local dev without keys), ``_send`` logs
the payload and returns without hitting the network.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

import resend
import structlog

from app.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    html_body: str
    text_body: str


def _from_line() -> str:
    name = (settings.mail_from_name or "Klaser").strip()
    email = settings.mail_from_email
    return f"{name} <{email}>" if name else email


def _send(msg: Message) -> None:
    """Fire-and-forget send. Never raises."""
    if not settings.resend_api_key:
        log.info(
            "mail.dry_run",
            to=msg.to,
            subject=msg.subject,
            reason="RESEND_API_KEY not set",
        )
        return
    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send(
            {
                "from": _from_line(),
                "to": [msg.to],
                "subject": msg.subject,
                "html": msg.html_body,
                "text": msg.text_body,
            }
        )
        log.info("mail.sent", to=msg.to, subject=msg.subject)
    except Exception as e:  # noqa: BLE001 — must not propagate
        log.warning("mail.send_failed", to=msg.to, error=str(e))


# ─────────────────────────────────────────────────────────────────────────
# Shared RTL-safe HTML wrapper
# ─────────────────────────────────────────────────────────────────────────


_BASE_STYLE = """
  <meta charset="utf-8">
  <style>
    body { margin: 0; padding: 0; background: #fafaf9; font-family: 'Heebo', 'Assistant', system-ui, sans-serif; color: #171717; direction: rtl; }
    a { color: #b8412b; text-decoration: none; }
    .btn { display: inline-block; background: #171717; color: #fafaf9 !important; text-decoration: none;
           padding: 14px 28px; font-weight: 700; letter-spacing: 0.02em; }
    .muted { color: #525252; font-size: 13px; line-height: 1.6; }
    .card { max-width: 560px; margin: 0 auto; background: #fafaf9; border: 1px solid #e7e5e4; padding: 40px 32px; direction: rtl; text-align: right; }
    h1 { font-size: 28px; font-weight: 900; margin: 0 0 12px; letter-spacing: -0.01em; }
    p  { line-height: 1.65; margin: 0 0 12px; font-size: 15px; }
    .tag { display: inline-block; text-transform: uppercase; letter-spacing: 0.25em; font-size: 10px; font-weight: 700; color: #b8412b; margin-bottom: 12px; }
    .foot { margin-top: 32px; padding-top: 20px; border-top: 1px solid #e7e5e4; font-size: 12px; color: #525252; }
  </style>
"""


# Gmail and Outlook strip <html>/<body> and their CSS, so `dir="rtl"` on
# those is lost. Repeat dir="rtl" and inline direction/text-align on every
# wrapper div so RTL survives the strip in every client. Any new template
# should route through this wrapper.
def _wrap_html(body: str) -> str:
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>{_BASE_STYLE}</head>
<body dir="rtl" style="direction: rtl; text-align: right;">
  <div dir="rtl" style="padding: 32px 16px; direction: rtl; text-align: right;">
    <div class="card" dir="rtl" style="direction: rtl; text-align: right;">
      {body}
      <div class="foot" dir="rtl" style="direction: rtl; text-align: right;">
        Klaser · <a href="{html.escape(settings.post_auth_redirect_url)}">klaser.co.il</a>
      </div>
    </div>
  </div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────────────────


def send_registration_invite(
    *,
    to_email: str,
    display_name: str | None,
    tenant_name: str,
    role: str,
    invited_by: str | None,
    invite_url: str,
) -> None:
    """Sent when a product backend creates a new user. Contains the
    /register?token=… link that lets the user set their initial password."""
    role_labels = {"admin": "מנהל", "reviewer": "בודק", "secretary": "מזכיר/ה"}
    role_he = role_labels.get(role, role)
    greeting_name = (display_name or "").strip() or to_email.split("@")[0]

    html_body = _wrap_html(
        f"""
        <div class="tag">ברוכים הבאים ל-Klaser</div>
        <h1>שלום {html.escape(greeting_name)}</h1>
        <p>הוספת חשבון בארגון <strong>{html.escape(tenant_name)}</strong> ב-Klaser
        {"על ידי " + html.escape(invited_by) if invited_by else ""}.</p>
        <p>התפקיד שלך במערכת: <strong>{role_he}</strong>.</p>
        <p>כדי להשלים את ההרשמה ולבחור סיסמה, לחץ על הכפתור:</p>

        <p style="margin: 32px 0;">
          <a href="{html.escape(invite_url)}" class="btn">הגדר סיסמה והתחל ←</a>
        </p>

        <p class="muted">הקישור בתוקף ל-{settings.registration_token_ttl_days} ימים.</p>
        """
    )

    text_body = (
        f"שלום {greeting_name},\n\n"
        f"נוספת לארגון {tenant_name} ב-Klaser"
        + (f" על ידי {invited_by}" if invited_by else "")
        + f".\nהתפקיד שלך: {role_he}.\n\n"
        f"להגדרת סיסמה: {invite_url}\n\n"
        f"הקישור בתוקף ל-{settings.registration_token_ttl_days} ימים.\n\n"
        f"— Klaser"
    )

    _send(
        Message(
            to=to_email,
            subject=f"ברוכים הבאים ל-Klaser · {tenant_name}",
            html_body=html_body,
            text_body=text_body,
        )
    )


def send_welcome_email(
    *,
    to_email: str,
    display_name: str | None,
    tenant_name: str,
) -> None:
    """Sent right after a user completes registration. Confirms the
    account is live and links back to the product."""
    greeting_name = (display_name or "").strip() or to_email.split("@")[0]
    app_url = settings.post_auth_redirect_url

    html_body = _wrap_html(
        f"""
        <div class="tag">Klaser</div>
        <h1>שלום {html.escape(greeting_name)}, החשבון שלך מוכן</h1>
        <p>ההרשמה ל-<strong>{html.escape(tenant_name)}</strong> ב-Klaser הושלמה.
        אפשר להיכנס בכל עת עם האימייל והסיסמה שהגדרת.</p>

        <p style="margin: 32px 0;">
          <a href="{html.escape(app_url)}" class="btn">כניסה למערכת ←</a>
        </p>
        """
    )

    text_body = (
        f"שלום {greeting_name},\n\n"
        f"ההרשמה ל-{tenant_name} ב-Klaser הושלמה.\n"
        f"כניסה למערכת: {app_url}\n\n"
        f"— Klaser"
    )

    _send(
        Message(
            to=to_email,
            subject=f"החשבון שלך ב-Klaser מוכן · {tenant_name}",
            html_body=html_body,
            text_body=text_body,
        )
    )


def send_password_reset_email(
    *,
    to_email: str,
    display_name: str | None,
    reset_url: str,
    ttl_hours: int,
) -> None:
    """Sent from forgot-password. The email is generic on purpose — the
    forgot-password endpoint returns the same response whether or not the
    address exists in the DB."""
    greeting_name = (display_name or "").strip() or to_email.split("@")[0]

    html_body = _wrap_html(
        f"""
        <div class="tag">Klaser · איפוס סיסמה</div>
        <h1>שלום {html.escape(greeting_name)}</h1>
        <p>קיבלנו בקשה לאפס את הסיסמה של החשבון שלך ב-Klaser.
        אם לא ביקשת — אפשר להתעלם מהמייל הזה.</p>

        <p style="margin: 32px 0;">
          <a href="{html.escape(reset_url)}" class="btn">אפס סיסמה ←</a>
        </p>

        <p class="muted">הקישור בתוקף ל-{ttl_hours} שעות.</p>
        """
    )

    text_body = (
        f"שלום {greeting_name},\n\n"
        f"קיבלנו בקשה לאפס את הסיסמה של החשבון שלך ב-Klaser.\n"
        f"אם לא ביקשת — אפשר להתעלם מהמייל הזה.\n\n"
        f"קישור לאיפוס: {reset_url}\n\n"
        f"הקישור בתוקף ל-{ttl_hours} שעות.\n\n"
        f"— Klaser"
    )

    _send(
        Message(
            to=to_email,
            subject="Klaser · איפוס סיסמה",
            html_body=html_body,
            text_body=text_body,
        )
    )

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from jinja2 import Environment, FileSystemLoader

from app.core.config import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 15

# Test hook only. None in production, where httpx uses its real transport.
_transport: Optional[httpx.BaseTransport] = None

def render_template(template_name: str, context: Dict[str, Any]) -> str:
    """Render a Jinja2 template with the given context."""
    templates_dir = Path(settings.EMAIL_TEMPLATES_DIR)
    env = Environment(loader=FileSystemLoader(templates_dir))
    template = env.get_template(f"{template_name}.html")
    return template.render(**context)

def send_email(
    email_to: str,
    subject: str,
    html_content: str,
) -> bool:
    """Send an email through the Resend HTTP API. Returns False (and logs) instead of raising, so a mail problem never
    turns the request that triggered it into a 500."""
    if not (settings.RESEND_KEY and settings.EMAILS_FROM_EMAIL):
        logger.warning("Email is not configured (RESEND_KEY / EMAILS_FROM_EMAIL); email %r was not sent", subject)
        return False

    payload = {
        "from": f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>",
        "to": [email_to],
        "subject": subject,
        "html": html_content,
    }
    headers = {"Authorization": f"Bearer {settings.RESEND_KEY}", "User-Agent": "oakly-backend/1"}
    try:
        with httpx.Client(transport=_transport, timeout=RESEND_TIMEOUT_SECONDS) as client:
            response = client.post(RESEND_URL, json=payload, headers=headers)
        if response.status_code >= 400:
            # Resend's error body names the problem (an unverified domain, a bad key); it never echoes the key.
            logger.warning("Failed to send email %r: Resend answered %s %s", subject, response.status_code, response.text[:300])
            return False
        return True
    except Exception as e:
        logger.warning("Failed to send email %r: %s", subject, e)
        return False

def send_verification_email(email_to: str, code: str) -> None:
    """Send verification email with OTP code."""
    subject = f"{settings.PROJECT_NAME} - Email Verification"
    
    html_content = render_template(
        "verification",
        {
            "project_name": settings.PROJECT_NAME,
            "code": code,
            "valid_minutes": settings.OTP_EXPIRE_MINUTES,
            "colors": {
                "primary": "#168283",
                "secondary": "#FD5E52",
                "secondaryLight": "#FBEFE9",
                "white": "#FFFFFF",
                "blue": "#0066F5",
                "text": "#333333",
                "textLight": "#757575",
                "border": "#E0E0E0",
                "background": "#F5F5F5",
                "success": "#4CAF50",
                "error": "#F44336",
                "warning": "#FFC107",
                "info": "#2196F3",
                "primaryLight": "#F2FDFD",
                "soft": "#F8F8FC"
                # ... other colors ...
            }
        },
    )
    
    send_email(email_to, subject, html_content)


def send_password_reset_email(email_to: str, code: str, legacy_token: str | None = None) -> None:
    """Send the password reset email: the 5-digit code, big. ``legacy_token`` (the long token the shipped caregiver app
    pastes, deprecated) is shown in small print only while it is enabled."""
    subject = f"{settings.PROJECT_NAME} - Your password reset code"

    html_content = render_template(
        "password_reset",
        {
            "project_name": settings.PROJECT_NAME,
            "code": code,
            "valid_minutes": settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES,
            "legacy_token": legacy_token,
            "valid_hours": settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS,
            "colors": {
                "primary": "#168283",
                "secondary": "#FD5E52",
                "secondaryLight": "#FBEFE9",
                "white": "#FFFFFF",
                "blue": "#0066F5",
                "text": "#333333",
                "textLight": "#757575",
                "border": "#E0E0E0",
                "background": "#F5F5F5",
                "success": "#4CAF50",
                "error": "#F44336",
                "warning": "#FFC107",
                "info": "#2196F3",
                "primaryLight": "#F2FDFD",
                "soft": "#F8F8FC"
            }
        },
    )

    send_email(email_to, subject, html_content)


_CAREGIVER_DECISIONS = {
    "approved": (
        "Your application was approved",
        "Congratulations, your application has been approved. You can now receive booking requests.",
    ),
    "rejected": (
        "Your application was not approved",
        "Unfortunately we could not approve your application.",
    ),
    "changes_requested": (
        "Changes needed on your application",
        "We need a few changes before we can approve your application. Please update the items below and resubmit.",
    ),
    "suspended": (
        "Your account has been suspended",
        "Your caregiver account has been suspended and you will not receive new booking requests.",
    ),
}


def send_caregiver_decision_email(
    *,
    email_to: str,
    full_name: str,
    decision: str,
    reason: str | None = None,
    items: list[str] | None = None,
    note: str | None = None,
) -> bool:
    """Tell a caregiver about an admin decision (approved, rejected, changes_requested, suspended)."""
    heading, message = _CAREGIVER_DECISIONS[decision]
    html_content = render_template(
        "caregiver_decision",
        {
            "project_name": settings.PROJECT_NAME,
            "heading": heading,
            "message": message,
            "full_name": full_name,
            "reason": reason,
            "items": items,
            "note": note,
        },
    )
    return send_email(email_to, f"{settings.PROJECT_NAME} - {heading}", html_content)

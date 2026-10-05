import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any, Dict
from jinja2 import Environment, FileSystemLoader
import certifi

from app.core.config import settings

def render_template(template_name: str, context: Dict[str, Any]) -> str:
    """Render a Jinja2 template with the given context."""
    templates_dir = Path(settings.EMAIL_TEMPLATES_DIR)
    env = Environment(loader=FileSystemLoader(templates_dir))
    template = env.get_template(f"{template_name}.html")
    return template.render(**context)

def send_email_smtp(
    email_to: str,
    subject: str,
    html_content: str,
) -> bool:
    """Send an email using Python's built-in smtplib."""
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    message["To"] = email_to
    
    # Attach HTML content
    part = MIMEText(html_content, "html")
    message.attach(part)
    
    # Create secure connection and send email
    try:
        # For SSL (port 465)
        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=context) as server:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, email_to, message.as_string())
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
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
    
    send_email_smtp(email_to, subject, html_content)


def send_password_reset_email(email_to: str, token: str) -> None:
    """Send password reset email with token."""
    subject = f"{settings.PROJECT_NAME} - Password Reset"
    
    html_template = render_template(
        "password_reset",
        {
            "project_name": settings.PROJECT_NAME,
            "token": token,
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
    
    send_email_smtp(
        email_to=email_to,
        subject_template=subject,
        html_template=html_template,
        # environment={"project_name": settings.PROJECT_NAME},
    )


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
    return send_email_smtp(email_to, f"{settings.PROJECT_NAME} - {heading}", html_content)

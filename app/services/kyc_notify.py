"""Telling people what the KYC engine decided (design 6 step 7, 7.3 and 11.6): the caregiver by push and e-mail, ops by e-mail.

Best effort, like every push in this app: the caregiver's app also polls ``GET /identity-verification``, so a notification is never
the source of truth and nothing here raises. Delivery happens AFTER the decision is committed, never inside the locked section.

What a caregiver is told is deliberately coarse (design 12.1 and 7.3): "verified", "try again" and "under review". The reason
(a duplicate identity, a watch list, a name mismatch, a disagreement between webhook and pull) never leaves the server in a
notification, because it would tell a fraudster what tripped. The ops alert names a KIND ("wallet_empty"), never a person or a body.
"""
import html
import logging
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import email as email_service
from app.services import push as push_service

logger = logging.getLogger(__name__)

VERIFIED, TRY_AGAIN, UNDER_REVIEW = "verified", "try_again", "under_review"
COPY = {
    VERIFIED: ("Identity verified", "Your identity has been verified. You can continue with your application."),
    TRY_AGAIN: ("Identity check unsuccessful",
                "We could not verify your identity this time. Please try again in good lighting with your ID ready."),
    UNDER_REVIEW: ("Your identity check is under review",
                   "Our team is reviewing your identity check. You can continue with your application; we will update you."),
}
OPS_ALERT_COPY = {
    "wallet_empty": "The Prembly wallet is out of funds: every caregiver identity check is failing until it is topped up.",
    "unauthorised": "Prembly refused our API key: every caregiver identity check is failing until the key is fixed.",
    "not_configured": "The Prembly API key or organisation id is not configured on the server: identity checks cannot run.",
}


@dataclass(frozen=True)
class Notice:
    """One message for one caregiver. The e-mail address and name are in memory only and hidden from ``repr``."""
    kind: str
    user_id: UUID
    email: Optional[str] = field(default=None, repr=False)
    full_name: str = field(default="", repr=False)


def deliver(db: Session, notice: Notice) -> None:
    """Push and e-mail the caregiver. Never raises."""
    heading, message = COPY[notice.kind]
    try:
        push_service.get_push_service().send_to_users(
            db, [notice.user_id], title=heading, body=message, data={"type": f"kyc_{notice.kind}"})
    except Exception:  # noqa: BLE001 - push is best effort
        logger.warning("kyc push failed kind=%s", notice.kind, exc_info=True)
    if not notice.email:
        return
    try:
        body = email_service.render_template(
            "caregiver_decision", {"project_name": settings.PROJECT_NAME, "heading": heading, "message": message,
                                   "full_name": notice.full_name, "reason": None, "items": None, "note": None})
        email_service.send_email_smtp(notice.email, f"{settings.PROJECT_NAME} - {heading}", body)
    except Exception:  # noqa: BLE001
        logger.warning("kyc email failed kind=%s", notice.kind, exc_info=True)


def alert_ops(kind: str) -> None:
    """The wallet is empty, the key is refused: it breaks every caregiver at once (design 11.6, R2). Always an ERROR log line (what
    the K3 worker already writes); an e-mail too when ``KYC_ALERT_EMAIL`` is set (owner question O5: who is told). Never raises."""
    message = OPS_ALERT_COPY.get(kind, f"Prembly identity verification needs attention ({kind}).")
    logger.error("kyc ops alert: %s", kind)
    if not settings.KYC_ALERT_EMAIL:
        return
    try:
        body = f"<p>{html.escape(message)}</p><p>Check the Prembly dashboard and the server's PREMBLY_* settings.</p>"
        email_service.send_email_smtp(settings.KYC_ALERT_EMAIL, f"{settings.PROJECT_NAME} - KYC provider needs attention", body)
    except Exception:  # noqa: BLE001
        logger.warning("kyc ops alert email failed kind=%s", kind, exc_info=True)

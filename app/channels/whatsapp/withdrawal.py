"""The data effects of STOP DATA, shared by the chat processor and account erasure.

The conversation engine decides *that* a person withdrew; these functions are what withdrawing does to the
database. None of them commits: the caller's transaction owns the commit.
"""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_chat
from app.channels.whatsapp.engine import ALL_WITHDRAWABLE
from app.db.models.whatsapp import WhatsAppContact
from app.services.consent import ConsentService
from app.services.support import COMPLIANCE_REASON, SupportService

CONSENT_CHANNEL = "whatsapp_button"  # becomes whatsapp_flow when the W2 consent Flow replaces the buttons


def withdraw_consents(db: Session, contact: WhatsAppContact, types: Iterable[str],
                      evidence_ref: Optional[str], channel: str = CONSENT_CHANNEL) -> None:
    """Append a withdrawal for each type, and cancel any draft: with consent gone no health answers stay."""
    ConsentService(db).record(
        types=types, decision="withdrawn", channel=channel, evidence_ref=evidence_ref,
        contact_id=contact.id, subject_user_id=contact.user_id, granted_by_user_id=contact.user_id,
    )
    booking_chat.close_open_quotes(db, contact.id)


def opt_out(contact: WhatsAppContact, now: datetime) -> None:
    contact.opted_out_at = now
    contact.marketing_opt_in = False


def stop_data(db: Session, contact: WhatsAppContact, *, evidence_ref: Optional[str], now: datetime,
              channel: str = CONSENT_CHANNEL) -> None:
    """Everything STOP DATA does to the rows: consents withdrawn, drafts closed, opted out, compliance task."""
    withdraw_consents(db, contact, ALL_WITHDRAWABLE, evidence_ref, channel)
    opt_out(contact, now)
    SupportService(db).open(contact_id=contact.id, reason=COMPLIANCE_REASON, priority="normal")

"""Phone-first identity: the Oakly user behind a WhatsApp contact (WA-FR-2, WA-FR-3)."""

import logging
import secrets
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.phone import contact_ref, phone_variants
from app.core.security import get_password_hash
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.patient import Patient
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact

logger = logging.getLogger(__name__)

DEFAULT_NAME = "WhatsApp patient"


def find_user_by_phone(db: Session, phone_e164: str) -> Optional[User]:
    """Match an older account whatever format it stored the number in (+234..., 234..., 080...)."""
    return db.query(User).filter(User.phone_number.in_(phone_variants(phone_e164))).first()


def ensure_user(db: Session, contact: WhatsAppContact) -> bool:
    """Make sure the contact has a patient user. Returns False when the number already belongs to an
    existing account: that must be linked with a one-time code (WA-FR-3), never automatically, so the
    contact is left without a user and the conversation carries on without one."""
    if contact.user_id is not None:
        return True
    if find_user_by_phone(db, contact.phone_e164) is not None:
        logger.warning("whatsapp contact %s matches an existing account; link needed", contact_ref(contact.wa_id))
        return False

    name = contact.display_name or DEFAULT_NAME
    try:
        with db.begin_nested():
            user = User(
                email=None, phone_number=contact.phone_e164, full_name=name, user_type="patient",
                # A real hash of a random secret: nobody knows it, and password checks still work
                # (an unparseable placeholder would make login raise instead of failing cleanly).
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                is_active=True, is_verified=False,
            )
            db.add(user)
            db.flush()
            db.add(Patient(user_id=user.id, email=None, full_name=name, phone_number=contact.phone_e164))
            recipient = CareRecipient(full_name=name, phone_e164=contact.phone_e164, created_by_user_id=user.id)
            db.add(recipient)
            db.flush()
            db.add(CareRelationship(contact_user_id=user.id, recipient_id=recipient.id, role="self",
                                    clinical_access=True))
    except IntegrityError:
        # An app registration took the number between our check and our insert.
        logger.warning("whatsapp contact %s lost a race for its phone number", contact_ref(contact.wa_id))
        return False
    contact.user_id = user.id
    return True

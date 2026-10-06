"""Factories for the WhatsApp channel tests (kept apart from factories.py)."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.support import SupportConversation
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation


def make_wa_id() -> str:
    return f"23480{uuid.uuid4().int % 10**8:08d}"  # a placeholder Nigerian-shaped number


def make_contact(db: Session, wa_id: str | None = None, state: str = "new", **overrides) -> WhatsAppContact:
    wa_id = wa_id or make_wa_id()
    contact = WhatsAppContact(wa_id=wa_id, phone_e164="+" + wa_id, last_inbound_at=datetime.utcnow(),
                              **overrides)
    db.add(contact)
    db.flush()
    db.add(WhatsAppConversation(contact_id=contact.id, state=state, context={}))
    db.commit()
    db.refresh(contact)
    return contact


def open_tickets(db: Session) -> list[SupportConversation]:
    return db.query(SupportConversation).filter(SupportConversation.status != "resolved").all()

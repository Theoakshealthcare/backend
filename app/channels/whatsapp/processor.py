"""Applies one inbound WhatsApp message: load the conversation, run the engine, apply the outcome.

Shape of ``process_inbound``
 1. In ONE transaction: claim the message (``processed_at``), make sure the contact has a user, run
    the pure engine, and apply its effects (consent rows, opt-out flags, handover tickets, new
    conversation state). Either all of it commits or none of it does, so a crash mid-way leaves the
    message unclaimed and the queue retry starts clean.
 2. After the commit: send the replies and record each as metadata. A send that fails does not undo
    the conversation step (the failure is on the ``whatsapp_messages`` row); resending a reply after
    a retry would otherwise double-send to the patient.

The row lock on the conversation serialises two messages from the same contact handled by two workers.
Nothing the user typed is stored or logged: only counters, flags and Meta ids.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.channels.whatsapp.engine import (
    PAUSED_STATES, EngineConfig, Inbound, OpenTicket, RecordConsent, SetOptOut, Snapshot, State, step,
)
from app.channels.whatsapp.identity import ensure_user
from app.channels.whatsapp.messages import OutboundMessage
from app.channels.whatsapp.meta_client import MetaCloudClient
from app.channels.whatsapp.outbound import send_and_record
from app.core.config import settings
from app.core.phone import contact_ref
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.services.consent import ConsentService
from app.services.support import SupportService

logger = logging.getLogger(__name__)

CONSENT_CHANNEL = "whatsapp_button"  # becomes whatsapp_flow when the W2 consent Flow replaces the buttons


def engine_config() -> EngineConfig:
    numbers = tuple(n.strip() for n in settings.WHATSAPP_EMERGENCY_NUMBERS.split(",") if n.strip()) or ("112",)
    return EngineConfig(
        emergency_numbers=numbers,
        support_phone=settings.WHATSAPP_SUPPORT_PHONE,
        privacy_url=settings.WHATSAPP_PRIVACY_POLICY_URL,
        policy_version=settings.WHATSAPP_POLICY_VERSION,
    )


def to_inbound(job: Dict[str, Any]) -> Inbound:
    kind = job.get("type")
    if kind == "text":
        return Inbound.text(job.get("text") or "")
    if kind == "interactive":
        reply_id = job.get("reply_id")
        if job.get("interactive_type") == "button_reply" and reply_id:
            return Inbound.button(reply_id)
        if job.get("interactive_type") == "list_reply" and reply_id:
            return Inbound.list_choice(reply_id)
        return Inbound.media()
    if kind == "location":
        return Inbound.location()
    if kind in ("reaction", "system"):
        return Inbound.ignored()
    return Inbound.media()


def process_inbound(db: Session, job: Dict[str, Any], client: Optional[MetaCloudClient] = None) -> None:
    try:
        applied = _apply(db, job)
    except Exception:
        db.rollback()
        logger.exception("whatsapp inbound %s failed; rolled back", _short(job))
        raise
    if applied is None:
        return
    contact, replies = applied
    if not replies:
        return
    client = client or MetaCloudClient.from_settings()
    for message in replies:
        try:
            send_and_record(db, client, contact, message)
        except Exception:
            db.rollback()
            logger.exception("whatsapp reply for contact %s could not be recorded", contact_ref(contact.wa_id))


def _short(job: Dict[str, Any]) -> str:
    return str(job.get("meta_message_id"))


def _apply(db: Session, job: Dict[str, Any]) -> Optional[Tuple[WhatsAppContact, List[OutboundMessage]]]:
    message = (
        db.query(WhatsAppMessage)
        .filter(WhatsAppMessage.meta_message_id == job["meta_message_id"], WhatsAppMessage.direction == "inbound")
        .populate_existing().with_for_update().one_or_none()
    )
    if message is None:
        logger.warning("whatsapp inbound %s has no row (queue retry after a rollback?); skipped", _short(job))
        db.rollback()
        return None
    if message.processed_at is not None:
        db.rollback()
        return None

    contact = db.query(WhatsAppContact).filter_by(id=message.contact_id).populate_existing().one()
    conversation = (
        db.query(WhatsAppConversation).filter_by(contact_id=contact.id).populate_existing().with_for_update().one()
    )
    now = datetime.utcnow()
    if contact.blocked:
        message.processed_at = now
        db.commit()
        return None

    context = dict(conversation.context or {})
    if ensure_user(db, contact):
        context.pop("link_required", None)
    else:
        context["link_required"] = True

    consents = ConsentService(db)
    snapshot = Snapshot(
        state=State(conversation.state), context=context,
        consent_ok=consents.has_required_consent(contact_id=contact.id),
        opted_out=contact.opted_out_at is not None, language=contact.language,
    )
    outcome = step(snapshot, to_inbound(job), engine_config())

    for effect in outcome.effects:
        if isinstance(effect, RecordConsent):
            consents.record(
                types=effect.types, decision=effect.decision, channel=CONSENT_CHANNEL,
                evidence_ref=message.meta_message_id, contact_id=contact.id,
                subject_user_id=contact.user_id, granted_by_user_id=contact.user_id,
            )
            if effect.decision == "granted" and contact.opted_in_at is None:
                contact.opted_in_at = now
        elif isinstance(effect, SetOptOut):
            contact.opted_out_at = now if effect.opted_out else None
            if effect.opted_out:
                contact.marketing_opt_in = False
        elif isinstance(effect, OpenTicket):
            SupportService(db).open(contact_id=contact.id, reason=effect.reason, priority=effect.priority)

    conversation.state = outcome.state.value
    conversation.context = dict(outcome.context)
    conversation.automation_paused = outcome.state in PAUSED_STATES
    message.processed_at = now
    db.commit()
    logger.info("whatsapp contact=%s %s -> %s", contact_ref(contact.wa_id), snapshot.state.value,
                outcome.state.value)
    return contact, list(outcome.replies)

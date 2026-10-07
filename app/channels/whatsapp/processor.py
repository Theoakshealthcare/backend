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

Booking effects (W2) are applied by ``booking_chat``. Most finish inside the transaction. The slow part of a
few (asking Paystack for the checkout link, verifying a payment when the person comes back) is returned as an
``after_commit`` step and runs between the commit and the replies: it never runs while the conversation row is
locked, and what it returns is sent after the engine's own replies.
"""

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_chat, copy, my_bookings, withdrawal
from app.channels.whatsapp.engine import (
    BOOKING_EFFECTS, MY_BOOKINGS_EFFECTS, PAUSED_STATES, EngineConfig, Inbound, Kind, OpenTicket, RecordConsent, SetOptOut, Snapshot,
    State, step,
)
from app.channels.whatsapp.identity import ensure_user
from app.channels.whatsapp.lexicon import is_emergency
from app.channels.whatsapp.messages import OutboundMessage
from app.channels.whatsapp.meta_client import MetaCloudClient
from app.channels.whatsapp.outbound import send_and_record
from app.core.config import settings
from app.core.phone import contact_ref
from app.db.models.support import SupportConversation
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.services.consent import ConsentService
from app.services.support import SupportService

logger = logging.getLogger(__name__)

CONSENT_CHANNEL = withdrawal.CONSENT_CHANNEL


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
    if kind in ("image", "video", "document"):
        return Inbound.media(job.get("text"))  # the caption, screened for emergencies only
    if kind == "interactive":
        reply_id = job.get("reply_id")
        if job.get("interactive_type") == "button_reply" and reply_id:
            return Inbound.button(reply_id)
        if job.get("interactive_type") == "list_reply" and reply_id:
            return Inbound.list_choice(reply_id)
        if job.get("interactive_type") == "nfm_reply" and reply_id:
            return Inbound.flow(reply_id)  # a finished Flow: only our flow token travels in it
        return Inbound.media()
    if kind == "button":  # a tap on a template's quick-reply button
        reply_id = job.get("reply_id")
        return Inbound.button(reply_id) if reply_id else Inbound.media()
    if kind == "location":
        lat, lng = job.get("latitude"), job.get("longitude")
        return Inbound.location(lat if isinstance(lat, (int, float)) else None,
                                lng if isinstance(lng, (int, float)) else None)
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
    contact, replies, emergency, followups = applied
    replies = replies + _run_followups(db, contact, followups, job)
    if not replies:
        return
    client = client or MetaCloudClient.from_settings()
    for message in replies:
        delivered = False
        try:
            delivered = send_and_record(db, client, contact, message).ok
        except Exception:
            db.rollback()
            logger.exception("whatsapp reply for contact %s could not be recorded", contact_ref(contact.wa_id))
        if emergency and not delivered:
            _escalate_failed_emergency_reply(db, contact)


def _run_followups(db: Session, contact: WhatsAppContact, followups: List[Callable], job: Dict[str, Any]
                   ) -> List[OutboundMessage]:
    """The slow half of a booking step, after the commit. A crash here must not undo the step (the message is
    already marked processed) and must not leave the person without an answer."""
    replies: List[OutboundMessage] = []
    for followup in followups:
        try:
            result = booking_chat.apply_after_commit(db, contact.id, followup(db))
            replies.extend(result.replies)
        except Exception:
            db.rollback()
            logger.exception("whatsapp booking follow-up for %s failed", _short(job))
            replies.append(copy.text(contact.language, "pay_failed_body"))
    return replies


def _escalate_failed_emergency_reply(db: Session, contact: WhatsAppContact) -> None:
    """The emergency screen is the one reply that must not silently fail: flag the urgent ticket so a human
    phones the person, and shout in the log (the line a pager hooks onto). Contact ref only, never the number."""
    ref = contact_ref(contact.wa_id)
    try:
        ticket = (db.query(SupportConversation)
                  .filter(SupportConversation.contact_id == contact.id, SupportConversation.reason == "emergency",
                          SupportConversation.status != "resolved")
                  .with_for_update().first())
        if ticket is not None:
            ticket.reply_failed_at = datetime.utcnow()
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("whatsapp could not flag the failed emergency reply for contact %s", ref)
    logger.error("whatsapp EMERGENCY reply could not be delivered to contact %s: a person must reach out", ref)


CONTENT_TYPES = ("text", "interactive", "image", "video", "document", "location", "button")  # the body key holds what was said


def _handle_missing_body(db: Session, message: WhatsAppMessage, job: Dict[str, Any]):
    """The body key is gone. Returns None when the message needs no body (carry on), otherwise a
    ``(contact, replies, emergency)`` result (no replies = skip).

    - Row younger than the TTL: the key was removed by a race with an older job. Skip WITHOUT claiming the
      row; the retry that owns it carries the text.
    - Row older than the TTL: the key genuinely expired (worker outage). The message was never read: mark it
      ``body_lost`` (not processed-with-content), ask the person to send it again, change nothing else, count
      no miss. Whatever it said may have been an emergency, so this is logged at ERROR.
    """
    if message.type not in CONTENT_TYPES:
        return None
    age = (datetime.utcnow() - message.created_at).total_seconds()
    if age <= settings.WHATSAPP_BODY_TTL_SECONDS:
        logger.warning("whatsapp inbound %s has no body key yet the row is young; skipped unclaimed", _short(job))
        db.rollback()
        return (None, [], False)
    contact = db.query(WhatsAppContact).filter_by(id=message.contact_id).populate_existing().one()
    message.status = "body_lost"
    message.processed_at = datetime.utcnow()
    db.commit()
    logger.error("whatsapp inbound %s from contact %s lost its body (expired before a worker read it); "
                 "asking to resend", _short(job), contact_ref(contact.wa_id))
    if contact.blocked:
        return (None, [], False)
    return (contact, [copy.text(contact.language, "resend_body")], False)


def _is_emergency_input(inbound: Inbound) -> bool:
    return inbound.kind in (Kind.TEXT, Kind.MEDIA) and bool(inbound.body) and is_emergency(inbound.body)


def _short(job: Dict[str, Any]) -> str:
    return str(job.get("meta_message_id"))


def _apply(db: Session, job: Dict[str, Any]
           ) -> Optional[Tuple[WhatsAppContact, List[OutboundMessage], bool, List[Callable]]]:
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

    if job.get("body_missing"):
        lost = _handle_missing_body(db, message, job)
        if lost is not None:
            return (*lost, []) if lost[1] else None
        job = {**job, "type": message.type}  # nothing to read in this kind of message: carry on from the row

    contact = db.query(WhatsAppContact).filter_by(id=message.contact_id).populate_existing().one()
    conversation = (
        db.query(WhatsAppConversation).filter_by(contact_id=contact.id).populate_existing().with_for_update().one()
    )
    now = datetime.utcnow()
    inbound = to_inbound(job)
    if contact.blocked and not _is_emergency_input(inbound):
        # Blocked contacts get nothing, except the emergency screen: a person in danger is not a spammer.
        message.processed_at = now
        db.commit()
        return None

    context = dict(conversation.context or {})
    if ensure_user(db, contact):
        context.pop("link_required", None)
    else:
        context["link_required"] = True

    # WA-FR-12: an unfinished booking that has sat idle past the limit starts fresh from the menu. A booking
    # that already exists is never touched here; it follows its own payment expiry.
    expired = booking_chat.idle_expired(conversation, now)
    if expired:
        booking_chat.expire_idle(db, conversation)
        conversation.state = State.IDLE.value
        context = {k: v for k, v in context.items() if k not in ("quote_id", "booking_id", "waitlist_quote_id")}
        conversation.context = dict(context)

    consents = ConsentService(db)
    snapshot = Snapshot(
        state=State(conversation.state), context=context,
        consent_ok=consents.has_required_consent(contact_id=contact.id),
        opted_out=contact.opted_out_at is not None, language=contact.language,
        can_book=contact.user_id is not None and bool(settings.WHATSAPP_BOOKING_FLOW_ID), expired=expired,
        has_account=contact.user_id is not None,
    )
    outcome = step(snapshot, inbound, engine_config())

    replies = list(outcome.replies)
    state, new_context = outcome.state, dict(outcome.context)
    followups: List[Callable] = []
    ctx = booking_chat.Ctx(db, contact, conversation, now)
    for effect in outcome.effects:
        if isinstance(effect, BOOKING_EFFECTS + MY_BOOKINGS_EFFECTS):
            result = (my_bookings.apply if isinstance(effect, MY_BOOKINGS_EFFECTS) else booking_chat.apply)(ctx, effect)
            replies.extend(result.replies)
            state = result.state or state
            for key, value in result.context.items():
                if value is None:
                    new_context.pop(key, None)
                else:
                    new_context[key] = value
            if result.after_commit is not None:
                followups.append(result.after_commit)
            continue
        if isinstance(effect, RecordConsent):
            if effect.decision == "withdrawn":
                withdrawal.withdraw_consents(db, contact, effect.types, message.meta_message_id)
            else:
                consents.record(
                    types=effect.types, decision=effect.decision, channel=CONSENT_CHANNEL,
                    evidence_ref=message.meta_message_id, contact_id=contact.id,
                    subject_user_id=contact.user_id, granted_by_user_id=contact.user_id,
                )
            if effect.decision == "granted" and contact.opted_in_at is None:
                contact.opted_in_at = now
        elif isinstance(effect, SetOptOut):
            if effect.opted_out:
                withdrawal.opt_out(contact, now)
            else:
                contact.opted_out_at = None
        elif isinstance(effect, OpenTicket):
            SupportService(db).open(contact_id=contact.id, reason=effect.reason, priority=effect.priority)

    conversation.state = state.value
    conversation.context = new_context
    conversation.automation_paused = state in PAUSED_STATES
    conversation.updated_at = now
    message.processed_at = now
    db.commit()
    logger.info("whatsapp contact=%s %s -> %s", contact_ref(contact.wa_id), snapshot.state.value, state.value)
    emergency = any(isinstance(e, OpenTicket) and e.reason == "emergency" for e in outcome.effects)
    return contact, replies, emergency, followups

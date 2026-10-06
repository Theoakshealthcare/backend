"""Turns a verified Meta webhook payload into stored metadata and queue jobs.

Payload format: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components

What happens here is deliberately small and fast (Meta wants a 200 quickly): parse, make sure the
contact exists, record the inbound message id (the dedupe key) and apply status callbacks. The
conversation logic runs later in the worker. Message text (and image captions) travel to the worker through a short-lived
Redis key (see queue.py) and are never written to the database.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.phone import contact_ref, normalize_e164
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage

logger = logging.getLogger(__name__)

_REF_CODE = re.compile(r"\bref:([A-Za-z0-9_-]{1,32})\b")
STATUS_RANK = {"received": 0, "failed": 0, "sent": 1, "delivered": 2, "read": 3}


@dataclass
class InboundEnvelope:
    meta_message_id: str
    wa_id: str
    name: Optional[str]
    type: str
    text: Optional[str] = None
    reply_id: Optional[str] = None
    interactive_type: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timestamp: Optional[str] = None
    referral: bool = False


@dataclass
class StatusUpdate:
    meta_message_id: str
    wa_id: str
    status: str
    error_code: Optional[str] = None
    category: Optional[str] = None


@dataclass
class ParsedWebhook:
    messages: List[InboundEnvelope] = field(default_factory=list)
    statuses: List[StatusUpdate] = field(default_factory=list)


@dataclass
class IngestResult:
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    statuses_applied: int = 0


def _dicts(items: Any) -> List[Dict[str, Any]]:
    """The dict members of a list; anything else (hostile or malformed) is dropped."""
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _ident(value: Any) -> Optional[str]:
    """A Meta id / type word: short, printable. A NUL byte would make Postgres raise (a 500 and a retry storm)."""
    return value if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable() else None


def _name(value: Any) -> Optional[str]:
    return value.replace("\x00", "").strip()[:200] or None if isinstance(value, str) else None


def parse_webhook(payload: Dict[str, Any], expected_phone_number_id: Optional[str] = None) -> ParsedWebhook:
    """Extract messages and status callbacks. Anything malformed is skipped, never raised: a bad
    entry must not turn into a 500 that makes Meta retry the whole batch forever. The payload is
    signed but still untrusted shape-wise (the sender is Meta, the content is a stranger's)."""
    expected = settings.WHATSAPP_PHONE_NUMBER_ID if expected_phone_number_id is None else expected_phone_number_id
    parsed = ParsedWebhook()
    if not isinstance(payload, dict):
        return parsed
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = _dict(change.get("value"))
            if change.get("field") not in (None, "messages"):
                continue
            number_id = _dict(value.get("metadata")).get("phone_number_id")
            if expected and isinstance(number_id, str) and number_id and number_id != expected:
                logger.warning("whatsapp webhook for another phone number id ignored")
                continue
            names = {c.get("wa_id"): _name(_dict(c.get("profile")).get("name"))
                     for c in _dicts(value.get("contacts")) if isinstance(c.get("wa_id"), str)}
            for raw in _dicts(value.get("messages")):
                try:
                    envelope = _parse_message(raw, names)
                except Exception:  # a shape nobody anticipated: skip this one item, keep the batch
                    logger.warning("whatsapp message %s could not be parsed; skipped", _str(raw.get("id")))
                    continue
                if envelope:
                    parsed.messages.append(envelope)
            for raw in _dicts(value.get("statuses")):
                update = _parse_status(raw)
                if update:
                    parsed.statuses.append(update)
    return parsed


_CAPTIONED = ("image", "video", "document")


def _parse_message(raw: Dict[str, Any], names: Dict[str, Optional[str]]) -> Optional[InboundEnvelope]:
    message_id, wa_id, kind = _ident(raw.get("id")), _ident(raw.get("from")), _ident(raw.get("type"))
    if not (message_id and wa_id and kind):
        return None
    envelope = InboundEnvelope(meta_message_id=message_id, wa_id=wa_id, name=names.get(wa_id), type=kind,
                               timestamp=str(raw.get("timestamp") or ""), referral=bool(raw.get("referral")))
    if kind == "text":
        envelope.text = _str(_dict(raw.get("text")).get("body"))
    elif kind in _CAPTIONED:
        envelope.text = _str(_dict(raw.get(kind)).get("caption"))  # screened for emergencies, nothing else
    elif kind == "interactive":
        interactive = _dict(raw.get("interactive"))
        envelope.interactive_type = _str(interactive.get("type"))
        chosen = _dict(interactive.get(envelope.interactive_type)) if envelope.interactive_type else {}
        if envelope.interactive_type == "nfm_reply":
            envelope.reply_id = _flow_token(chosen.get("response_json"))
        else:
            envelope.reply_id = _str(chosen.get("id"))
    elif kind == "button":  # a tap on a template's quick-reply button
        button = _dict(raw.get("button"))
        envelope.interactive_type = "template_button"
        envelope.reply_id = _ident(button.get("payload"))
    elif kind == "location":
        location = _dict(raw.get("location"))
        envelope.latitude, envelope.longitude = _coordinate(location.get("latitude")), _coordinate(
            location.get("longitude"))
    return envelope


def _flow_token(response_json: Any) -> Optional[str]:
    """The flow token out of a completed Flow's ``response_json`` (a JSON string). Our Flow completes with
    nothing else: the answers went to the encrypted data endpoint, so no answer is ever in this webhook."""
    try:
        decoded = json.loads(response_json) if isinstance(response_json, str) and len(response_json) < 4096 else {}
    except ValueError:
        return None
    token = decoded.get("flow_token") if isinstance(decoded, dict) else None
    return token if isinstance(token, str) and 0 < len(token) <= 200 and token.isprintable() else None


def _coordinate(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _parse_status(raw: Dict[str, Any]) -> Optional[StatusUpdate]:
    message_id, status, recipient = _ident(raw.get("id")), _ident(raw.get("status")), _ident(raw.get("recipient_id"))
    if not (message_id and status and recipient):
        return None
    if status not in ("sent", "delivered", "read", "failed"):
        return None
    errors = _dicts(raw.get("errors"))
    code = str(errors[0]["code"]) if errors and "code" in errors[0] else None
    return StatusUpdate(message_id, recipient, status, code, _ident(_dict(raw.get("pricing")).get("category")))


def _source_for(envelope: InboundEnvelope) -> str:
    if envelope.referral:
        return "ctwa"
    match = _REF_CODE.search(envelope.text or "")
    return f"ref:{match.group(1)}" if match else "direct"


def _ensure_contact(db: Session, envelope: InboundEnvelope) -> Optional[WhatsAppContact]:
    phone = normalize_e164(envelope.wa_id)
    if phone is None:
        logger.warning("whatsapp message from an unusable wa_id (ref %s) ignored", contact_ref(envelope.wa_id))
        return None
    db.execute(
        insert(WhatsAppContact).values(
            wa_id=envelope.wa_id, phone_e164=phone, display_name=envelope.name,
            source=_source_for(envelope), created_at=datetime.utcnow(),
        ).on_conflict_do_nothing()
    )
    contact = db.query(WhatsAppContact).filter_by(wa_id=envelope.wa_id).one_or_none()
    if contact is None:  # the number belongs to a different wa_id: refuse rather than merge identities
        logger.warning("whatsapp contact conflict (ref %s) ignored", contact_ref(envelope.wa_id))
        return None
    db.execute(
        insert(WhatsAppConversation).values(
            contact_id=contact.id, state="new", context={}, automation_paused=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ).on_conflict_do_nothing(index_elements=["contact_id"])
    )
    return contact


def ingest(db: Session, parsed: ParsedWebhook) -> IngestResult:
    result = IngestResult()
    for envelope in parsed.messages:
        contact = _ensure_contact(db, envelope)
        if contact is None:
            continue
        inserted = db.execute(
            insert(WhatsAppMessage).values(
                contact_id=contact.id, direction="inbound", meta_message_id=envelope.meta_message_id,
                type=envelope.type, status="received", created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ).on_conflict_do_nothing(index_elements=["meta_message_id"]).returning(WhatsAppMessage.id)
        ).first()
        if inserted is None:
            result.duplicates += 1
            continue
        contact.last_inbound_at = datetime.utcnow()
        if envelope.name and not contact.display_name:
            contact.display_name = envelope.name
        result.jobs.append({
            "meta_message_id": envelope.meta_message_id, "wa_id": envelope.wa_id, "name": envelope.name,
            "type": envelope.type, "text": envelope.text, "reply_id": envelope.reply_id,
            "interactive_type": envelope.interactive_type, "latitude": envelope.latitude,
            "longitude": envelope.longitude, "timestamp": envelope.timestamp,
        })
    for update in parsed.statuses:
        if _apply_status(db, update):
            result.statuses_applied += 1
    db.commit()
    return result


def _apply_status(db: Session, update: StatusUpdate) -> bool:
    contact = db.query(WhatsAppContact).filter_by(wa_id=update.wa_id).one_or_none()
    if contact is None:
        return False
    # An early callback can arrive before our send call has been written down; keep it as a stub that
    # outbound.record_outbound() completes.
    db.execute(
        insert(WhatsAppMessage).values(
            contact_id=contact.id, direction="outbound", meta_message_id=update.meta_message_id,
            type="unknown", status="sent", created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ).on_conflict_do_nothing(index_elements=["meta_message_id"])
    )
    row = (db.query(WhatsAppMessage).filter_by(meta_message_id=update.meta_message_id)
           .populate_existing().with_for_update().one())
    if row.contact_id != contact.id or row.direction != "outbound":
        return False
    if update.category:
        row.cost_category = update.category
    if update.status == "failed":
        if row.status in ("delivered", "read"):
            return False
        row.status, row.error_code = "failed", update.error_code
    elif STATUS_RANK[update.status] > STATUS_RANK.get(row.status, 0):
        row.status = update.status
    else:
        return False
    return True


def compensate(db: Session, jobs: List[Dict[str, Any]]) -> None:
    """The queue refused the jobs: forget the inbound rows so Meta's retry is not deduped away."""
    ids = [j["meta_message_id"] for j in jobs]
    if not ids:
        return
    db.query(WhatsAppMessage).filter(
        WhatsAppMessage.meta_message_id.in_(ids), WhatsAppMessage.direction == "inbound",
        WhatsAppMessage.processed_at.is_(None),
    ).delete(synchronize_session=False)
    db.commit()

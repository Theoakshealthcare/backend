"""Send a message and write down its metadata (never its body).

The 24-hour rule lives here, in the one function every send goes through: free-form messages (text, buttons,
lists, CTA, Flow, location request) are only sent while the contact has written to us in the last 24 hours;
outside it only a template goes out. A free-form send outside the window is not attempted (Meta would refuse it
with 131047 and it would count against our quality); it is recorded as failed with ``window_closed`` so the
caller can fall back to a template (see ``templates.choose``).
"""

import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.channels.whatsapp.messages import OutboundMessage, TemplateMessage
from app.channels.whatsapp.meta_client import MetaCloudClient, SendResult
from app.channels.whatsapp.templates import within_window
from app.core.phone import contact_ref
from app.db.models.whatsapp import WhatsAppContact, WhatsAppMessage

logger = logging.getLogger(__name__)

WINDOW_CLOSED = "window_closed"


def send_and_record(db: Session, client: MetaCloudClient, contact: WhatsAppContact,
                    message: OutboundMessage, *, now: Optional[datetime] = None,
                    on_accepted: Optional[Callable[[SendResult], None]] = None) -> SendResult:
    """``on_accepted`` runs the instant Meta has accepted the message, before anything else is written, so a
    caller that must not send twice can record "sent" while the window for a crash is as small as it can be."""
    now = now or datetime.utcnow()
    if not isinstance(message, TemplateMessage) and not within_window(contact.last_inbound_at, now):
        result = SendResult(ok=False, error_code=WINDOW_CLOSED, attempts=0)
        logger.warning("whatsapp free-form message to contact %s refused: outside the 24 h window",
                       contact_ref(contact.wa_id))
        record_outbound(db, contact.id, message, result)
        return result
    result = client.send(contact.wa_id, message)
    if result.ok and on_accepted is not None:
        on_accepted(result)
    record_outbound(db, contact.id, message, result)
    return result


def record_outbound(db: Session, contact_id, message: OutboundMessage, result: SendResult) -> None:
    template_name: Optional[str] = message.name if isinstance(message, TemplateMessage) else None
    category: Optional[str] = message.category if isinstance(message, TemplateMessage) else None
    now = datetime.utcnow()
    values = dict(contact_id=contact_id, direction="outbound", type=message.kind, template_name=template_name,
                  category=category, created_at=now, updated_at=now)
    if result.ok:
        # Upsert: a delivery callback may already have created the row (see ingest._apply_status). Its
        # status is further along than "sent", so only the descriptive columns are filled in.
        stmt = insert(WhatsAppMessage).values(**values, meta_message_id=result.meta_message_id, status="sent")
        stmt = stmt.on_conflict_do_update(
            index_elements=["meta_message_id"],
            set_={"type": stmt.excluded.type, "template_name": stmt.excluded.template_name,
                  "category": stmt.excluded.category, "updated_at": now},
        )
    else:
        stmt = insert(WhatsAppMessage).values(**values, meta_message_id=None, status="failed",
                                              error_code=result.error_code)
    db.execute(stmt)
    db.commit()
